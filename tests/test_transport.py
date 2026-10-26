import pytest

from infomentor_mcp.errors import AuthenticationExpiredError, InfoMentorError, UnsafeRedirectError
from infomentor_mcp.hub.transport import HubTransport, validate_auth_url


def test_access_denial_does_not_count_as_expiry(httpx_mock):
    httpx_mock.add_response(status_code=403)
    client = HubTransport()
    try:
        with pytest.raises(InfoMentorError) as error:
            client.post("/timetable/timetable/appData", {})
        assert not isinstance(error.value, AuthenticationExpiredError)
    finally:
        client.close()


@pytest.mark.parametrize("payload", [True, {}, {"items": "unexpected"}])
def test_probe_requires_actual_timetable_structure(httpx_mock, payload):
    httpx_mock.add_response(json=payload)
    client = HubTransport()
    try:
        with pytest.raises(InfoMentorError, match="inconclusive"):
            client.verify_authenticated_read()
    finally:
        client.close()


def test_rejects_auth_redirect_outside_approved_hosts() -> None:
    with pytest.raises(UnsafeRedirectError, match="example.invalid"):
        validate_auth_url("https://example.invalid/login")


def test_allows_infomentor_apex_domain() -> None:
    assert validate_auth_url("https://infomentor.se/") == "https://infomentor.se/"


def test_empty_hub_response_is_expired_session(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://hub.infomentor.se/Communication/News/GetNewsList",
        status_code=200,
        content=b"",
    )
    client = HubTransport()
    with pytest.raises(AuthenticationExpiredError):
        client.post("/Communication/News/GetNewsList", {})
    client.close()


def test_hub_login_redirect_is_expired_session(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://hub.infomentor.se/Communication/News/GetNewsList",
        status_code=302,
        headers={"location": "https://sso.infomentor.se/login.ashx"},
    )
    client = HubTransport()
    with pytest.raises(
        AuthenticationExpiredError, match="host: sso.infomentor.se, path: /login.ashx"
    ):
        client.post("/Communication/News/GetNewsList", {})
    client.close()
