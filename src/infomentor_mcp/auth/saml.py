"""Synchronous SAML adapter for the existing data and mobile-token clients.

Interactive BankID uses the async stockholm_bankid protocol and login flows.
This adapter deliberately leaves the established synchronous read client alone.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse

import httpx

from infomentor_mcp.auth.stockholm_bankid import MAX_HOPS, SSO_LOGIN_URL, parse_login_page
from infomentor_mcp.client.http import InfoMentorError, InfoMentorHttpClient, validate_auth_url


class SamlSession:
    def __init__(self, http: InfoMentorHttpClient, emit_diagnostics: bool = True) -> None:
        self.http = http
        self.emit_diagnostics = emit_diagnostics

    def login(self, method: str = "qr") -> None:
        """Compatibility entry for the synchronous session-lifetime experiment."""
        from infomentor_mcp.login.terminal_qr import login as qr_login
        from infomentor_mcp.login.url import login as url_login

        if method not in {"qr", "url"}:
            raise ValueError("BankID method must be qr or url.")
        session = asyncio.run(url_login() if method == "url" else qr_login())
        session.apply_to(self.http.client)

    def resume_from_stockholm_sso(self) -> bool:
        response = self.http.request_auth("GET", SSO_LOGIN_URL)
        for _ in range(MAX_HOPS):
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise InfoMentorError("SSO renewal redirect had no destination.")
                response = self.http.request_auth(
                    "GET", validate_auth_url(location, str(response.url))
                )
                continue
            page = parse_login_page(response.text)
            response_form = next(
                (form for form in page.forms if "SAMLResponse" in form.fields), None
            )
            if response_form:
                response = self.http.request_auth(
                    "POST", validate_auth_url(response_form.action, str(response.url)),
                    data=response_form.fields,
                )
                self.follow_forms(response)
                return True
            request_form = next(
                (form for form in page.forms if "SAMLRequest" in form.fields), None
            )
            if request_form:
                response = self.http.request_auth(
                    "POST", validate_auth_url(request_form.action, str(response.url)),
                    data=request_form.fields,
                )
                continue
            return False
        raise InfoMentorError("SSO renewal redirect limit reached.")

    def follow_forms(self, response: httpx.Response) -> None:
        for _ in range(MAX_HOPS):
            if self.emit_diagnostics:
                print(
                    f"Auth: SAML HTTP {response.status_code} at "
                    f"{urlparse(str(response.url)).hostname}."
                )
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise InfoMentorError("SAML redirect had no destination.")
                response = self.http.request_auth(
                    "GET", validate_auth_url(location, str(response.url))
                )
                continue
            response.raise_for_status()
            page = parse_login_page(response.text)
            form = next((item for item in page.forms if any(
                key in item.fields for key in ("SAMLResponse", "SAMLRequest")
            )), None)
            if form is None and "login in progress" in response.text.lower() and page.forms:
                form = page.forms[0]
            if form is None:
                return
            response = self.http.request_auth(
                "POST", validate_auth_url(form.action, str(response.url)), data=form.fields
            )
        raise InfoMentorError("SAML redirect/form limit reached.")
