"""Bounded, metadata-only measurement of Hub and Stockholm SSO continuity."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx

from infomentor_mcp.auth.saml import SamlSession
from infomentor_mcp.auth.session import SessionStore
from infomentor_mcp.client.http import (
    AuthenticationExpiredError,
    InfoMentorError,
    InfoMentorHttpClient,
)


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


def measure_session(
    http: InfoMentorHttpClient,
    store: SessionStore,
    emit: Callable[[dict], None],
    *,
    interval: float = 120,
    duration: float = 8 * 3600,
    idle: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    http_factory: Callable = InfoMentorHttpClient,
    login_factory: Callable = SamlSession,
) -> str:
    """Measure under the caller's session lock, never initializing a BankID order.

    The caller must freshly log in and verify a read immediately beforehand.
    Idle mode performs exactly one delayed check, then exits. Network errors
    and unexpected provider responses end the test as inconclusive.
    """
    if interval < 1 or duration < 1 or (idle is not None and not 1 <= idle <= duration):
        raise ValueError("Use positive interval/duration and an idle time within the duration.")
    started = clock()
    generation = started
    last_good = started
    renewals = 0

    def report(event: str, **metadata) -> None:
        emit({"event": event, "elapsed_seconds": round(clock() - started, 3), **metadata})

    report(
        "started",
        mode="idle" if idle is not None else "active",
        interval_seconds=interval,
        duration_seconds=duration,
        idle_seconds=idle,
    )
    try:
        while clock() - started < duration:
            remaining = duration - (clock() - started)
            sleep(min(idle if idle is not None else interval, remaining))
            try:
                http_status = http.verify_authenticated_read()
            except AuthenticationExpiredError as error:
                report(
                    "hub_authentication_failed",
                    http_status=error.http_status,
                    last_success_elapsed_seconds=round(last_good - started, 3),
                    hub_lifetime_lower_seconds=round(last_good - generation, 3),
                    hub_lifetime_upper_seconds=round(clock() - generation, 3),
                )
                renewal = http_factory(http.session().stockholm_sso_only())
                try:
                    report("sso_attempt")
                    login = login_factory(renewal, emit_diagnostics=False)
                    if not login.resume_from_stockholm_sso():
                        report("bankid_required", successful_renewals=renewals)
                        return "bankid_required"
                    renewal.verify_authenticated_read()
                    session = renewal.session()
                    store.save(session)
                    session.apply_to(http.client)
                finally:
                    renewal.close()
                renewals += 1
                generation = clock()
                report("sso_renewed", successful_renewals=renewals)
            else:
                store.save(http.session())
                report("hub_read_ok", http_status=http_status)
            last_good = clock()
            if idle is not None:
                report("idle_check_complete", successful_renewals=renewals)
                return "idle_check_complete"
        report("duration_reached", successful_renewals=renewals)
        return "duration_reached"
    except httpx.HTTPStatusError as error:
        report("inconclusive", reason="http_error", http_status=error.response.status_code)
    except httpx.RequestError:
        report("inconclusive", reason="network_error")
    except InfoMentorError:
        report("inconclusive", reason="unexpected_authentication_response")
    except KeyboardInterrupt:
        report("interrupted")
        return "interrupted"
    return "inconclusive"
