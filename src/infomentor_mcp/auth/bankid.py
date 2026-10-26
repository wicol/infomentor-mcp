"""Async Stockholm guardian SSO/BankID protocol, independent of any display."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlencode, urlparse

import httpx

from infomentor_mcp.auth.saml import MAX_HOPS, SSO_LOGIN_URL, LoginPage, SamlFlow
from infomentor_mcp.errors import InfoMentorError
from infomentor_mcp.hub.transport import (
    HUB_ORIGIN,
    USER_AGENT_HEADERS,
    HubTransport,
    validate_auth_url,
)
from infomentor_mcp.storage.session import AuthSession

APPROVAL_TIMEOUT_SECONDS = 240


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


class ApprovalView(Protocol):
    """Shows the animated QR code and login status while waiting for approval."""

    def show(self, qr_data: str | None, status: str) -> None: ...


class StockholmBankId:
    """Concrete protocol steps: start, poll, SAML completion and verified session."""

    def __init__(self) -> None:
        self.client = httpx.AsyncClient(
            follow_redirects=False, timeout=20, headers=USER_AGENT_HEADERS,
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
            page = LoginPage(response.text)
            if form := page.form_with("SAMLRequest"):
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

    async def wait(self, order: BankIdOrder, view: ApprovalView | None = None) -> AuthSession:
        """Poll until approved, keeping an optional view's QR code and status current."""
        qr_data = order.qr_data
        if view:
            view.show(qr_data, "waiting")
        async with asyncio.timeout(APPROVAL_TIMEOUT_SECONDS):
            while True:
                await asyncio.sleep(1)
                status = await self.poll(order)
                if status.state == "OK":
                    if view:
                        view.show(None, "verifying")
                    return await self.finish(order)
                qr_data = status.qr_data or qr_data
                if view:
                    view.show(qr_data, "user_sign" if status.state == "USER_SIGN" else "waiting")

    async def finish(self, order: BankIdOrder) -> AuthSession:
        self.stage = "saml"
        response = await self.request("GET", order.page_url)
        for _ in range(MAX_HOPS):
            hop = SamlFlow.next_request(response)
            if hop is None:
                break
            method, url, data = hop
            response = await self.request(method, url, data=data)
        else:
            raise InfoMentorError("SAML redirect/form limit reached.")
        self.stage = "verify"
        response = await self.request(
            "POST", HUB_ORIGIN + HubTransport.VERIFY_PATH,
            json=HubTransport.verification_payload(),
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
