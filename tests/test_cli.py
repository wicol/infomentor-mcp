import json
from contextlib import nullcontext

import pytest

from infomentor_mcp.app.cli import Cli
from infomentor_mcp.login.terminal import TerminalLogin
from infomentor_mcp.storage.session import AuthSession, SessionStore


class EmptySessionStore:
    def __init__(self, state_dir):
        self.state_dir = state_dir

    def locked(self, timeout=30):
        return nullcontext()

    def load(self):
        return None

    def clear(self) -> None:
        pass


@pytest.mark.parametrize("args", [["auth", "status"], ["auth", "status", "--verify"]])
def test_auth_status_is_available_before_login(capsys, tmp_path, args) -> None:
    assert Cli(EmptySessionStore(tmp_path)).run(args) == 0
    assert "No authenticated session" in capsys.readouterr().out


def test_sync_without_login_returns_json(capsys, tmp_path, httpx_mock) -> None:
    assert Cli(EmptySessionStore(tmp_path)).run(["sync"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "authentication_required"
    assert not httpx_mock.get_requests()


def test_measurement_cli_accepts_idle_mode():
    args = Cli().parser.parse_args(["auth", "measure", "--idle-minutes", "60", "--method", "url"])
    assert args.idle_minutes == 60
    assert args.method == "url"


def test_invalid_measurement_options_fail_before_login(monkeypatch):
    monkeypatch.setattr(TerminalLogin, "run", lambda self: pytest.fail("Login started"))
    with pytest.raises(SystemExit) as error:
        Cli().run(["auth", "measure", "--duration-minutes", "10", "--idle-minutes", "20"])
    assert error.value.code == 2


def test_cli_async_login_saves_verified_session(monkeypatch, capsys, completed_mobile_login):
    async def fake_login(self):
        return AuthSession()

    monkeypatch.setattr(TerminalLogin, "run", fake_login)
    assert Cli().run(["auth", "login", "--method", "url"]) == 0
    assert SessionStore().load().mobile_binding == "synthetic-bound-login"
    assert "bound for mobile renewal" in capsys.readouterr().out
