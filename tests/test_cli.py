from contextlib import nullcontext

import pytest

import infomentor_mcp.cli as cli


class EmptySessionStore:
    def locked(self):
        return nullcontext()

    def load(self):
        return None

    def clear(self) -> None:
        pass


def test_auth_status_is_available_before_login(capsys, monkeypatch) -> None:
    monkeypatch.setattr(cli, "SessionStore", EmptySessionStore)
    assert cli.main(["auth", "status"]) == 0
    assert "No authenticated session" in capsys.readouterr().out


def test_auth_status_accepts_verify_flag(capsys, monkeypatch) -> None:
    monkeypatch.setattr(cli, "SessionStore", EmptySessionStore)
    assert cli.main(["auth", "status", "--verify"]) == 0
    assert "No authenticated session" in capsys.readouterr().out


def test_pending_command_returns_nonzero(capsys) -> None:
    assert cli.main(["sync"]) == 2
    assert "sync command" in capsys.readouterr().out


def test_measurement_cli_accepts_idle_mode_without_starting_login(monkeypatch):
    received = []
    monkeypatch.setattr(cli, "_run_measurement", lambda args: received.append(args) or 0)
    assert cli.main(["auth", "measure", "--idle-minutes", "60", "--method", "url"]) == 0
    assert received[0].idle_minutes == 60
    assert received[0].method == "url"


def test_invalid_measurement_options_fail_before_login():
    with pytest.raises(SystemExit) as error:
        cli.main(["auth", "measure", "--duration-minutes", "10", "--idle-minutes", "20"])
    assert error.value.code == 2


def test_cli_async_login_saves_verified_session(monkeypatch, capsys):
    from infomentor_mcp.auth.session import AuthSession, SessionStore

    async def fake_login():
        return AuthSession()

    monkeypatch.setattr(cli.url, "login", fake_login)
    assert cli.main(["auth", "login", "--method", "url"]) == 0
    assert SessionStore().load() is not None
    assert "local session was saved" in capsys.readouterr().out
