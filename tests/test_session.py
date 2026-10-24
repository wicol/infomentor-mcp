import asyncio
from pathlib import Path
from time import time
from unittest.mock import patch

import httpx
import pytest

from infomentor_mcp.auth.session import AuthSession, SessionStore, StoredCookie


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


def test_read_reloads_latest_cookies_under_lock_and_persists_rotation(tmp_path):
    from infomentor_mcp.client.infomentor import InfoMentorClient

    store = SessionStore(tmp_path / "session.json")
    http = httpx.Client()
    http.cookies.set("IMHome", "old", domain="hub.infomentor.se")
    stale_session = AuthSession.from_client(http)
    client = InfoMentorClient(stale_session, session_store=store)
    http.cookies.set("IMHome", "latest", domain="hub.infomentor.se")
    store.save(AuthSession.from_client(http))

    def read_and_rotate():
        value = client._http.client.cookies.get("IMHome")
        client._http.client.cookies.set("IMHome", "rotated", domain="hub.infomentor.se")
        return value

    try:
        assert client._with_session_renewal(read_and_rotate) == "latest"
        saved = store.load()
        assert saved is not None
        assert saved.cookies[0].value == "rotated"
    finally:
        client.close()
        http.close()


def test_read_reloads_changed_cookie_attributes_even_when_value_is_unchanged(tmp_path):
    from infomentor_mcp.client.infomentor import InfoMentorClient

    session = AuthSession(cookies=[StoredCookie(
        name="synthetic", value="same-value", domain="example.invalid", secure=False,
    )])
    store = SessionStore(tmp_path / "session.json")
    store.save(session)
    client = InfoMentorClient(session, session_store=store)
    session.cookies[0].secure = True
    session.cookies[0].expires = int(time()) + 3600
    store.save(session)
    try:
        cookie = client._with_session_renewal(lambda: next(iter(client._http.client.cookies.jar)))
        assert cookie.secure is True and cookie.expires == session.cookies[0].expires
    finally:
        client.close()


def test_reads_still_report_session_save_failure(tmp_path):
    from infomentor_mcp.client.infomentor import InfoMentorClient

    store = SessionStore(tmp_path / "session.json")
    session = AuthSession()
    store.save(session)
    client = InfoMentorClient(session, session_store=store)
    try:
        with patch.object(store, "save", side_effect=OSError("synthetic failure")):
            with pytest.raises(OSError, match="synthetic failure"):
                client._with_session_renewal(lambda: "synthetic read result")
    finally:
        client.close()
