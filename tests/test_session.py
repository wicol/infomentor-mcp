from pathlib import Path

import httpx

from infomentor_mcp.auth.session import AuthSession, SessionStore


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
