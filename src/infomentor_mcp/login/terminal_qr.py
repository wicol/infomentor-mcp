"""Terminal-only animated QR rendering and its linear async login flow."""

import asyncio
import sys
from io import StringIO

import qrcode

from infomentor_mcp.auth.stockholm_bankid import BankIdOrder, StockholmBankId


class TerminalQr:
    def __init__(self) -> None:
        self.lines = 0

    def draw(self, data: str) -> None:
        qr = qrcode.QRCode(border=2)
        qr.add_data(data)
        qr.make(fit=True)
        buffer = StringIO()
        qr.print_ascii(out=buffer, invert=True)
        frame = buffer.getvalue().splitlines()
        if sys.stdout.isatty() and self.lines:
            sys.stdout.write(f"\033[{self.lines}A")
            height = max(self.lines, len(frame))
            for index in range(height):
                line = frame[index] if index < len(frame) else ""
                sys.stdout.write(f"\033[2K{line}\n")
            self.lines = height
        else:
            sys.stdout.write(buffer.getvalue())
            self.lines = len(frame)
        sys.stdout.flush()


async def wait_for_approval(protocol: StockholmBankId, order: BankIdOrder):
    renderer = TerminalQr()
    last = order.qr_data
    if last:
        renderer.draw(last)
    async with asyncio.timeout(240):
        while True:
            await asyncio.sleep(1)
            status = await protocol.poll(order)
            if status.state == "OK":
                return await protocol.finish(order)
            if status.qr_data and status.qr_data != last:
                last = status.qr_data
                renderer.draw(last)


async def login():
    """Direct terminal path, without a background task or web server."""
    protocol = StockholmBankId()
    try:
        async with asyncio.timeout(300):
            order = await protocol.begin("qr")
            print("Skanna med BankID på en annan enhet. Avbryt med Ctrl-C.", flush=True)
            return await wait_for_approval(protocol, order)
    except Exception:
        print(f"Auth: failed at {protocol.stage}; HTTP diagnostics: {protocol.diagnostics}")
        raise
    finally:
        await protocol.close()
