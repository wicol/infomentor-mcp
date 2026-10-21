"""Async Stockholm guardian SSO/BankID protocol, independent of any display."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

import httpx

from infomentor_mcp.auth.session import AuthSession
from infomentor_mcp.client.http import HUB_ORIGIN, InfoMentorError, validate_auth_url

SSO_LOGIN_URL = "https://sso.infomentor.se/login.ashx?idp=stockholm_par"
MAX_HOPS = 12


@dataclass(frozen=True)
class HtmlForm:
    action: str = field(repr=False)
    fields: dict[str, str] = field(repr=False)


class LoginPage(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[HtmlForm] = []
        self.mbid_href: str | None = None
        self._action: str | None = None
        self._fields: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "form":
            self._action = attributes.get("action") or ""
            self._fields = {}
        elif tag == "input" and self._action is not None and attributes.get("name"):
            self._fields[attributes["name"]] = attributes.get("value") or ""
        elif tag == "a" and self.mbid_href is None:
            href = attributes.get("href") or ""
            if "NECSadc/mbid" in href:
                self.mbid_href = href

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._action is not None:
            self.forms.append(HtmlForm(self._action, self._fields))
            self._action = None
            self._fields = {}


def parse_login_page(html: str) -> LoginPage:
    page = LoginPage()
    page.feed(html)
    return page


@dataclass(frozen=True)
class BankIdOrder:
    page_url: str = field(repr=False)
    reference: str = field(repr=False)
    launch_url: str | None = field(default=None, repr=False)
    qr_data: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class BankIdStatus:
    state: str
    qr_data: str | None = field(default=None, repr=False)


class StockholmBankId:
    """Concrete protocol steps: start, poll, SAML completion and verified session."""

    def __init__(self) -> None:
        self.client = httpx.AsyncClient(
            follow_redirects=False, timeout=20,
            headers={"User-Agent": "infomentor-mcp/0.0.1", "Accept": "*/*"},
        )
        self.stage = "sso"
        self.diagnostics: list[dict] = []

    async def close(self) -> None:
        await self.client.aclose()

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        response = await self.client.request(method, validate_auth_url(url), **kwargs)
        self.diagnostics.append({
            "stage": self.stage, "http_status": response.status_code,
            "host": urlparse(str(response.url)).hostname,
        })
        self.diagnostics = self.diagnostics[-32:]
        if not response.is_redirect:
            response.raise_for_status()
        return response

    async def begin(self, method: str) -> BankIdOrder:
        self.stage = "sso"
        if method not in {"url", "qr"}:
            raise ValueError("BankID method must be url or qr.")
        url = SSO_LOGIN_URL
        verb, data = "GET", None
        for _ in range(MAX_HOPS):
            response = await self.request(verb, url, data=data)
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise InfoMentorError("SSO redirect had no destination.")
                url = validate_auth_url(location, str(response.url))
                verb, data = "GET", None
                continue
            page = parse_login_page(response.text)
            form = next((item for item in page.forms if "SAMLRequest" in item.fields), None)
            if form:
                url = validate_auth_url(form.action, str(response.url))
                verb, data = "POST", form.fields
                continue
            if page.mbid_href:
                url = validate_auth_url(page.mbid_href, str(response.url))
                verb, data = "GET", None
                continue
            url = str(response.url)
            break
        else:
            raise InfoMentorError("SSO redirect limit reached.")
        self.stage = "bankid_start"
        separator = "&" if "?" in url else "?"
        initialize = "bankid" if method == "url" else "qr"
        response = await self.request("GET", f"{url}{separator}initialize={initialize}")
        try:
            payload = response.json()
            reference = payload["order"]
            value = payload["token" if method == "url" else "qrData"]
            if not all(isinstance(item, str) and item.strip() for item in (reference, value)):
                raise ValueError("Invalid order")
        except (ValueError, TypeError, KeyError):
            raise InfoMentorError(
                "Unexpected BankID initialization response; values withheld."
            ) from None
        launch_url = None
        if method == "url":
            launch_url = "https://app.bankid.com/?" + urlencode({
                "autostarttoken": value, "redirect": "null",
            })
        self.stage = "bankid_wait"
        return BankIdOrder(url, reference, launch_url, value if method == "qr" else None)

    async def poll(self, order: BankIdOrder) -> BankIdStatus:
        separator = "&" if "?" in order.page_url else "?"
        query = urlencode({"verifyorder": order.reference, "_": int(time.time() * 1000)})
        response = await self.request("GET", f"{order.page_url}{separator}{query}")
        try:
            payload = response.json()
            state = payload["state"]
            if state not in {"PENDING", "USER_SIGN", "OK", "ERROR", "CANCELLED"}:
                raise ValueError("Unknown state")
            qr_data = payload.get("qrData")
            if qr_data is not None and not isinstance(qr_data, str):
                raise ValueError("Invalid QR")
        except (ValueError, TypeError, KeyError):
            raise InfoMentorError("Unexpected BankID polling response; values withheld.") from None
        if state in {"ERROR", "CANCELLED"}:
            raise InfoMentorError("BankID order was rejected or cancelled.")
        return BankIdStatus(state, qr_data)

    async def finish(self, order: BankIdOrder) -> AuthSession:
        self.stage = "saml"
        response = await self.request("GET", order.page_url)
        for _ in range(MAX_HOPS):
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise InfoMentorError("SAML redirect had no destination.")
                response = await self.request(
                    "GET", validate_auth_url(location, str(response.url))
                )
                continue
            page = parse_login_page(response.text)
            form = next((item for item in page.forms if any(
                key in item.fields for key in ("SAMLResponse", "SAMLRequest")
            )), None)
            if form is None and "login in progress" in response.text.lower() and page.forms:
                form = page.forms[0]
            if form is None:
                break
            response = await self.request(
                "POST", validate_auth_url(form.action, str(response.url)), data=form.fields
            )
        else:
            raise InfoMentorError("SAML form/redirect limit reached.")
        self.stage = "verify"
        start = datetime.now(ZoneInfo("Europe/Stockholm")).date()
        response = await self.request(
            "POST", HUB_ORIGIN + "/timetable/timetable/appData",
            json={
                "startDate": start.isoformat(), "endDate": (start + timedelta(days=1)).isoformat(),
            },
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
        )
        try:
            payload = response.json()
            if response.is_redirect or not isinstance(payload, dict):
                raise ValueError("Not authenticated")
            if not isinstance(payload.get("items"), list):
                raise ValueError("Unexpected timetable")
        except ValueError:
            raise InfoMentorError(
                "New session did not pass the read-only Hub verification."
            ) from None
        return AuthSession.from_client(self.client)
