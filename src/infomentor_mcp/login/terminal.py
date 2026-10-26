"""Direct CLI BankID login: a same-device link or an animated terminal QR code."""

from __future__ import annotations

import asyncio
import sys
from io import StringIO

import qrcode

from infomentor_mcp.auth.bankid import StockholmBankId
from infomentor_mcp.storage.session import AuthSession


class TerminalQr:
    """Redraws the QR code in place when the terminal supports it."""

    def __init__(self) -> None:
        self.lines = 0
        self.last: str | None = None

    def show(self, qr_data: str | None, status: str) -> None:
        if qr_data and qr_data != self.last:
            self.last = qr_data
            self.draw(qr_data)

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


class TerminalLogin:
    """One linear login without a background task or web server."""

    def __init__(self, method: str) -> None:
        if method not in {"qr", "url"}:
            raise ValueError("BankID method must be qr or url.")
        self.method = method

    async def run(self) -> AuthSession:
        protocol = StockholmBankId()
        try:
            async with asyncio.timeout(300):
                order = await protocol.begin(self.method)
                if self.method == "url":
                    print("Öppna engångslänken i BankID på telefonen. Dela den inte.", flush=True)
                    print(order.launch_url, flush=True)
                    print("Auth: väntar på BankID, högst fyra minuter.", flush=True)
                    return await protocol.wait(order)
                print("Skanna med BankID på en annan enhet. Avbryt med Ctrl-C.", flush=True)
                return await protocol.wait(order, TerminalQr())
        except Exception:
            print(f"Auth: failed at {protocol.stage}; HTTP diagnostics: {protocol.diagnostics}")
            raise
        finally:
            await protocol.close()
