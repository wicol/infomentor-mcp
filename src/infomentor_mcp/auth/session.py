"""Persisted, redacted-at-the-boundary InfoMentor web sessions."""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
import keyring
from pydantic import BaseModel, Field

from infomentor_mcp.auth.locking import session_lock

SERVICE_NAME = "infomentor-mcp"
ACCOUNT_NAME = "swedish-guardian-session"


class StoredCookie(BaseModel):
    """The cookie attributes needed to reconstruct an httpx cookie jar."""

    name: str
    value: str
    domain: str
    path: str = "/"
    expires: int | None = None
    secure: bool = True


class AuthSession(BaseModel):
    """A local authenticated web session; never return this through MCP."""

    cookies: list[StoredCookie] = Field(default_factory=list)
    saved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    mobile_binding: str | None = None

    @classmethod
    def from_client(cls, client: httpx.Client | httpx.AsyncClient) -> AuthSession:
        cookies = [
            StoredCookie(
                name=cookie.name,
                value=cookie.value,
                domain=cookie.domain,
                path=cookie.path,
                expires=cookie.expires,
                secure=cookie.secure,
            )
            for cookie in client.cookies.jar
        ]
        return cls(cookies=cookies)

    def apply_to(self, client: httpx.Client | httpx.AsyncClient) -> None:
        client.cookies.clear()
        for cookie in self.cookies:
            client.cookies.set(
                cookie.name,
                cookie.value,
                domain=cookie.domain,
                path=cookie.path,
            )

    def stockholm_sso_only(self) -> AuthSession:
        """Copy only Stockholm cookies for an isolated SSO renewal probe."""
        return self.model_copy(
            update={
                "cookies": [
                    cookie
                    for cookie in self.cookies
                    if cookie.domain.lower().rstrip(".").endswith(".stockholm.se")
                ]
            }
        )


class SessionStore:
    """Use OS keyrings when possible, with a private XDG-state fallback."""

    def __init__(self, fallback_path: Path | None = None) -> None:
        state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        self._fallback_path = fallback_path or state_home / "infomentor-mcp" / "session.json"
        self._lock = session_lock(self._fallback_path.absolute().with_suffix(".lock"))

    def locked(self, timeout: float = 30.0):
        """Serialize a complete operation, including nested load/save calls."""
        return self._lock.acquire(timeout)

    def async_locked(self, timeout: float = 30.0):
        """Hold across async login; use _load/_save inside to avoid nested flock."""
        return self._lock.acquire_async(timeout)

    def load(self) -> AuthSession | None:
        with self.locked():
            return self._load()

    def _load(self) -> AuthSession | None:
        serialized = self._load_keyring()
        if serialized is None:
            serialized = self._load_file()
        return AuthSession.model_validate_json(serialized) if serialized else None

    def save(self, session: AuthSession) -> None:
        with self.locked():
            self._save(session)

    def _save(self, session: AuthSession) -> None:
        serialized = session.model_dump_json()
        if self._save_keyring(serialized):
            self._remove_file()
            return

        self._fallback_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(dir=self._fallback_path.parent, prefix=".session-")
        temporary_path = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            temporary_path.replace(self._fallback_path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def clear(self) -> None:
        with self.locked():
            self._clear()

    def _clear(self) -> None:
        try:
            keyring.delete_password(SERVICE_NAME, ACCOUNT_NAME)
        except keyring.errors.KeyringError:
            pass
        self._remove_file()

    def _load_keyring(self) -> str | None:
        try:
            return keyring.get_password(SERVICE_NAME, ACCOUNT_NAME)
        except keyring.errors.KeyringError:
            return None

    def _save_keyring(self, serialized: str) -> bool:
        try:
            keyring.set_password(SERVICE_NAME, ACCOUNT_NAME, serialized)
        except keyring.errors.KeyringError:
            return False
        return True

    def _load_file(self) -> str | None:
        try:
            return self._fallback_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def _remove_file(self) -> None:
        try:
            self._fallback_path.unlink()
        except FileNotFoundError:
            pass

    @property
    def fallback_path(self) -> Path:
        """Return the fallback path for status reporting and tests only."""
        return self._fallback_path
