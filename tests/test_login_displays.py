import asyncio

from infomentor_mcp.auth.bankid import BankIdOrder, BankIdStatus, StockholmBankId
from infomentor_mcp.login.terminal import TerminalQr
from infomentor_mcp.storage.session import AuthSession


class ReadyProtocol(StockholmBankId):
    def __init__(self):
        self.statuses = iter([
            BankIdStatus("USER_SIGN", "updated-qr"), BankIdStatus("USER_SIGN"), BankIdStatus("OK")
        ])

    async def poll(self, order):
        return next(self.statuses)

    async def finish(self, order):
        return AuthSession()


def test_terminal_view_redraws_only_changed_qr_codes(monkeypatch):
    frames = []

    async def no_delay(_):
        pass

    monkeypatch.setattr(asyncio, "sleep", no_delay)
    monkeypatch.setattr(TerminalQr, "draw", lambda self, data: frames.append(data))
    result = asyncio.run(ReadyProtocol().wait(
        BankIdOrder("https://login003.stockholm.se/", "order", qr_data="initial-qr"),
        TerminalQr(),
    ))
    assert isinstance(result, AuthSession)
    assert frames == ["initial-qr", "updated-qr"]
