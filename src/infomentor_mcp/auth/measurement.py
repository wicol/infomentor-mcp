"""Bounded, metadata-only measurement of Hub and Stockholm SSO continuity."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx

from infomentor_mcp.auth.saml import SamlFlow
from infomentor_mcp.errors import AuthenticationExpiredError, InfoMentorError
from infomentor_mcp.hub.transport import HubTransport
from infomentor_mcp.storage.session import SessionStore


class MeasurementLog:
    """An exclusive, private JSONL file containing only generated test metadata."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._stream = os.fdopen(descriptor, "w", encoding="utf-8")

    def emit(self, record: dict) -> None:
        line = json.dumps({"at": datetime.now(UTC).isoformat(), **record})
        self._stream.write(line + "\n")
        self._stream.flush()
        print(line, flush=True)

    def close(self) -> None:
        self._stream.close()


class SessionLifetimeProbe:
    """Measure under the caller's session lock, never initializing a BankID order.

    The caller must freshly log in and verify a read immediately beforehand.
    Idle mode performs exactly one delayed check, then exits. Network errors
    and unexpected provider responses end the test as inconclusive.
    """

    def __init__(
        self,
        http: HubTransport,
        store: SessionStore,
        emit: Callable[[dict], None],
        *,
        interval: float = 120,
        duration: float = 8 * 3600,
        idle: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        http_factory: Callable = HubTransport,
        login_factory: Callable = SamlFlow,
    ) -> None:
        if interval < 1 or duration < 1 or (idle is not None and not 1 <= idle <= duration):
            raise ValueError("Use positive interval/duration and an idle time within the duration.")
        self.http, self.store, self.emit = http, store, emit
        self.interval, self.duration, self.idle = interval, duration, idle
        self.clock, self.sleep = clock, sleep
        self.http_factory, self.login_factory = http_factory, login_factory
        self.started = self.generation = self.last_good = 0.0
        self.renewals = 0

    def run(self) -> str:
        self.started = self.generation = self.last_good = self.clock()
        self._report(
            "started",
            mode="idle" if self.idle is not None else "active",
            interval_seconds=self.interval,
            duration_seconds=self.duration,
            idle_seconds=self.idle,
        )
        try:
            while self.clock() - self.started < self.duration:
                remaining = self.duration - (self.clock() - self.started)
                self.sleep(min(self.idle if self.idle is not None else self.interval, remaining))
                try:
                    http_status = self.http.verify_authenticated_read()
                except AuthenticationExpiredError as error:
                    if not self._renew(error):
                        self._report("bankid_required", successful_renewals=self.renewals)
                        return "bankid_required"
                else:
                    self.store.save(self.http.session())
                    self._report("hub_read_ok", http_status=http_status)
                self.last_good = self.clock()
                if self.idle is not None:
                    self._report("idle_check_complete", successful_renewals=self.renewals)
                    return "idle_check_complete"
            self._report("duration_reached", successful_renewals=self.renewals)
            return "duration_reached"
        except httpx.HTTPStatusError as error:
            self._report(
                "inconclusive", reason="http_error", http_status=error.response.status_code,
            )
        except httpx.RequestError:
            self._report("inconclusive", reason="network_error")
        except InfoMentorError:
            self._report("inconclusive", reason="unexpected_authentication_response")
        except KeyboardInterrupt:
            self._report("interrupted")
            return "interrupted"
        return "inconclusive"

    def _renew(self, error: AuthenticationExpiredError) -> bool:
        """Record the Hub lifetime bounds, then try Stockholm SSO; False if BankID is needed."""
        self._report(
            "hub_authentication_failed",
            http_status=error.http_status,
            last_success_elapsed_seconds=round(self.last_good - self.started, 3),
            hub_lifetime_lower_seconds=round(self.last_good - self.generation, 3),
            hub_lifetime_upper_seconds=round(self.clock() - self.generation, 3),
        )
        renewal = self.http_factory(self.http.session().stockholm_sso_only())
        try:
            self._report("sso_attempt")
            if not self.login_factory(renewal, emit_diagnostics=False).resume_stockholm_sso():
                return False
            renewal.verify_authenticated_read()
            session = renewal.session()
            self.store.save(session)
            self.http.reset(session)
        finally:
            renewal.close()
        self.renewals += 1
        self.generation = self.clock()
        self._report("sso_renewed", successful_renewals=self.renewals)
        return True

    def _report(self, event: str, **metadata) -> None:
        self.emit({"event": event, "elapsed_seconds": round(self.clock() - self.started, 3),
                   **metadata})
