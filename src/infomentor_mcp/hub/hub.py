"""One guardian's Hub connection: locking, session refresh, renewal and persistence."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, TypeVar

import keyring.errors

from infomentor_mcp.errors import AuthenticationExpiredError, SessionStorageError
from infomentor_mcp.hub.pupils import PupilSwitcher
from infomentor_mcp.hub.transport import HubTransport
from infomentor_mcp.storage.session import AuthSession, SessionStore

T = TypeVar("T")
STORAGE_ERRORS = (OSError, ValueError, keyring.errors.KeyringError)


class SessionRenewer(Protocol):
    def renew(self, current: AuthSession) -> AuthSession:
        """Return a renewed, already persisted session, or raise AuthenticationExpiredError."""


class Hub:
    """Operations on the stored guardian session, serialized by the session lock.

    Hub's pupil selection is shared server-side state, so every operation holds the lock
    and restores the selection it found.
    """

    def __init__(self, store: SessionStore, renewer: SessionRenewer | None = None) -> None:
        self.store = store
        self.renewer = renewer
        self.transport = HubTransport()
        self.pupils = PupilSwitcher(self.transport)

    def __enter__(self) -> Hub:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        self.transport.close()

    def refresh(self) -> None:
        """Use the latest stored session; another process may have renewed or replaced it."""
        try:
            latest = self.store.load()
        except STORAGE_ERRORS:
            raise SessionStorageError() from None
        if latest is None:
            raise AuthenticationExpiredError(
                "No local InfoMentor session is stored; run infomentor-mcp auth login."
            )
        if not self.transport.holds(latest):
            self.transport.reset(latest)

    def run(self, operation: Callable[[], T], *, keep_result_on_storage_error: bool = False) -> T:
        """Run `operation`, renew an expired session and replay it once, then persist rotated
        cookies. Operations that write must catch expiry themselves once a write was sent."""
        with self.store.locked():
            self.pupils.original_id = None
            self.refresh()
            persist = True
            completed = False
            result = None
            try:
                try:
                    result = operation()
                except AuthenticationExpiredError:
                    # A renewal may persist newer credentials/session before a later
                    # step fails. Never overwrite those with this old jar.
                    persist = False
                    self._renew()
                    persist = True
                    result = operation()
                completed = True
            finally:
                if persist:
                    self._persist(result if completed else None, keep_result_on_storage_error)
            return result

    def _renew(self) -> None:
        if self.renewer is None:
            raise AuthenticationExpiredError("InfoMentor session expired; run auth login again.")
        self.transport.reset(self.renewer.renew(self.transport.session()))
        if self.pupils.original_id is not None:
            self.pupils.select(self.pupils.original_id)

    def _persist(self, result, keep_result_on_storage_error: bool) -> None:
        try:
            self.store.save(self.transport.session())
        except STORAGE_ERRORS:
            if not keep_result_on_storage_error or result is None:
                raise SessionStorageError() from None
            # A local persistence failure does not undo remote changes. Keep verified,
            # unknown and per-day outcomes so callers do not blindly retry.
            result.setdefault("warnings", []).append({
                "code": "session_storage_error",
                "message": "Could not save the local session. Operation results are "
                "retained; do not repeat writes just because of this warning. "
                "Read current state before retrying uncertain changes. "
                "Reauthentication may be needed for subsequent requests.",
            })
