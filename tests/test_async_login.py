import asyncio
import threading

import httpx
import pytest

from infomentor_mcp.auth.bankid import BankIdOrder, BankIdStatus, StockholmBankId
from infomentor_mcp.errors import MobileFlowError
from infomentor_mcp.login.attempt import LoginAttempt
from infomentor_mcp.login.complete import complete_mobile_login
from infomentor_mcp.login.web_qr import WebQrOptions
from infomentor_mcp.storage.lock import SessionLockTimeout
from infomentor_mcp.storage.session import AuthSession, SessionStore

pytestmark = pytest.mark.usefixtures("completed_mobile_login")


class FakeProtocol(StockholmBankId):
    def __init__(self):
        self.stage = "bankid_wait"
        self.diagnostics = []
        self.closed = False
        self.approve = asyncio.Event()
        self.begun = False

    async def begin(self, method):
        self.begun = True
        return BankIdOrder(
            "https://login003.stockholm.se/auth", "synthetic-order",
            "https://app.bankid.com/?autostarttoken=synthetic-secret",
            "synthetic-qr-secret" if method == "qr" else None,
        )

    async def poll(self, order):
        await self.approve.wait()
        return BankIdStatus("OK")

    async def finish(self, order):
        self.stage = "verify"
        return AuthSession()

    async def close(self):
        self.closed = True


def test_task_returns_link_before_approval_and_cancellation_releases_lock():
    async def scenario():
        store = SessionStore()
        store.save(AuthSession(mobile_binding="existing-session"))
        protocol = FakeProtocol()
        attempt = LoginAttempt(store, "url", WebQrOptions(), protocol_factory=lambda: protocol)
        started = await attempt.start()
        assert started["status"] == "waiting"
        assert "synthetic-secret" in started["login_url"]
        assert not attempt.task.done()
        assert "synthetic-secret" not in str(attempt.snapshot())
        with pytest.raises(SessionLockTimeout), store.locked(timeout=0):
            pass
        result = await attempt.cancel()
        assert result["status"] == "cancelled"
        assert attempt.link is None
        assert protocol.closed
        assert store.load().mobile_binding == "existing-session"

    asyncio.run(scenario())


def test_approved_login_saves_only_after_verification():
    async def scenario():
        store = SessionStore()
        store.save(AuthSession(mobile_binding="old-login"))
        protocol = FakeProtocol()
        attempt = LoginAttempt(store, "url", WebQrOptions(), protocol_factory=lambda: protocol)
        await attempt.start()
        protocol.approve.set()
        await asyncio.wait_for(attempt.task, 3)
        assert attempt.snapshot()["status"] == "complete"
        assert attempt.link is None
        assert store.load().mobile_binding == "synthetic-bound-login"
        assert attempt.snapshot()["mobile_session_bound"]
        assert protocol.closed

    asyncio.run(scenario())


def test_failure_is_redacted_and_does_not_overwrite_saved_session():
    class FailingProtocol(FakeProtocol):
        async def begin(self, method):
            raise httpx.ConnectError("secret-cookie and secret-signed-url")

    async def scenario():
        store = SessionStore()
        store.save(AuthSession(mobile_binding="keep-me"))
        protocol = FailingProtocol()
        attempt = LoginAttempt(store, "url", WebQrOptions(), protocol_factory=lambda: protocol)
        result = await attempt.start()
        await attempt.task
        assert result["status"] == "failed"
        assert "secret" not in str(result)
        assert store.load().mobile_binding == "keep-me"
        assert protocol.closed

    asyncio.run(scenario())


def test_login_timeout_closes_protocol_and_releases_session():
    class TimeoutProtocol(FakeProtocol):
        async def poll(self, order):
            raise TimeoutError()

    async def scenario():
        store = SessionStore()
        protocol = TimeoutProtocol()
        attempt = LoginAttempt(store, "url", WebQrOptions(), protocol_factory=lambda: protocol)
        await attempt.start()
        await asyncio.wait_for(attempt.task, 3)
        assert attempt.snapshot()["error_code"] == "login_timeout"
        assert protocol.closed
        with store.locked(timeout=0):
            pass

    asyncio.run(scenario())


def test_web_task_closes_listener_on_cancel():
    async def scenario():
        store = SessionStore()
        protocol = FakeProtocol()
        attempt = LoginAttempt(store, "web-qr", WebQrOptions(), protocol_factory=lambda: protocol)
        started = await attempt.start()
        link = started["login_url"]
        assert started["stage"] == "web_wait"
        assert not protocol.begun
        async with httpx.AsyncClient(trust_env=False) as browser:
            assert (await browser.get(link)).status_code == 200
            assert not protocol.begun  # Link previews must not consume the order lifetime.
            await browser.get(link + "frame")
            frame = (await browser.get(link + "frame")).json()
            assert protocol.begun
            assert frame["matrix"]
            assert "synthetic-qr-secret" not in str(frame)
            await attempt.cancel()
            with pytest.raises(httpx.TransportError):
                await browser.get(link)
        assert protocol.closed
        with store.locked(timeout=0):
            pass

    asyncio.run(scenario())


def test_async_lock_contends_between_tasks_without_blocking_event_loop():
    async def scenario():
        store = SessionStore()
        async with store.async_locked():
            with pytest.raises(SessionLockTimeout):
                async with store.async_locked(timeout=0):
                    pass
            with pytest.raises(SessionLockTimeout), store.locked(timeout=0):
                pass
        with store.locked(timeout=0):
            pass

    asyncio.run(scenario())


def test_mobile_setup_failure_returns_partial_without_exposing_secrets(monkeypatch):
    def failed(_):
        raise ValueError("secret-token")

    monkeypatch.setattr("infomentor_mcp.auth.mobile.MobileAuthorization.complete_login", failed)

    async def scenario():
        store = SessionStore()
        protocol = FakeProtocol()
        attempt = LoginAttempt(store, "url", WebQrOptions(), protocol_factory=lambda: protocol)
        await attempt.start()
        protocol.approve.set()
        await asyncio.wait_for(attempt.task, 3)
        result = attempt.snapshot()
        assert result["status"] == "partial"
        assert result["web_session_stored"] and not result["mobile_session_bound"]
        assert result["error_code"] == "mobile_authorization_failed"
        assert store.load() is not None and store.load().mobile_binding is None
        assert "secret" not in str(result)

    asyncio.run(scenario())


def test_wait_and_cancel_do_not_abandon_mobile_persistence(monkeypatch):
    started, release = threading.Event(), threading.Event()

    def finish(authorization):
        started.set()
        assert release.wait(3)
        session = authorization.store.load()
        session.mobile_binding = "completed-binding"
        authorization.store.save(session)

    monkeypatch.setattr("infomentor_mcp.auth.mobile.MobileAuthorization.complete_login", finish)

    async def scenario():
        store = SessionStore()
        protocol = FakeProtocol()
        attempt = LoginAttempt(store, "url", WebQrOptions(), protocol_factory=lambda: protocol)
        await attempt.start()
        protocol.approve.set()
        try:
            assert await asyncio.to_thread(started.wait, 2)
            assert attempt.snapshot()["status"] == "pairing" and not attempt.task.done()
            cancelled = asyncio.create_task(attempt.cancel())
            await asyncio.sleep(0)
            release.set()
            assert (await cancelled)["status"] == "complete"
            assert store.load().mobile_binding == "completed-binding"
        finally:
            release.set()
            await attempt.cancel()

    asyncio.run(scenario())


def test_mobile_setup_does_not_overwrite_a_newer_login():
    async def scenario():
        store = SessionStore()
        newer = AuthSession(mobile_binding="newer-login")
        store.save(newer)
        with pytest.raises(MobileFlowError, match="Saved login changed"):
            await complete_mobile_login(store, AuthSession())
        assert store.load() == newer

    asyncio.run(scenario())
