import pytest

from infomentor_mcp.auth.session import AuthSession
from infomentor_mcp.client.http import AuthenticationExpiredError
from infomentor_mcp.client.infomentor import InfoMentorClient


def test_retries_a_read_once_after_session_renewal(monkeypatch) -> None:
    client = InfoMentorClient(AuthSession())
    attempts = 0
    renewals = 0

    def fail_once() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise AuthenticationExpiredError("synthetic expiry")
        return "retried successfully"

    def renew() -> None:
        nonlocal renewals
        renewals += 1

    monkeypatch.setattr(client, "_renew_session", renew)

    assert client._with_session_renewal(fail_once) == "retried successfully"
    assert attempts == 2
    assert renewals == 1


def test_expired_session_without_persistence_requires_login() -> None:
    client = InfoMentorClient(AuthSession())

    with pytest.raises(AuthenticationExpiredError, match="run auth login"):
        client._renew_session()


def test_replayed_read_is_not_renewed_again(monkeypatch):
    client = InfoMentorClient(AuthSession())
    calls = []

    def expired():
        calls.append("read")
        raise AuthenticationExpiredError("synthetic expiry")

    monkeypatch.setattr(client, "_renew_session", lambda: calls.append("renew"))
    try:
        with pytest.raises(AuthenticationExpiredError):
            client._with_session_renewal(expired)
        assert calls == ["read", "renew", "read"]
    finally:
        client.close()
