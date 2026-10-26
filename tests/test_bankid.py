import asyncio

import pytest

from infomentor_mcp.auth.bankid import BankIdOrder, StockholmBankId
from infomentor_mcp.auth.saml import SSO_LOGIN_URL, LoginPage
from infomentor_mcp.errors import InfoMentorError, UnsafeRedirectError

GATEWAY = "https://login003.stockholm.se/auth"


def mock_begin(httpx_mock, payload, method="url"):
    httpx_mock.add_response(url=SSO_LOGIN_URL, status_code=302, headers={"location": GATEWAY})
    httpx_mock.add_response(url=GATEWAY, text="<html>BankID</html>")
    initialize = "bankid" if method == "url" else "qr"
    httpx_mock.add_response(url=f"{GATEWAY}?initialize={initialize}", json=payload)


def test_parses_interstitial_form_without_saml_fields() -> None:
    page = LoginPage(
        '<html><body>Login in progress<form action="/continue">'
        '<input name="relay" value="value"></form></body></html>'
    )

    assert page.forms[0].action == "/continue"
    assert page.forms[0].fields == {"relay": "value"}


def test_url_order_is_encoded_and_core_never_prints_secrets(httpx_mock, capsys):
    mock_begin(httpx_mock, {"order": "synthetic-order", "token": "synthetic+token&value"})

    async def scenario():
        protocol = StockholmBankId()
        try:
            order = await protocol.begin("url")
            assert order.launch_url.endswith(
                "autostarttoken=synthetic%2Btoken%26value&redirect=null"
            )
            assert "synthetic" not in repr(order)
            assert "synthetic" not in str(protocol.diagnostics)
        finally:
            await protocol.close()

    asyncio.run(scenario())
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("payload", [{}, {"order": "order", "token": None}, []])
def test_invalid_url_order_does_not_print_secrets(httpx_mock, capsys, payload) -> None:
    mock_begin(httpx_mock, payload)

    async def scenario():
        protocol = StockholmBankId()
        try:
            with pytest.raises(InfoMentorError, match="initialization"):
                await protocol.begin("url")
        finally:
            await protocol.close()

    asyncio.run(scenario())
    assert capsys.readouterr().out == ""


def test_polling_is_async_and_has_no_presentation(httpx_mock, capsys):
    httpx_mock.add_response(json={"state": "OK", "qrData": "synthetic-qr-secret"})

    async def scenario():
        protocol = StockholmBankId()
        try:
            status = await protocol.poll(BankIdOrder(GATEWAY, "synthetic-order"))
            assert status.state == "OK"
            assert "secret" not in repr(status)
        finally:
            await protocol.close()

    asyncio.run(scenario())
    assert capsys.readouterr().out == ""


def test_async_auth_rejects_external_redirect_before_contact(httpx_mock):
    httpx_mock.add_response(
        url=SSO_LOGIN_URL, status_code=302, headers={"location": "https://evil.example/secret"}
    )

    async def scenario():
        protocol = StockholmBankId()
        try:
            with pytest.raises(UnsafeRedirectError):
                await protocol.begin("url")
        finally:
            await protocol.close()

    asyncio.run(scenario())
    assert len(httpx_mock.get_requests()) == 1


def test_async_saml_follows_interstitial_and_verifies_before_returning_cookies(httpx_mock):
    httpx_mock.add_response(
        url=GATEWAY,
        text='<form action="https://sso.infomentor.se/login.ashx">'
        '<input name="SAMLResponse" value="secret-saml"></form>',
    )
    httpx_mock.add_response(
        url="https://sso.infomentor.se/login.ashx", method="POST", status_code=302,
        headers={"location": "https://hub.infomentor.se/authentication/authentication/login"},
    )
    httpx_mock.add_response(
        url="https://hub.infomentor.se/authentication/authentication/login",
        text='Login in progress<form action="https://infomentor.se/swedish/production/mentor/">'
        '<input name="oauth_token" value="secret-openid"></form>',
    )
    httpx_mock.add_response(
        url="https://infomentor.se/swedish/production/mentor/", method="POST", status_code=302,
        headers={"location": "https://hub.infomentor.se/Authentication/Authentication/LoginCallback"},
    )
    httpx_mock.add_response(
        url="https://hub.infomentor.se/Authentication/Authentication/LoginCallback",
        status_code=302,
        headers={"location": "/", "set-cookie": "IMHome=synthetic-cookie; Path=/; Secure"},
    )
    httpx_mock.add_response(url="https://hub.infomentor.se/", text="<html>Hub</html>")
    httpx_mock.add_response(
        url="https://hub.infomentor.se/timetable/timetable/appData", json={"items": []}
    )

    async def scenario():
        protocol = StockholmBankId()
        try:
            session = await protocol.finish(BankIdOrder(GATEWAY, "synthetic-order"))
            assert any(cookie.name == "IMHome" for cookie in session.cookies)
            assert "secret" not in str(protocol.diagnostics)
            assert "synthetic-cookie" not in str(protocol.diagnostics)
        finally:
            await protocol.close()

    asyncio.run(scenario())


def test_async_saml_refuses_unverified_session(httpx_mock):
    httpx_mock.add_response(url=GATEWAY, text="<html>Finished</html>")
    httpx_mock.add_response(
        url="https://hub.infomentor.se/timetable/timetable/appData", json={},
    )

    async def scenario():
        protocol = StockholmBankId()
        try:
            with pytest.raises(InfoMentorError, match="verification"):
                await protocol.finish(BankIdOrder(GATEWAY, "synthetic-order"))
        finally:
            await protocol.close()

    asyncio.run(scenario())
