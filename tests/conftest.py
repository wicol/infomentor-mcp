"""Keep all tests away from real sessions, OS keyrings, and school records."""

import keyring.errors
import pytest
from fakehub import FakeHub

from infomentor_mcp.hub.hub import Hub
from infomentor_mcp.storage.session import AuthSession, SessionStore


@pytest.fixture(autouse=True)
def isolated_session_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    def unavailable(*args, **kwargs):
        raise keyring.errors.NoKeyringError("Synthetic test backend unavailable")

    monkeypatch.setattr("keyring.get_password", unavailable)
    monkeypatch.setattr("keyring.set_password", unavailable)
    monkeypatch.setattr("keyring.delete_password", unavailable)


@pytest.fixture
def completed_mobile_login(monkeypatch):
    def complete(authorization):
        session = authorization.store.load()
        session.mobile_binding = "synthetic-bound-login"
        authorization.store.save(session)

    monkeypatch.setattr(
        "infomentor_mcp.auth.mobile.MobileAuthorization.complete_login", complete,
    )


@pytest.fixture
def fake_hub(httpx_mock):
    return FakeHub(httpx_mock)


@pytest.fixture
def store():
    store = SessionStore()
    store.save(AuthSession())
    return store


@pytest.fixture
def hub(fake_hub, store):
    """A Hub on the simulated InfoMentor, without session renewal."""
    with Hub(store) as hub:
        yield hub
