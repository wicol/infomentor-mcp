import asyncio

from infomentor_mcp.auth.session import AuthSession
from infomentor_mcp.auth.stockholm_bankid import BankIdOrder, BankIdStatus
from infomentor_mcp.login import terminal_qr


class ReadyProtocol:
    def __init__(self):
        self.statuses = iter([
            BankIdStatus("USER_SIGN", "updated-qr"), BankIdStatus("OK")
        ])

    async def poll(self, order):
        return next(self.statuses)

    async def finish(self, order):
        return AuthSession()


def test_terminal_flow_owns_its_qr_updates(monkeypatch):
    frames = []

    async def no_delay(_):
        pass

    monkeypatch.setattr(asyncio, "sleep", no_delay)
    monkeypatch.setattr(terminal_qr.TerminalQr, "draw", lambda self, data: frames.append(data))
    result = asyncio.run(terminal_qr.wait_for_approval(
        ReadyProtocol(),
        BankIdOrder("https://login003.stockholm.se/", "order", qr_data="initial-qr"),
    ))
    assert isinstance(result, AuthSession)
    assert frames == ["initial-qr", "updated-qr"]
