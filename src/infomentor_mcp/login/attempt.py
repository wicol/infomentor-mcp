"""One async MCP login task and its small, sanitized status object."""

from __future__ import annotations

import asyncio
import secrets

from infomentor_mcp.auth.bankid import StockholmBankId
from infomentor_mcp.login.complete import complete_mobile_login
from infomentor_mcp.login.web_qr import WebQr, WebQrOptions
from infomentor_mcp.storage.lock import SessionLockTimeout
from infomentor_mcp.storage.session import SessionStore


class LoginAttempt:
    """One background MCP login: BankID, then automatic mobile authorization setup.

    Tools observe it through sanitized snapshots; the login link is only returned once.
    """

    def __init__(
        self, store: SessionStore, method: str, options: WebQrOptions,
        *, protocol_factory=None,
    ) -> None:
        if method not in {"url", "web-qr"}:
            raise ValueError("MCP login method must be url or web-qr.")
        self.store, self.method, self.options = store, method, options
        self.protocol_factory = protocol_factory or StockholmBankId
        self.id = secrets.token_urlsafe(24)
        self.state = "starting"
        self.error_code: str | None = None
        self.link: str | None = None
        self.protocol: StockholmBankId | None = None
        self.view: WebQr | None = None
        self.task: asyncio.Task | None = None
        self.ready = asyncio.Event()
        self.web_session_stored = False

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    async def start(self) -> dict:
        self.task = asyncio.create_task(self._run(), name="infomentor-bankid-login")
        try:
            async with asyncio.timeout(30):
                await self.ready.wait()
        except TimeoutError:
            await self.cancel()
            self.state, self.error_code = "failed", "startup_timeout"
        except asyncio.CancelledError:
            await self.cancel()
            raise
        return self.snapshot(include_link=True)

    def snapshot(self, *, include_link: bool = False) -> dict:
        stage = self.protocol.stage if self.protocol else "starting"
        status = (
            "verifying" if self.state == "waiting" and stage in {"saml", "verify"} else self.state
        )
        result = {
            "login_id": self.id, "method": self.method, "status": status, "stage": stage,
            "error_code": self.error_code, "client_timeout_seconds": 240,
            "web_session_stored": self.web_session_stored,
            "mobile_session_bound": self.state in {"finishing", "complete"},
            "diagnostics": list(self.protocol.diagnostics) if self.protocol else [],
        }
        if include_link and self.link is not None and self.state == "waiting":
            result["login_url"] = self.link
            result["privacy_notice"] = "This short-lived login link is sensitive; do not share it."
        return result

    async def _run(self) -> None:
        try:
            async with asyncio.timeout(300), self.store.async_locked(timeout=0) as record:
                self.protocol = self.protocol_factory()
                if self.method == "web-qr":
                    self.protocol.stage = "web_setup"
                    self.view = WebQr(self.options)
                    await self.view.start()
                    self.link = self.view.url
                    self.protocol.stage = "web_wait"
                    self.state = "waiting"
                    self.ready.set()
                    # Stockholm expires an unscanned order after about 30 seconds.
                    # Wait for the browser's frame request, not chat/link delivery.
                    async with asyncio.timeout(240):
                        await self.view.opened.wait()
                order = await self.protocol.begin("url" if self.method == "url" else "qr")
                self.link = order.launch_url if self.method == "url" else self.view.url
                self.state = "waiting"
                self.ready.set()
                session = await self.protocol.wait(order, self.view)
                self.state = "saving"
                record.save(session)
                self.web_session_stored = True
            self.state = "pairing"
            self.protocol.stage = "mobile_setup"
            if self.view:
                self.view.show(None, "pairing")
            await complete_mobile_login(self.store, session)
            if self.view:
                self.state = "finishing"
                self.view.show(None, "complete")
                await asyncio.sleep(1)
            self.state = "complete"
        except asyncio.CancelledError:
            # Closing the viewer after persistence cannot undo a completed login.
            self.state = "complete" if self.state == "finishing" else "cancelled"
            raise
        except TimeoutError:
            self.state, self.error_code = "failed", "login_timeout"
        except SessionLockTimeout:
            self.state, self.error_code = "failed", "session_busy"
        except ValueError:
            self.state = "failed"
            self.error_code = (
                "web_configuration_invalid"
                if self.protocol and self.protocol.stage == "web_setup" else "login_failed"
            )
        except OSError:
            self.state = "failed"
            self.error_code = (
                "web_bind_failed"
                if self.protocol and self.protocol.stage == "web_setup"
                else "session_storage_failed"
            )
        except Exception:
            # Upstream exceptions and validation errors can contain credentials.
            self.state, self.error_code = "failed", "login_failed"
        finally:
            if self.web_session_stored and self.state == "failed":
                self.state, self.error_code = "partial", "mobile_authorization_failed"
            self.link = None
            cleanup = []
            if self.view:
                cleanup.append(self.view.close())
            if self.protocol:
                cleanup.append(self.protocol.close())
            await asyncio.gather(*cleanup, return_exceptions=True)
            self.ready.set()

    async def cancel(self) -> dict:
        if self.running:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            if self.state not in {"complete", "failed", "partial"}:
                self.state = "cancelled"
            self.link = None
            self.ready.set()
        return self.snapshot()
