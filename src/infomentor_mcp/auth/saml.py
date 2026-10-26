"""SAML login pages and redirect/form chains between approved hosts only.

The hop logic is shared by the synchronous SSO renewal and the async BankID login.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

from infomentor_mcp.errors import InfoMentorError
from infomentor_mcp.hub.transport import HubTransport, validate_auth_url

SSO_LOGIN_URL = "https://sso.infomentor.se/login.ashx?idp=stockholm_par"
MAX_HOPS = 12


@dataclass(frozen=True)
class HtmlForm:
    action: str = field(repr=False)
    fields: dict[str, str] = field(repr=False)


class LoginPage(HTMLParser):
    def __init__(self, html: str = "") -> None:
        super().__init__()
        self.forms: list[HtmlForm] = []
        self.mbid_href: str | None = None
        self._action: str | None = None
        self._fields: dict[str, str] = {}
        self.feed(html)

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

    def form_with(self, *names: str) -> HtmlForm | None:
        return next((form for form in self.forms if any(n in form.fields for n in names)), None)


class SamlFlow:
    """Synchronous SAML hops over a HubTransport, without printing anything secret."""

    def __init__(self, transport: HubTransport, *, emit_diagnostics: bool = True) -> None:
        self.transport = transport
        self.emit_diagnostics = emit_diagnostics

    @staticmethod
    def next_request(response: httpx.Response) -> tuple[str, str, dict | None] | None:
        """The next validated hop after a SAML response, or None when the chain is done."""
        if response.is_redirect:
            location = response.headers.get("location")
            if not location:
                raise InfoMentorError("SAML redirect had no destination.")
            return "GET", validate_auth_url(location, str(response.url)), None
        response.raise_for_status()
        page = LoginPage(response.text)
        form = page.form_with("SAMLResponse", "SAMLRequest")
        if form is None and "login in progress" in response.text.lower() and page.forms:
            form = page.forms[0]
        if form is None:
            return None
        return "POST", validate_auth_url(form.action, str(response.url)), form.fields

    def follow(self, response: httpx.Response) -> None:
        for _ in range(MAX_HOPS):
            if self.emit_diagnostics:
                print(
                    f"Auth: SAML HTTP {response.status_code} at "
                    f"{urlparse(str(response.url)).hostname}."
                )
            hop = self.next_request(response)
            if hop is None:
                return
            method, url, data = hop
            response = self.transport.request_auth(method, url, data=data)
        raise InfoMentorError("SAML redirect/form limit reached.")

    def resume_stockholm_sso(self) -> bool:
        """Mint a new Hub session from Stockholm SSO cookies; False when BankID is required."""
        response = self.transport.request_auth("GET", SSO_LOGIN_URL)
        for _ in range(MAX_HOPS):
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise InfoMentorError("SSO renewal redirect had no destination.")
                response = self.transport.request_auth(
                    "GET", validate_auth_url(location, str(response.url))
                )
                continue
            page = LoginPage(response.text)
            if form := page.form_with("SAMLResponse"):
                response = self.transport.request_auth(
                    "POST", validate_auth_url(form.action, str(response.url)), data=form.fields,
                )
                self.follow(response)
                return True
            if form := page.form_with("SAMLRequest"):
                response = self.transport.request_auth(
                    "POST", validate_auth_url(form.action, str(response.url)), data=form.fields,
                )
                continue
            return False
        raise InfoMentorError("SSO renewal redirect limit reached.")
