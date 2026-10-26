import hashlib
import json

import pytest

from infomentor_mcp.auth.mobile import API_ORIGIN, AUTH_ORIGIN, TOKEN_PATH
from infomentor_mcp.auth.renewal import Renewal
from infomentor_mcp.errors import AuthenticationExpiredError, InfoMentorError
from infomentor_mcp.features.news import News
from infomentor_mcp.hub.hub import Hub
from infomentor_mcp.hub.transport import HUB_ORIGIN
from infomentor_mcp.storage.credentials import MobileCredentials, MobileCredentialStore
from infomentor_mcp.storage.session import AuthSession, SessionStore, StoredCookie

PAGE = """
<script>IMHome.init = { selectedPupilName: 'One, Ada' };
var pupils = ['/Account/PupilSwitcher/SwitchPupil/first'];</script>
"""


def prepare_store():
    store = SessionStore()
    store.save(AuthSession(mobile_binding="binding", cookies=[
        StoredCookie(name="IMHome", value="expired-hub", domain="hub.infomentor.se")
    ]))
    tokens = MobileCredentialStore(store)
    tokens.save(MobileCredentials(
        access_token="old-access", refresh_token="old-refresh", device_id="device",
        binding="binding", verified=True,
        children_fingerprint=hashlib.sha256(json.dumps(["first"]).encode()).hexdigest(),
    ))
    return store, tokens


def add_initial_expiry(httpx_mock):
    httpx_mock.add_response(
        url=HUB_ORIGIN + "/", status_code=302,
        headers={"location": "/Authentication/Authentication/Login"},
    )


@pytest.mark.parametrize("access_expired", [False, True])
def test_expired_hub_and_access_refresh_then_replay_news_once(httpx_mock, capsys, access_expired):
    store, tokens = prepare_store()
    add_initial_expiry(httpx_mock)
    if access_expired:
        httpx_mock.add_response(url=API_ORIGIN + "/NA1/Authentication/sso", status_code=401)
        httpx_mock.add_response(url=AUTH_ORIGIN + TOKEN_PATH, json={
            "access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 600,
        })
    httpx_mock.add_response(
        url=API_ORIGIN + "/NA1/Authentication/sso", json=HUB_ORIGIN + "/signin?token=secret"
    )
    httpx_mock.add_response(
        url=HUB_ORIGIN + "/signin?token=secret", status_code=302,
        headers={"location": "/", "set-cookie": "IMHome=new-hub; Path=/; Secure"},
    )
    httpx_mock.add_response(url=HUB_ORIGIN + "/", text=PAGE, is_reusable=True)
    httpx_mock.add_response(
        url=HUB_ORIGIN + "/timetable/timetable/appData", json={"items": []}
    )
    httpx_mock.add_response(
        url=HUB_ORIGIN + "/Account/PupilSwitcher/SwitchPupil/first", text=PAGE, is_reusable=True
    )
    httpx_mock.add_response(
        url=HUB_ORIGIN + "/Communication/News/GetNewsList", json={"items": [{"id": "news"}]}
    )
    hub = Hub(store, Renewal(store))
    try:
        hub.refresh()
        original_client = hub.transport.client
        news = News(hub).items(["first"])
        assert hub.transport.client is not original_client
        assert original_client.is_closed
        assert news[0].child_ids == ["first"]
        expected_refresh = "new-refresh" if access_expired else "old-refresh"
        assert tokens.load().refresh_token.get_secret_value() == expected_refresh
        assert store.load().mobile_binding == "binding"
        assert any(cookie.value == "new-hub" for cookie in store.load().cookies)
        requests = httpx_mock.get_requests()
        assert sum(request.url.path == TOKEN_PATH for request in requests) == int(access_expired)
        assert sum(request.url.path.endswith("GetNewsList") for request in requests) == 1
        assert not any(request.url.host == "sso.infomentor.se" for request in requests)
        assert all(
            "authorization" not in request.headers
            for request in requests if request.url.host != "api-im.infomentor.se"
        )
        assert capsys.readouterr().out == ""
    finally:
        hub.close()


@pytest.mark.parametrize("status", [403, 429, 503])
def test_bridge_failures_do_not_trigger_refresh_or_other_login_flows(httpx_mock, status):
    store, _ = prepare_store()
    add_initial_expiry(httpx_mock)
    httpx_mock.add_response(url=API_ORIGIN + "/NA1/Authentication/sso", status_code=status)
    hub = Hub(store, Renewal(store))
    try:
        with pytest.raises(InfoMentorError):
            hub.run(hub.pupils.discover)
        assert len(httpx_mock.get_requests()) == 2
    finally:
        hub.close()


def test_failed_mobile_refresh_requires_explicit_auth_without_loop(httpx_mock):
    store, tokens = prepare_store()
    add_initial_expiry(httpx_mock)
    httpx_mock.add_response(url=API_ORIGIN + "/NA1/Authentication/sso", status_code=401)
    httpx_mock.add_response(url=AUTH_ORIGIN + TOKEN_PATH, status_code=400, text="Bad Request")
    hub = Hub(store, Renewal(store))
    try:
        with pytest.raises(AuthenticationExpiredError, match="explicit BankID"):
            hub.run(hub.pupils.discover)
        assert tokens.load().refresh_token.get_secret_value() == "old-refresh"
        assert len(httpx_mock.get_requests()) == 3
    finally:
        hub.close()


def test_no_retry_of_refresh_if_new_access_token_is_also_rejected(httpx_mock):
    store, tokens = prepare_store()
    add_initial_expiry(httpx_mock)
    httpx_mock.add_response(
        url=API_ORIGIN + "/NA1/Authentication/sso", status_code=401, is_reusable=True
    )
    httpx_mock.add_response(url=AUTH_ORIGIN + TOKEN_PATH, json={
        "access_token": "rotated-access", "refresh_token": "rotated-refresh",
    })
    hub = Hub(store, Renewal(store))
    try:
        with pytest.raises(AuthenticationExpiredError, match="after one refresh"):
            hub.run(hub.pupils.discover)
        assert tokens.load().refresh_token.get_secret_value() == "rotated-refresh"
        assert sum(request.url.path == TOKEN_PATH for request in httpx_mock.get_requests()) == 1
    finally:
        hub.close()


def test_fresh_explicit_login_does_not_use_a_previous_accounts_mobile_tokens(httpx_mock):
    store, _ = prepare_store()
    store.save(AuthSession())
    httpx_mock.add_response(url=HUB_ORIGIN + "/", text=PAGE)
    httpx_mock.add_response(
        url=HUB_ORIGIN + "/Account/PupilSwitcher/SwitchPupil/first", text=PAGE
    )
    hub = Hub(store, Renewal(store))
    try:
        assert len(hub.run(hub.pupils.discover)) == 1
        assert all(request.url.host == "hub.infomentor.se" for request in httpx_mock.get_requests())
    finally:
        hub.close()


def test_root_access_denial_does_not_start_mobile_renewal(httpx_mock):
    store, _ = prepare_store()
    httpx_mock.add_response(url=HUB_ORIGIN + "/", status_code=403)
    hub = Hub(store, Renewal(store))
    try:
        with pytest.raises(InfoMentorError, match="access denied"):
            hub.run(hub.pupils.discover)
        assert len(httpx_mock.get_requests()) == 1
    finally:
        hub.close()


@pytest.mark.parametrize("state", ["different_binding", "revocation_pending"])
def test_renewal_requires_matching_nonrevoked_pairing(httpx_mock, state):
    store, tokens = prepare_store()
    credentials = tokens.load()
    if state == "different_binding":
        credentials.binding = "another-login"
    else:
        credentials.revocation_acknowledged = True
    tokens.save(credentials)
    add_initial_expiry(httpx_mock)
    hub = Hub(store, Renewal(store))
    try:
        with pytest.raises(AuthenticationExpiredError):
            hub.run(hub.pupils.discover)
        assert len(httpx_mock.get_requests()) == 1
    finally:
        hub.close()
