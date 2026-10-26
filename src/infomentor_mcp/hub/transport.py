"""Guarded HTTP transport for InfoMentor Hub and its Swedish web-authentication flow."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse

import httpx

from infomentor_mcp.errors import AuthenticationExpiredError, InfoMentorError, UnsafeRedirectError
from infomentor_mcp.models import STOCKHOLM
from infomentor_mcp.storage.session import AuthSession

HUB_ORIGIN = "https://hub.infomentor.se"
ALLOWED_EXACT_HOSTS = {
    "infomentor.se", "hub.infomentor.se", "sso.infomentor.se", "im.infomentor.se"
}
ALLOWED_STOCKHOLM_SUFFIX = ".stockholm.se"
USER_AGENT_HEADERS = {"User-Agent": "infomentor-mcp/0.0.1", "Accept": "*/*"}


def validate_auth_url(url: str, base_url: str | None = None) -> str:
    """Resolve and restrict authentication navigation to HTTPS InfoMentor/Stockholm hosts."""
    resolved = urljoin(base_url, url) if base_url else url
    parsed = urlparse(resolved)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or (
        host not in ALLOWED_EXACT_HOSTS and not host.endswith(ALLOWED_STOCKHOLM_SUFFIX)
    ):
        raise UnsafeRedirectError(
            "Authentication redirect points outside approved providers "
            f"(host: {host or 'missing'})."
        )
    return resolved


class HubTransport:
    """An httpx client with explicit redirect and expired-session handling.

    The object is stable for its users; reset() swaps the connection pool and cookies.
    """

    VERIFY_PATH = "/timetable/timetable/appData"

    def __init__(self, session: AuthSession | None = None) -> None:
        self.client: httpx.Client | None = None
        self.mobile_binding: str | None = None
        self.reset(session or AuthSession())

    def __enter__(self) -> HubTransport:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def reset(self, session: AuthSession) -> None:
        """Use a new connection pool with this session, e.g. after another process renewed
        it on a different backend."""
        previous = self.client
        self.client = httpx.Client(
            follow_redirects=False, headers=USER_AGENT_HEADERS, timeout=httpx.Timeout(20.0),
        )
        session.apply_to(self.client)
        self.mobile_binding = session.mobile_binding
        if previous is not None:
            previous.close()

    def holds(self, session: AuthSession) -> bool:
        """Whether this transport already uses exactly the session's cookies and binding."""
        def jar(cookies):
            return sorted(
                (cookie.domain, cookie.path, cookie.name, cookie.value, cookie.secure,
                 cookie.expires) for cookie in cookies
            )

        return (
            jar(session.cookies) == jar(self.client.cookies.jar)
            and session.mobile_binding == self.mobile_binding
        )

    def session(self) -> AuthSession:
        session = AuthSession.from_client(self.client)
        session.mobile_binding = self.mobile_binding
        return session

    @contextmanager
    def request_hook(self, hook):
        """Run `hook` before each request of the current connection pool."""
        hooks = self.client.event_hooks["request"]
        hooks.append(hook)
        try:
            yield
        finally:
            hooks.remove(hook)

    def request_auth(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        return self.client.request(method, validate_auth_url(url), **kwargs)

    def get_page(self, path: str) -> str:
        response = self.client.get(f"{HUB_ORIGIN}{path}", follow_redirects=False)
        if response.status_code == 403:
            raise InfoMentorError("Hub access denied (HTTP 403); no automatic renewal attempted.")
        if response.is_redirect or response.status_code == 401:
            raise AuthenticationExpiredError(
                "InfoMentor session has expired; run auth login again."
            )
        response.raise_for_status()
        if not response.content.strip():
            raise AuthenticationExpiredError("Hub returned an empty successful response.")
        return response.text

    def post(self, path: str, payload: object, *, form: bool = False) -> httpx.Response:
        response = self.client.post(
            f"{HUB_ORIGIN}{path}",
            **({"data": payload} if form else {"json": payload}),
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        if response.is_redirect:
            location = response.headers.get("location", "")
            redirect = urlparse(urljoin(str(response.url), location))
            raise AuthenticationExpiredError(
                "InfoMentor session verification was redirected "
                f"(HTTP {response.status_code}, host: {redirect.hostname or 'missing'}, "
                f"path: {redirect.path or '/'}).",
                http_status=response.status_code,
            )
        if response.status_code == 403:
            raise InfoMentorError(
                "InfoMentor denied access (HTTP 403); no automatic renewal attempted."
            )
        if response.status_code == 401:
            raise AuthenticationExpiredError(
                "InfoMentor session has expired; run auth login again.",
                http_status=401,
            )
        response.raise_for_status()
        if not response.content.strip():
            raise AuthenticationExpiredError(
                "InfoMentor returned an empty successful response.",
                http_status=response.status_code,
            )
        return response

    @staticmethod
    def verification_payload() -> dict[str, str]:
        """A minimal documented read-only request proving that a session is authenticated."""
        start = datetime.now(STOCKHOLM).date()
        return {
            "startDate": start.isoformat(), "endDate": (start + timedelta(days=1)).isoformat(),
        }

    def verify_authenticated_read(self) -> int:
        """Perform the verification read before persisting a new session."""
        response = self.post(self.VERIFY_PATH, self.verification_payload())
        try:
            payload = response.json()
        except ValueError as error:
            raise InfoMentorError(
                "InfoMentor did not return timetable JSON; session validity is inconclusive."
            ) from error
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise InfoMentorError(
                "Unexpected timetable structure; session validity is inconclusive."
            )
        return response.status_code

    def close(self) -> None:
        self.client.close()
