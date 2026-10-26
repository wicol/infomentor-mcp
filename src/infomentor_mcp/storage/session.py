"""Persisted, redacted-at-the-boundary InfoMentor web sessions."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from http.cookiejar import Cookie
from pathlib import Path

import httpx
from pydantic import BaseModel, Field

from infomentor_mcp.storage.lock import SessionLock
from infomentor_mcp.storage.secrets import SecretStore

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
            client.cookies.jar.set_cookie(Cookie(
                version=0, name=cookie.name, value=cookie.value,
                port=None, port_specified=False,
                domain=cookie.domain, domain_specified=bool(cookie.domain),
                domain_initial_dot=cookie.domain.startswith("."),
                path=cookie.path, path_specified=bool(cookie.path),
                secure=cookie.secure, expires=cookie.expires, discard=cookie.expires is None,
                comment=None, comment_url=None, rest={}, rfc2109=False,
            ))

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


class SessionRecord:
    """Unlocked load/save of the stored web session, for a caller that holds the lock."""

    def __init__(self, secret: SecretStore) -> None:
        self.secret = secret

    def load(self) -> AuthSession | None:
        text = self.secret.read()
        return AuthSession.model_validate_json(text) if text else None

    def save(self, session: AuthSession) -> None:
        self.secret.write(session.model_dump_json())

    def clear(self) -> None:
        self.secret.delete()


class SessionStore:
    """The guardian web session in the OS keyring, with a private XDG-state fallback.

    Every operation holds the session lock, which also serializes Hub pupil selection and
    mobile credential changes across threads and processes.
    """

    def __init__(self, fallback_path: Path | None = None) -> None:
        state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        self.fallback_path = fallback_path or state_home / "infomentor-mcp" / "session.json"
        self.lock = SessionLock.shared(self.fallback_path.absolute().with_suffix(".lock"))
        self._record = SessionRecord(SecretStore(ACCOUNT_NAME, self.fallback_path))

    @property
    def state_dir(self) -> Path:
        """The private directory for other local state next to the session."""
        return self.fallback_path.parent

    def locked(self, timeout: float = 30.0):
        """Serialize a complete operation, including nested load/save calls."""
        return self.lock.acquire(timeout)

    @asynccontextmanager
    async def async_locked(self, timeout: float = 30.0):
        """Hold the lock across an async login and persist through the yielded record.

        The async lock is not reentrant, so nested locked load/save calls would deadlock.
        """
        async with self.lock.acquire_async(timeout):
            yield self._record

    def load(self) -> AuthSession | None:
        with self.locked():
            return self._record.load()

    def save(self, session: AuthSession) -> None:
        with self.locked():
            self._record.save(session)

    def clear(self) -> None:
        with self.locked():
            self._record.clear()
