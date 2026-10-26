import json

import pytest

from infomentor_mcp.app.cli import main
from infomentor_mcp.auth.discovery import DISCOVERY_PATH, EndpointMetadata, MobileDiscovery
from infomentor_mcp.errors import MobileDiscoveryError
from infomentor_mcp.hub.transport import HUB_ORIGIN, HubTransport
from infomentor_mcp.storage.session import AuthSession, SessionStore, StoredCookie

SYNTHETIC_PAYLOAD = {
    "apiUrl": "https://api.infomentor.se/NA1/",
    "authenticationUrl": (
        "https://auth.infomentor.se/Authentication/Authentication/LoginOAuth2?authGuid=secret-guid"
    ),
    "tokenUrl": "https://auth.infomentor.se/Authentication/OAuth2/Token",
    "revokeRefreshTokenUrl": "https://auth.infomentor.se/opaque-secret-path?token=secret-token",
    "clientId": "secret-client-id",
    "expirationTime": "secret-expiration-value",
    "secret-key-name": {"access_token": "secret-access-token"},
}


def test_discovery_projects_only_metadata_and_never_contacts_returned_urls(httpx_mock):
    httpx_mock.add_response(url=HUB_ORIGIN + DISCOVERY_PATH, json=SYNTHETIC_PAYLOAD)
    http = HubTransport()
    try:
        report = MobileDiscovery(http).run()
    finally:
        http.close()
    serialized = report.model_dump_json()
    assert "secret" not in serialized
    assert report.complete_expected_shape
    assert report.unknown_field_count == 1
    assert report.endpoints["authenticationUrl"].query_present
    assert report.endpoints["revokeRefreshTokenUrl"].path_redacted
    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].content == b""
    assert str(requests[0].url) == HUB_ORIGIN + DISCOVERY_PATH


@pytest.mark.parametrize("url", [
    "http://api.infomentor.se/",
    "https://infomentor.se.example.org/?secret=1",
    "https://api.infomentor.is/",
    "https://secret-user:secret-password@api.infomentor.se/",
    "https://api.infomentor.se:8443/",
    "https://api.infomentor.se/#secret-fragment",
    "https://bad!.infomentor.se/",
    "https://api.infomentor.se/\nsecret",
    "https://api.infomentor.se\\@example.org/",
    "https://api.infomentor.se:bad/",
    None,
])
def test_unapproved_addresses_are_not_echoed_or_accepted(url):
    metadata = EndpointMetadata.inspect(url)
    assert not metadata.valid_first_party_https
    assert metadata.origin is None
    assert "secret" not in metadata.model_dump_json()


@pytest.mark.parametrize("status", [302, 401, 403, 404, 429, 500])
def test_failed_discovery_does_not_follow_redirects_retry_or_echo_payload(httpx_mock, status):
    httpx_mock.add_response(
        url=HUB_ORIGIN + DISCOVERY_PATH,
        status_code=status,
        headers={"location": "https://unapproved.example/secret-token"},
        text="secret-response",
    )
    http = HubTransport()
    try:
        with pytest.raises(MobileDiscoveryError) as error:
            MobileDiscovery(http).run()
        assert "secret" not in str(error.value)
        assert error.value.http_status == status
        assert len(httpx_mock.get_requests()) == 1
    finally:
        http.close()


@pytest.mark.parametrize("body", ["", "<html>secret-login</html>", "null", "[]", '"secret"'])
def test_unexpected_discovery_bodies_are_not_echoed(httpx_mock, body):
    httpx_mock.add_response(text=body)
    http = HubTransport()
    try:
        with pytest.raises(MobileDiscoveryError) as error:
            MobileDiscovery(http).run()
        assert "secret" not in str(error.value)
    finally:
        http.close()


def test_partial_object_is_reported_without_claiming_support(httpx_mock):
    httpx_mock.add_response(json={"clientId": "secret-client-id"})
    http = HubTransport()
    try:
        report = MobileDiscovery(http).run()
        assert not report.complete_expected_shape
        assert "apiUrl" in report.missing_fields
        assert "secret" not in report.model_dump_json()
    finally:
        http.close()


def test_cli_requires_existing_session_without_starting_login(httpx_mock, capsys):
    assert main(["auth", "mobile-discover"]) == 1
    assert "No saved session" in capsys.readouterr().out
    assert not httpx_mock.get_requests()


def test_cli_checks_hub_first_and_preserves_only_rotated_cookies(httpx_mock, capsys):
    store = SessionStore()
    store.save(AuthSession(cookies=[
        StoredCookie(name="IMHome", value="old-cookie", domain="hub.infomentor.se")
    ]))
    httpx_mock.add_response(
        method="POST", url=HUB_ORIGIN + "/timetable/timetable/appData", json={"items": []}
    )
    httpx_mock.add_response(
        method="POST", url=HUB_ORIGIN + DISCOVERY_PATH, json=SYNTHETIC_PAYLOAD,
        headers={"set-cookie": "IMHome=rotated-cookie; Path=/; Secure"},
    )
    assert main(["auth", "mobile-discover"]) == 0
    output = capsys.readouterr().out
    assert "secret" not in output
    assert "cookie" not in output
    assert "does not establish Stockholm" in output
    stored = store.load()
    assert any(cookie.value == "rotated-cookie" for cookie in stored.cookies)
    assert "secret-guid" not in stored.model_dump_json()
    assert len(httpx_mock.get_requests()) == 2
    # The command never persists or echoes the discovery JSON itself.
    assert "authenticationUrl" not in json.dumps(stored.model_dump(mode="json"))


def test_cli_does_not_probe_discovery_on_expired_session(httpx_mock, capsys):
    SessionStore().save(AuthSession())
    httpx_mock.add_response(
        status_code=302,
        headers={"location": "https://hub.infomentor.se/secret-path?token=secret-token"},
    )
    assert main(["auth", "mobile-discover"]) == 1
    output = capsys.readouterr().out
    assert "authentication_required" in output
    assert "secret" not in output
    assert len(httpx_mock.get_requests()) == 1
