import re

import pytest

from infomentor_mcp.auth.discovery import DISCOVERY_PATH
from infomentor_mcp.auth.mobile import (
    API_ORIGIN,
    AUTH_ORIGIN,
    AUTH_PATH,
    REVOKE_PATH,
    TOKEN_PATH,
    MobileAuthorization,
)
from infomentor_mcp.errors import AuthenticationExpiredError, MobileFlowError
from infomentor_mcp.hub.transport import HUB_ORIGIN, HubTransport
from infomentor_mcp.storage.credentials import MobileCredentials, MobileCredentialStore
from infomentor_mcp.storage.session import AuthSession, SessionStore

PAGE = "<script>var pupils = ['/Account/PupilSwitcher/SwitchPupil/synthetic-child'];</script>"
DISCOVERY = {
    "apiUrl": API_ORIGIN,
    "authenticationUrl": AUTH_ORIGIN + AUTH_PATH + "?authGuid=synthetic-guid",
    "tokenUrl": AUTH_ORIGIN + TOKEN_PATH,
    "revokeRefreshTokenUrl": AUTH_ORIGIN + REVOKE_PATH + "?authGuid=synthetic-guid",
    "clientId": "notificationapp", "expirationTime": "unused",
}


def prepare_pairing(httpx_mock, *, callback=None, sign_in_origin=None):
    store = SessionStore()
    store.save(AuthSession())
    httpx_mock.add_response(
        url=HUB_ORIGIN + "/timetable/timetable/appData", json={"items": []}, is_reusable=True
    )
    httpx_mock.add_response(url=HUB_ORIGIN + "/", text=PAGE, is_reusable=True)
    httpx_mock.add_response(url=HUB_ORIGIN + DISCOVERY_PATH, json=DISCOVERY)
    httpx_mock.add_response(
        url=re.compile(re.escape(AUTH_ORIGIN + AUTH_PATH) + r"\?.*"),
        method="GET", status_code=302,
        headers={"location": callback or "InfomentorNotification://oauth2Callback?code=synthetic-code"},
    )
    if callback is None:
        httpx_mock.add_response(
            url=AUTH_ORIGIN + TOKEN_PATH,
            json={"access_token": "synthetic-access", "refresh_token": "synthetic-refresh",
                  "token_type": "Bearer", "expires_in": 3600},
        )
        httpx_mock.add_response(
            url=API_ORIGIN + "/NA1/Authentication/sso",
            json=(sign_in_origin or AUTH_ORIGIN) + "/mobile-sign-in?secret=synthetic-sign-in",
        )
        if sign_in_origin is None:
            httpx_mock.add_response(
                url=AUTH_ORIGIN + "/mobile-sign-in?secret=synthetic-sign-in",
                status_code=302, headers={"location": HUB_ORIGIN + "/"},
            )
    return store


def test_pairing_persists_tokens_and_verifies_fresh_child_scope(httpx_mock):
    store = prepare_pairing(httpx_mock)
    flow = MobileAuthorization(store)
    try:
        flow.complete_login()
        credentials = flow.tokens.load()
        assert credentials.verified
        assert credentials.refresh_token.get_secret_value() == "synthetic-refresh"
        assert store.load().mobile_binding == credentials.binding
        assert "synthetic-refresh" not in credentials.model_dump_json()
        assert flow.tokens.path.stat().st_mode & 0o777 == 0o600
        requests = httpx_mock.get_requests()
        authorize = next(request for request in requests if request.url.path == AUTH_PATH)
        assert authorize.url.params["DeviceFriendlyName"].startswith("InfoMentor MCP")
        assert authorize.url.params["DevicePlatform"] == "Android"
        assert "cookie" not in authorize.headers
        assert all(
            "authorization" not in request.headers
            for request in requests if request.url.host != "api-im.infomentor.se"
        )
    finally:
        flow.close()


@pytest.mark.parametrize("outcome,expected", [
    ("valid", ["refresh", "revoke", "pair"]),
    ("rejected", ["refresh", "revoke", "pair"]),
    ("network", ["refresh"]),
    ("different_scope", []),
])
def test_login_cleans_up_mobile_authorization_before_replacing(
    monkeypatch, outcome, expected,
):
    store = SessionStore()
    store.save(AuthSession())
    flow = MobileAuthorization(store)
    flow.tokens.save(MobileCredentials(
        access_token="access", refresh_token="refresh", device_id="device",
        binding="old-binding", children_fingerprint="scope",
    ))
    monkeypatch.setattr(HubTransport, "verify_authenticated_read", lambda _: 200)
    monkeypatch.setattr(MobileAuthorization, "_children_fingerprint",
                        lambda self, web: "different" if outcome == "different_scope" else "scope")
    events = []

    def refresh(*args, **kwargs):
        events.append("refresh")
        if outcome == "rejected":
            raise AuthenticationExpiredError("Rejected", http_status=400)
        if outcome == "network":
            raise MobileFlowError("Network failed")

    monkeypatch.setattr(flow, "_refresh", refresh)
    monkeypatch.setattr(flow, "revoke", lambda: events.append("revoke"))
    monkeypatch.setattr(flow, "pair", lambda: events.append("pair"))
    try:
        if outcome in {"network", "different_scope"}:
            with pytest.raises(MobileFlowError):
                flow.complete_login()
        else:
            flow.complete_login()
        assert events == expected
        assert flow.tokens.load().binding == "old-binding"  # No blind overwrite/clear.
    finally:
        flow.close()


@pytest.mark.parametrize("callback", [
    "https://evil.example/?code=secret",
    "InfomentorNotification://wrong?code=secret",
    "InfomentorNotification://oauth2Callback?code=one&code=two",
])
def test_authorization_rejects_unexpected_callbacks_without_token_exchange(httpx_mock, callback):
    store = prepare_pairing(httpx_mock, callback=callback)
    flow = MobileAuthorization(store)
    try:
        with pytest.raises(MobileFlowError, match="code callback"):
            flow.pair()
        assert flow.tokens.load() is None
        assert not any(request.url.path == TOKEN_PATH for request in httpx_mock.get_requests())
    finally:
        flow.close()


def test_tokens_retained_for_revocation_when_sso_url_is_unapproved(httpx_mock):
    store = prepare_pairing(httpx_mock, sign_in_origin="https://evil.example")
    flow = MobileAuthorization(store)
    try:
        with pytest.raises(MobileFlowError, match="not approved"):
            flow.pair()
        credentials = flow.tokens.load()
        assert credentials is not None and not credentials.verified
        assert not any(request.url.host == "evil.example" for request in httpx_mock.get_requests())
    finally:
        flow.close()


def test_refresh_rotation_saved_before_later_sso_failure(httpx_mock):
    store = SessionStore()
    store.save(AuthSession(mobile_binding="synthetic-binding"))
    tokens = MobileCredentialStore(store)
    tokens.save(MobileCredentials(
        access_token="old-access", refresh_token="old-refresh", device_id="synthetic-device",
        binding="synthetic-binding", children_fingerprint="synthetic-fingerprint",
    ))
    httpx_mock.add_response(url=AUTH_ORIGIN + TOKEN_PATH, json={
        "access_token": "rotated-access", "refresh_token": "rotated-refresh", "expires_in": 3600,
    })
    httpx_mock.add_response(url=API_ORIGIN + "/NA1/Authentication/sso", status_code=503)
    flow = MobileAuthorization(store)
    try:
        with pytest.raises(MobileFlowError, match="HTTP 503"):
            flow.test(refresh=True)
        assert tokens.load().refresh_token.get_secret_value() == "rotated-refresh"
        assert not tokens.load().verified
    finally:
        flow.close()


def test_mobile_tokens_cannot_replace_a_differently_bound_web_session(httpx_mock):
    store = SessionStore()
    store.save(AuthSession(mobile_binding="new-login"))
    MobileCredentialStore(store).save(MobileCredentials(
        access_token="old-access", refresh_token="old-refresh", device_id="device",
        binding="different-login", children_fingerprint="fingerprint",
    ))
    flow = MobileAuthorization(store)
    try:
        with pytest.raises(MobileFlowError, match="not bound"):
            flow.test(refresh=True)
        assert not httpx_mock.get_requests()
    finally:
        flow.close()


def test_expiry_test_requires_401_before_refresh(httpx_mock, monkeypatch):
    monkeypatch.setattr("infomentor_mcp.auth.mobile.time.sleep", lambda _: None)
    store = SessionStore()
    store.save(AuthSession(mobile_binding="binding"))
    tokens = MobileCredentialStore(store)
    tokens.save(MobileCredentials(
        access_token="expired-access", refresh_token="refresh", device_id="device",
        binding="binding", children_fingerprint="fingerprint", expires_in=600,
    ))
    httpx_mock.add_response(url=API_ORIGIN + "/NA1/Authentication/sso", status_code=401)
    httpx_mock.add_response(url=AUTH_ORIGIN + TOKEN_PATH, json={
        "access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 600,
    })
    restored = []
    flow = MobileAuthorization(store)
    monkeypatch.setattr(flow, "_restore_hub", lambda credentials: restored.append(credentials))
    try:
        flow.test(after_expiry=True)
        assert tokens.load().access_token.get_secret_value() == "new-access"
        assert len(restored) == 1
    finally:
        flow.close()


def test_still_valid_access_token_is_not_reported_as_expiry_refresh(httpx_mock, monkeypatch):
    monkeypatch.setattr("infomentor_mcp.auth.mobile.time.sleep", lambda _: None)
    store = SessionStore()
    store.save(AuthSession(mobile_binding="binding"))
    MobileCredentialStore(store).save(MobileCredentials(
        access_token="access", refresh_token="refresh", device_id="device",
        binding="binding", children_fingerprint="fingerprint", expires_in=600,
    ))
    httpx_mock.add_response(url=API_ORIGIN + "/NA1/Authentication/sso", status_code=200)
    flow = MobileAuthorization(store)
    try:
        with pytest.raises(MobileFlowError, match="inconclusive"):
            flow.test(after_expiry=True)
        assert len(httpx_mock.get_requests()) == 1
    finally:
        flow.close()


def test_wrong_child_scope_does_not_replace_existing_session(httpx_mock):
    store = SessionStore()
    store.save(AuthSession(mobile_binding="binding"))
    tokens = MobileCredentialStore(store)
    tokens.save(MobileCredentials(
        access_token="access", refresh_token="refresh", device_id="device",
        binding="binding", children_fingerprint="different-scope",
    ))
    httpx_mock.add_response(
        url=API_ORIGIN + "/NA1/Authentication/sso", json=HUB_ORIGIN + "/"
    )
    httpx_mock.add_response(url=HUB_ORIGIN + "/", text=PAGE, is_reusable=True)
    httpx_mock.add_response(
        url=HUB_ORIGIN + "/timetable/timetable/appData", json={"items": []}
    )
    before = store.load().model_dump_json()
    flow = MobileAuthorization(store)
    try:
        with pytest.raises(MobileFlowError, match="different linked-child scope"):
            flow.test()
        assert store.load().model_dump_json() == before
        assert tokens.load() is not None
    finally:
        flow.close()


def test_keyring_entries_for_web_and_mobile_are_separate(monkeypatch):
    entries = {}
    monkeypatch.setattr("keyring.set_password", lambda service, account, text: entries.update(
        {(service, account): text}
    ))
    monkeypatch.setattr("keyring.get_password", lambda service, account: entries.get(
        (service, account)
    ))
    store = SessionStore()
    store.save(AuthSession(mobile_binding="binding"))
    tokens = MobileCredentialStore(store)
    tokens.save(MobileCredentials(
        access_token="access", refresh_token="refresh", device_id="device",
        binding="binding", children_fingerprint="fingerprint",
    ))
    assert len(entries) == 2
    assert store.load().mobile_binding == "binding"
    assert tokens.load().access_token.get_secret_value() == "access"
    assert not tokens.path.exists()


def test_revocation_preserves_unexpectedly_rotated_tokens(httpx_mock):
    store = SessionStore()
    tokens = MobileCredentialStore(store)
    tokens.save(MobileCredentials(
        access_token="access", refresh_token="refresh", device_id="device",
        binding="binding", children_fingerprint="fingerprint",
    ))
    httpx_mock.add_response(
        url=re.compile(re.escape(AUTH_ORIGIN + REVOKE_PATH) + r"\?.*"), json=True
    )
    httpx_mock.add_response(url=AUTH_ORIGIN + TOKEN_PATH, json={
        "access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 600,
    })
    flow = MobileAuthorization(store)
    try:
        with pytest.raises(MobileFlowError, match="still renewed"):
            flow.revoke()
        assert tokens.load().refresh_token.get_secret_value() == "new-refresh"
    finally:
        flow.close()


@pytest.mark.parametrize("status,body,removed", [
    (200, "true", True), (204, "", True), (200, "false", False),
    (302, "", False), (401, "", True), (400, "", True),
    (403, "", False), (200, "<html>secret</html>", False),
])
def test_revocation_only_clears_local_credentials_on_confirmation(
    httpx_mock, status, body, removed
):
    store = SessionStore()
    tokens = MobileCredentialStore(store)
    tokens.save(MobileCredentials(
        access_token="access", refresh_token="refresh", device_id="device",
        binding="binding", children_fingerprint="fingerprint",
    ))
    httpx_mock.add_response(
        url=re.compile(re.escape(AUTH_ORIGIN + REVOKE_PATH) + r"\?.*"),
        status_code=status, text=body,
    )
    if removed:
        httpx_mock.add_response(
            url=AUTH_ORIGIN + TOKEN_PATH, status_code=400,
            text="Bad Request" if status == 400 else '{"error":"invalid_grant"}',
        )
    flow = MobileAuthorization(store)
    try:
        if removed:
            flow.revoke()
        else:
            with pytest.raises(MobileFlowError):
                flow.revoke()
        assert (tokens.load() is None) == removed
        request = httpx_mock.get_requests()[0]
        assert request.url.path == REVOKE_PATH
        assert request.headers["authorization"] == "Bearer access"
        assert request.url.params["refreshToken"] == "refresh"
    finally:
        flow.close()
