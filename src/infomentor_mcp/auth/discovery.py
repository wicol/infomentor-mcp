"""Experimental Hub mobile discovery, without device pairing or OAuth exchange.

The single candidate request is documented in family-mcp's Android Hub static
analysis. Discovery has been verified with Swedish BankID; this module itself
never performs the separate experimental pairing flow.
Only a deliberately allowlisted metadata projection may leave this module.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from pydantic import BaseModel

from infomentor_mcp.errors import MobileDiscoveryError
from infomentor_mcp.hub.transport import HUB_ORIGIN, HubTransport

DISCOVERY_PATH = "/account/pair/GetAuthenticationData"
URL_FIELDS = ("apiUrl", "authenticationUrl", "tokenUrl", "revokeRefreshTokenUrl")
EXPECTED_FIELDS = (*URL_FIELDS, "clientId", "expirationTime")
# Only fixed paths documented in the app reference can be printed. Unknown
# paths might embed an authorization reference and are never echoed.
SAFE_PATHS = {
    "", "/", "/NA1", "/NA1/", "/NA1/Authentication/sso",
    "/Authentication/Authentication/LoginOAuth2", "/Authentication/OAuth2/Token",
}


class EndpointMetadata(BaseModel):
    """Display-only URL assessment, not authorization to contact this origin."""

    valid_first_party_https: bool
    origin: str | None = None
    path: str | None = None
    path_redacted: bool = False
    query_present: bool = False
    reason: str | None = None

    @classmethod
    def inspect(cls, value: object) -> EndpointMetadata:
        """Assess a returned address without contacting it or exposing its secrets."""
        if not isinstance(value, str) or not value or any(
            char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value
        ):
            return cls(valid_first_party_https=False, reason="invalid_url")
        try:
            parsed = urlsplit(value)
            host = parsed.hostname or ""
            port = parsed.port
        except ValueError:
            return cls(valid_first_party_https=False, reason="invalid_url")
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
            or parsed.fragment
            or "\\" in value
            or not (host == "infomentor.se" or host.endswith(".infomentor.se"))
            or any(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None
                for label in host.split(".")
            )
        ):
            return cls(valid_first_party_https=False, reason="unapproved_url")
        # No discovered address is followed, even if this display-only check passes.
        safe_path = parsed.path in SAFE_PATHS
        return cls(
            valid_first_party_https=True,
            origin=f"https://{host}",
            path=parsed.path if safe_path else None,
            path_redacted=not safe_path,
            query_present=bool(parsed.query),
        )


class MobileDiscoveryReport(BaseModel):
    """Safe diagnostics: no client ID, expiration value, GUID, or token values."""

    experimental: bool = True
    http_status: int
    response_field_count: int
    recognized_fields: list[str]
    missing_fields: list[str]
    unknown_field_count: int
    nonempty_client_id_present: bool
    expiration_value_present: bool
    endpoints: dict[str, EndpointMetadata]
    complete_expected_shape: bool
    pairing_attempted: bool = False
    token_exchange_attempted: bool = False
    discovered_urls_requested: bool = False


class MobileDiscovery:
    """Experimental, metadata-only inspection of Hub's app authorization configuration."""

    def __init__(self, transport: HubTransport) -> None:
        self.transport = transport

    def run(self) -> MobileDiscoveryReport:
        """Make exactly one empty-body candidate request; never follow redirects.

        The caller must hold the shared session lock and first verify the existing
        web session. Discovery may allocate a temporary upstream authGuid; this
        method neither returns it nor persists the raw response.
        """
        response = self.transport.client.post(
            f"{HUB_ORIGIN}{DISCOVERY_PATH}",
            content=b"",
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
            follow_redirects=False,
        )
        status = response.status_code
        if response.is_redirect or status == 401:
            raise MobileDiscoveryError(
                "Discovery requires a valid Hub session; run auth login.",
                code="authentication_required", http_status=status,
            )
        if status != 200:
            raise MobileDiscoveryError(
                "Discovery did not return HTTP 200; no retries or alternate paths were attempted.",
                code="discovery_http_error", http_status=status,
            )
        if not response.content.strip():
            raise MobileDiscoveryError(
                "Discovery returned an empty body.", code="empty_response", http_status=status,
            )
        try:
            payload = response.json()
        except ValueError:
            raise MobileDiscoveryError(
                "Discovery did not return JSON; body withheld.",
                code="unexpected_response", http_status=status,
            ) from None
        if not isinstance(payload, dict):
            raise MobileDiscoveryError(
                "Discovery JSON was not an object; body withheld.",
                code="unexpected_response", http_status=status,
            )
        recognized = [name for name in EXPECTED_FIELDS if name in payload]
        endpoints = {
            name: EndpointMetadata.inspect(payload[name]) for name in URL_FIELDS if name in payload
        }
        client_id_present = (
            isinstance(payload.get("clientId"), str) and bool(payload["clientId"].strip())
        )
        expiration_present = payload.get("expirationTime") is not None
        missing = [name for name in EXPECTED_FIELDS if name not in payload]
        return MobileDiscoveryReport(
            http_status=status,
            response_field_count=len(payload),
            recognized_fields=recognized,
            missing_fields=missing,
            unknown_field_count=len(payload) - len(recognized),
            nonempty_client_id_present=client_id_present,
            expiration_value_present=expiration_present,
            endpoints=endpoints,
            complete_expected_shape=(
                not missing and client_id_present and expiration_present
                and all(endpoint.valid_first_party_https for endpoint in endpoints.values())
            ),
        )
