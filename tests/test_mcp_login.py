import asyncio

import pytest

from infomentor_mcp.auth.session import AuthSession, SessionStore
from infomentor_mcp.auth.stockholm_bankid import BankIdOrder, BankIdStatus
from infomentor_mcp.server import create_server

pytestmark = pytest.mark.usefixtures("completed_mobile_login")


class PendingProtocol:
    def __init__(self):
        self.stage = "bankid_wait"
        self.diagnostics = []
        self.closed = False
        self.approve = asyncio.Event()

    async def begin(self, method):
        return BankIdOrder(
            "https://login003.stockholm.se/auth", "secret-order",
            "https://app.bankid.com/?autostarttoken=synthetic-secret", "synthetic-qr",
        )

    async def poll(self, order):
        await self.approve.wait()
        return BankIdStatus("OK")

    async def finish(self, order):
        return AuthSession()

    async def close(self):
        self.closed = True


def test_mcp_start_progress_cancel_and_busy_calls_are_responsive(monkeypatch):
    async def scenario():
        protocol = PendingProtocol()
        monkeypatch.setattr("infomentor_mcp.login.attempt.StockholmBankId", lambda: protocol)
        server = create_server()
        started = (await server.call_tool("auth_start", {})).structured_content
        login_id = started["login_id"]
        assert started["login_url"].startswith("https://app.bankid.com/")
        result = await server.call_tool("auth_progress", {"login_id": login_id})
        progress = result.structured_content
        assert progress["status"] == "waiting"
        assert "synthetic-secret" not in str(progress)
        busy = (await server.call_tool("auth_start", {})).structured_content
        assert busy["error"]["code"] == "session_busy"
        data = await asyncio.wait_for(server.call_tool("list_children", {}), 2)
        assert data.structured_content["error"]["code"] == "session_busy"
        status = (await server.call_tool("auth_status", {})).structured_content
        assert status["login_in_progress"]
        waiter = asyncio.create_task(server.call_tool("auth_wait", {"login_id": login_id}))
        await asyncio.sleep(0)
        assert not waiter.done()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not protocol.closed
        waiter = asyncio.create_task(server.call_tool("auth_wait", {"login_id": login_id}))
        result = await server.call_tool("auth_cancel", {"login_id": login_id})
        cancelled = result.structured_content
        assert cancelled["status"] == "cancelled"
        assert (await asyncio.wait_for(waiter, 2)).structured_content == cancelled
        assert protocol.closed
        with SessionStore().locked(timeout=0):
            pass

    asyncio.run(scenario())


@pytest.mark.parametrize("tool", ["auth_progress", "auth_wait"])
def test_unknown_login_ids_do_not_expose_another_login(tool):
    server = create_server()
    result = asyncio.run(server.call_tool(tool, {"login_id": "unknown-secret"}))
    assert result.structured_content["error"]["code"] == "unknown_login"
    assert "unknown-secret" not in str(result)


def test_wait_returns_verified_completion_without_secrets(monkeypatch):
    async def scenario():
        protocol = PendingProtocol()
        monkeypatch.setattr("infomentor_mcp.login.attempt.StockholmBankId", lambda: protocol)
        server = create_server()
        started = (await server.call_tool("auth_start", {})).structured_content
        args = {"login_id": started["login_id"]}
        waiter = asyncio.create_task(server.call_tool("auth_wait", args))
        await asyncio.sleep(0)
        assert not waiter.done()
        protocol.approve.set()
        result = (await asyncio.wait_for(waiter, 3)).structured_content
        assert result["status"] == "complete"
        assert SessionStore().load() is not None
        assert "secret" not in str(result)
        assert "login_url" not in result
        assert (await server.call_tool("auth_wait", args)).structured_content == result

    asyncio.run(scenario())


def test_mcp_mobile_authorization_changes_require_explicit_confirmation(httpx_mock):
    server = create_server()
    result = asyncio.run(server.call_tool("auth_revoke", {"confirm": False}))
    assert result.structured_content["error"]["code"] == "confirmation_required"
    assert not httpx_mock.get_requests()
