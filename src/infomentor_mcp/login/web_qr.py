"""Temporary asyncio HTTP view for animated BankID QR, including trusted-LAN use."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import secrets
from dataclasses import dataclass
from urllib.parse import urlsplit

import qrcode

from infomentor_mcp.auth.stockholm_bankid import BankIdOrder, StockholmBankId


@dataclass(frozen=True)
class WebQrOptions:
    host: str = "127.0.0.1"
    port: int = 0
    public_url: str | None = None


class WebQr:
    def __init__(self, options: WebQrOptions) -> None:
        self.options = options
        self.server: asyncio.Server | None = None
        self.path = "/" + secrets.token_urlsafe(32) + "/"
        self.url = ""
        self.origin = ""
        self.matrix: list[list[bool]] | None = None
        self.status = "starting"
        self.opened = asyncio.Event()
        self._writers: set[asyncio.StreamWriter] = set()
        self._hosts: set[str] = set()

    async def start(self) -> None:
        address = ipaddress.ip_address(self.options.host)
        if not 0 <= self.options.port <= 65535:
            raise ValueError("Invalid web QR port.")
        if not address.is_loopback and not self.options.public_url:
            raise ValueError("Non-loopback web QR requires an explicit --public-url.")
        base = self.options.public_url or (
            f"http://[{address}]" if address.version == 6 else f"http://{address}"
        )
        parsed = urlsplit(base)
        if (
            parsed.scheme != "http" or not parsed.hostname
            or parsed.hostname in {"0.0.0.0", "::"}
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
            or any(char.isspace() or ord(char) < 32 for char in base) or "\\" in base
        ):
            raise ValueError(
                "Public URL must be an HTTP origin without credentials, path or query."
            )
        # Validate the port before binding; TLS/reverse-proxy setups are not implied.
        advertised_port = parsed.port
        self.server = await asyncio.start_server(
            self._handle, self.options.host, self.options.port, limit=8192
        )
        port = self.server.sockets[0].getsockname()[1]
        if advertised_port is not None and advertised_port != port:
            await self.close()
            raise ValueError("Public URL port must match the bound --port.")
        hostname = parsed.hostname
        host = f"[{hostname}]" if ":" in hostname else hostname
        self.origin = f"http://{host}" if port == 80 else f"http://{host}:{port}"
        self._hosts = {f"{host}:{port}".lower()}
        if port == 80:
            self._hosts.add(host.lower())
        self.url = self.origin + self.path

    def update(self, qr_data: str | None, status: str) -> None:
        self.status = status
        if qr_data is None:
            self.matrix = None
            return
        qr = qrcode.QRCode(border=4)
        qr.add_data(qr_data)
        qr.make(fit=True)
        self.matrix = qr.get_matrix()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            if len(raw) > 8192:
                return
            lines = raw.decode("ascii").split("\r\n")
            method, path, version = lines[0].split(" ")
            if version != "HTTP/1.1" or len(lines[0]) > 2048:
                return
            headers = {}
            for line in lines[1:]:
                if line:
                    key, value = line.split(":", 1)
                    key = key.lower()
                    if key in headers:
                        return
                    headers[key] = value.strip()
            code, mime, body = 404, "text/plain; charset=utf-8", b"Not found"
            nonce = secrets.token_urlsafe(18)
            if (
                method == "GET" and headers.get("host", "").lower() in self._hosts
                and headers.get("origin", self.origin) == self.origin
            ):
                if path == self.path:
                    code, mime = 200, "text/html; charset=utf-8"
                    body = _page(nonce).encode()
                elif path == self.path + "frame":
                    self.opened.set()
                    code, mime = 200, "application/json"
                    body = json.dumps({"matrix": self.matrix, "status": self.status}).encode()
            response_headers = (
                f"HTTP/1.1 {code} {'OK' if code == 200 else 'Not Found'}\r\n"
                f"Content-Type: {mime}\r\nContent-Length: {len(body)}\r\n"
                "Cache-Control: no-store\r\nReferrer-Policy: no-referrer\r\n"
                "X-Content-Type-Options: nosniff\r\nConnection: close\r\n"
                f"Content-Security-Policy: default-src 'none'; script-src 'nonce-{nonce}'; "
                "connect-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'none'\r\n\r\n"
            )
            writer.write(response_headers.encode() + body)
            await asyncio.wait_for(writer.drain(), timeout=5)
        except (ValueError, UnicodeError, TimeoutError, ConnectionError,
                asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass  # Never log request targets containing the capability.
        finally:
            self._writers.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    async def close(self) -> None:
        self.matrix = None
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        writers = list(self._writers)
        for writer in writers:
            writer.close()
        await asyncio.gather(*(writer.wait_closed() for writer in writers), return_exceptions=True)


def _page(nonce: str) -> str:
    return f"""<!doctype html><html lang="sv"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>InfoMentor BankID</title><style>
body{{font:18px system-ui;text-align:center;margin:2rem}} canvas{{width:min(90vw,450px)}}
</style><h1>Logga in med BankID</h1><p>Skanna med BankID på en annan enhet.</p>
<canvas width="600" height="600" aria-label="Animerad BankID QR-kod"></canvas>
<p id="status">Startar…</p><script nonce="{nonce}">
const canvas=document.querySelector('canvas'),ctx=canvas.getContext('2d');
const labels={{starting:'Startar…',waiting:'Skanna QR-koden',user_sign:'Godkänn i BankID',
verifying:'Verifierar inloggningen…',pairing:'Förbereder sessionsförnyelse…',
complete:'Inloggningen är sparad',
failed:'Inloggningen misslyckades',cancelled:'Inloggningen avbröts'}};
async function tick(){{try{{
 const r=await fetch('frame',{{cache:'no-store'}}); if(!r.ok)throw new Error();
 const d=await r.json();document.querySelector('#status').textContent=labels[d.status]||'Väntar…';
 ctx.fillStyle='white';ctx.fillRect(0,0,600,600);
 if(d.matrix){{const n=d.matrix.length,s=600/n;ctx.fillStyle='black';
 for(let y=0;y<n;y++)for(let x=0;x<n;x++)if(d.matrix[y][x])
 ctx.fillRect(Math.floor(x*s),Math.floor(y*s),Math.ceil(s),Math.ceil(s));}}
 if(!['complete','failed','cancelled'].includes(d.status))setTimeout(tick,700);
}}catch(e){{document.querySelector('#status').textContent=
'Visningen är stängd. Kontrollera inloggningsstatus.';
ctx.clearRect(0,0,600,600);}}}}tick();</script></html>"""


async def wait_for_approval(protocol: StockholmBankId, order: BankIdOrder, view: WebQr):
    view.update(order.qr_data, "waiting")
    last = order.qr_data
    async with asyncio.timeout(240):
        while True:
            await asyncio.sleep(1)
            status = await protocol.poll(order)
            if status.state == "OK":
                view.update(None, "verifying")
                return await protocol.finish(order)
            last = status.qr_data or last
            view.update(last,
                        "user_sign" if status.state == "USER_SIGN" else "waiting")
