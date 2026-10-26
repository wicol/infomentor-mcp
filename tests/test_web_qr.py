import asyncio
from urllib.parse import urlsplit

import httpx
import pytest

from infomentor_mcp.login.web_qr import WebQr, WebQrOptions


def test_loopback_view_has_no_external_resources_or_public_frames(capsys):
    async def scenario():
        view = WebQr(WebQrOptions())
        await view.start()
        try:
            view.show("synthetic-qr-secret", "waiting")
            async with httpx.AsyncClient(trust_env=False) as browser:
                page = await browser.get(view.url)
                assert page.status_code == 200
                assert "script-src 'nonce-" in page.headers["content-security-policy"]
                assert page.headers["referrer-policy"] == "no-referrer"
                assert page.headers["cache-control"] == "no-store"
                assert "https://" not in page.text
                assert "synthetic-qr-secret" not in page.text
                assert (await browser.get(view.origin + "/frame")).status_code == 404
                assert (await browser.get(view.url + "frame")).json()["matrix"]
                denied = await browser.get(view.url, headers={"Host": "evil.example"})
                assert denied.status_code == 404
                assert (await browser.get(
                    view.url + "frame", headers={"Origin": "https://evil.example"}
                )).status_code == 404
                assert (await browser.post(view.url)).status_code == 404
        finally:
            await view.close()

    asyncio.run(scenario())
    assert capsys.readouterr().out == ""


def test_lan_bind_requires_a_separate_visitable_origin():
    async def scenario():
        invalid = WebQr(WebQrOptions(host="0.0.0.0"))
        with pytest.raises(ValueError, match="public-url"):
            await invalid.start()
        view = WebQr(WebQrOptions(host="0.0.0.0", public_url="http://qr-host.test"))
        await view.start()
        try:
            assert view.url.startswith("http://qr-host.test:")
            port = urlsplit(view.url).port
            async with httpx.AsyncClient(trust_env=False) as browser:
                response = await browser.get(
                    f"http://127.0.0.1:{port}{view.path}", headers={"Host": f"qr-host.test:{port}"}
                )
                assert response.status_code == 200
        finally:
            await view.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("origin", [
    "http://user:secret@qr-host.test", "http://0.0.0.0",
])
def test_invalid_advertised_addresses_are_rejected_before_binding(origin):
    async def scenario():
        view = WebQr(WebQrOptions(public_url=origin))
        with pytest.raises(ValueError):
            await view.start()
        assert view.server is None

    asyncio.run(scenario())
