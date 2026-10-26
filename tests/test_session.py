import asyncio
from pathlib import Path
from time import time

import httpx
import pytest

from infomentor_mcp.storage.session import AuthSession, SessionStore, StoredCookie


def test_session_survives_client_recreation(tmp_path: Path) -> None:
    initial_client = httpx.Client()
    initial_client.cookies.set("IMHome", "secret", domain="hub.infomentor.se", path="/")
    store = SessionStore(tmp_path / "session.json")
    store.save(AuthSession.from_client(initial_client))

    restored = store.load()
    restored_client = httpx.Client()
    assert restored is not None
    restored.apply_to(restored_client)

    assert restored_client.cookies.get("IMHome", domain="hub.infomentor.se", path="/") == "secret"
    assert (tmp_path / "session.json").stat().st_mode & 0o777 == 0o600


def test_stockholm_sso_probe_excludes_infomentor_cookies() -> None:
    client = httpx.Client()
    client.cookies.set("SMSESSION", "sso", domain="login001.stockholm.se", path="/")
    client.cookies.set("IMHome", "hub", domain="hub.infomentor.se", path="/")

    sso_session = AuthSession.from_client(client).stockholm_sso_only()

    assert [cookie.name for cookie in sso_session.cookies] == ["SMSESSION"]


@pytest.mark.parametrize("client_type", [httpx.Client, httpx.AsyncClient])
@pytest.mark.parametrize("secure,expiry", [(True, "future"), (True, "expired"), (False, "session")])
def test_restored_cookie_preserves_security_expiry_and_request_scope(
    tmp_path, client_type, secure, expiry,
):
    expires = {"future": int(time()) + 3600, "expired": 1, "session": None}[expiry]
    session = AuthSession(cookies=[StoredCookie(
        name="synthetic", value="synthetic-value", domain="example.invalid", path="/school",
        secure=secure, expires=expires,
    )])
    store = SessionStore(tmp_path / "session.json")
    store.save(session)
    restored = store.load()
    client = client_type()
    try:
        restored.apply_to(client)
        assert AuthSession.from_client(client).cookies == session.cookies
        http = client.build_request("GET", "http://example.invalid/school/")
        https = client.build_request("GET", "https://example.invalid/school/")
        assert ("cookie" in http.headers) == (not secure and expiry != "expired")
        assert ("cookie" in https.headers) == (expiry != "expired")
        for url in ("https://other.invalid/school/", "https://example.invalid/other/"):
            assert "cookie" not in client.build_request("GET", url).headers
    finally:
        if isinstance(client, httpx.AsyncClient):
            asyncio.run(client.aclose())
        else:
            client.close()
