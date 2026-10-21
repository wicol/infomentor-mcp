"""Guarded HTTP primitives for InfoMentor's Swedish web-authentication flow."""

from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import urljoin, urlparse

import httpx

from infomentor_mcp.auth.session import AuthSession

HUB_ORIGIN = "https://hub.infomentor.se"
SSO_ORIGIN = "https://sso.infomentor.se"
ALLOWED_EXACT_HOSTS = {
    "infomentor.se", "hub.infomentor.se", "sso.infomentor.se", "im.infomentor.se"
}
ALLOWED_STOCKHOLM_SUFFIX = ".stockholm.se"


class InfoMentorError(Exception):
    """Base error safe to surface to CLI and MCP callers."""


class UnsafeRedirectError(InfoMentorError):
    """An authentication redirect left the explicitly permitted domains."""


class AuthenticationExpiredError(InfoMentorError):
    """The stored hub session is expired or no longer accepted."""

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


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


def is_login_redirect(response: httpx.Response) -> bool:
    location = response.headers.get("location", "")
    return response.is_redirect and ("login" in location.lower() or "stockholm" in location.lower())


class InfoMentorHttpClient:
    """An httpx client with explicit redirect and expired-session handling."""

    def __init__(self, session: AuthSession | None = None) -> None:
        self.mobile_binding = session.mobile_binding if session else None
        self.client = httpx.Client(
            follow_redirects=False,
            headers={"User-Agent": "infomentor-mcp/0.0.1", "Accept": "*/*"},
            timeout=httpx.Timeout(20.0),
        )
        if session:
            session.apply_to(self.client)

    def request_auth(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        safe_url = validate_auth_url(url)
        return self.client.request(method, safe_url, **kwargs)

    def hub_post(self, path: str, payload: object) -> httpx.Response:
        response = self.client.post(
            f"{HUB_ORIGIN}{path}",
            json=payload,
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

    def session(self) -> AuthSession:
        session = AuthSession.from_client(self.client)
        session.mobile_binding = self.mobile_binding
        return session

    def verify_authenticated_read(self) -> int:
        """Perform a minimal documented read before persisting a new session."""
        start_date = date.today()
        response = self.hub_post(
            "/timetable/timetable/appData",
            {
                "startDate": start_date.isoformat(),
                "endDate": (start_date + timedelta(days=1)).isoformat(),
            },
        )
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
