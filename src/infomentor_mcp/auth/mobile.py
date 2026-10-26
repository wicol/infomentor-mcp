"""Hub app authorization: explicit pairing, guarded renewal, tests and revocation.

Only the Swedish discovery-verified origins are contacted. No push registration
or school-record writes are performed. Explicit login includes mobile setup;
renew() only restores an existing, bound authorization without creating a grant.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx

from infomentor_mcp.auth.discovery import DISCOVERY_PATH
from infomentor_mcp.auth.saml import SamlFlow
from infomentor_mcp.errors import AuthenticationExpiredError, MobileFlowError
from infomentor_mcp.hub.pupils import parse_child_ids
from infomentor_mcp.hub.transport import HUB_ORIGIN, HubTransport
from infomentor_mcp.storage.credentials import MobileCredentials, MobileCredentialStore
from infomentor_mcp.storage.session import AuthSession, SessionStore

AUTH_ORIGIN = "https://im.infomentor.se"
API_ORIGIN = "https://api-im.infomentor.se"
AUTH_PATH = "/Authentication/Authentication/LoginOAuth2"
TOKEN_PATH = "/Authentication/OAuth2/Token"
REVOKE_PATH = "/Authentication/OAuth2/RevokeRefreshToken"
SSO_BRIDGE_PATH = "/NA1/Authentication/sso"
CALLBACK = "InfomentorNotification://oauth2Callback"
CLIENT_ID = "notificationapp"
SCOPE = "IM2-API-NOTIFICATION"
KNOWN_OAUTH_ERRORS = {
    "invalid_grant", "invalid_token", "invalid_request", "invalid_client",
    "unauthorized_client", "access_denied", "invalid_refresh_token",
}


class MobileAuthorization:
    """Explicit authorization plus guarded reuse of a bound mobile credential.

    The caller holds the session lock for every operation.
    """

    def __init__(self, store: SessionStore) -> None:
        self.store = store
        self.tokens = MobileCredentialStore(store)
        # Separate transport: no guardian web cookies, no redirect following.
        self.transport = httpx.Client(follow_redirects=False, timeout=20)

    def __enter__(self) -> MobileAuthorization:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        self.transport.close()

    def complete_login(self) -> None:
        """Bind mobile access after an explicitly requested, fresh BankID login.

        Never rebind an old grant: matching children do not prove guardian identity.
        Existing credentials must pass revocation/cleanup before replacement.
        """
        credentials = self.tokens.load()
        if credentials is None:
            self.pair()
            return
        session = self.store.load()
        if session is None:
            raise MobileFlowError("No verified web session is stored.")
        with HubTransport(session) as web:
            web.verify_authenticated_read()
            if self._children_fingerprint(web) != credentials.children_fingerprint:
                raise MobileFlowError(
                    "Existing mobile authorization has a different child scope; retained. "
                    "Revoke it explicitly before logging in to this account again."
                )
        if not credentials.revocation_acknowledged:
            try:
                # Refresh only to obtain bearer access for revocation, never to
                # transplant the old guardian's Hub session into the new login.
                self._refresh(credentials)
            except AuthenticationExpiredError as error:
                if error.http_status not in {400, 401}:
                    raise
        self.revoke()
        self.pair()

    def pair(self) -> None:
        if self.tokens.load() is not None:
            raise MobileFlowError(
                "Mobile credentials already exist; revoke them before pairing again."
            )
        session = self.store.load()
        if session is None:
            raise MobileFlowError("No saved web session; run auth login first.")
        with HubTransport(session) as web:
            web.verify_authenticated_read()
            fingerprint = self._children_fingerprint(web)
            guid = self._authorization_reference(web)
            device_id = str(uuid4())
            response = self.transport.get(AUTH_ORIGIN + AUTH_PATH, params={
                "authGuid": guid,
                "DeviceIdentifier": device_id,
                "DeviceFriendlyName": "InfoMentor MCP (experimental local client)",
                "DevicePlatform": "Android",
                "scope": SCOPE, "redirect_uri": CALLBACK,
                "client_id": CLIENT_ID, "response_type": "code", "isSandBox": "false",
            })
            self._require_status(response, 302, "app authorization")
            code = self._authorization_code(response)
            pending = MobileCredentials(
                access_token="pending", refresh_token="pending", device_id=device_id,
                binding=str(uuid4()), children_fingerprint=fingerprint,
            )
            response = self.transport.post(AUTH_ORIGIN + TOKEN_PATH, data={
                "code": code, "client_id": CLIENT_ID, "client_secret": "NONE",
                "redirect_uri": CALLBACK, "grant_type": "authorization_code",
            })
            credentials = self._read_tokens(response, pending)
            # Save immediately: later verification failures must not strand a
            # granted credential without a local revocation path.
            self.tokens.save(credentials)
            verified_web = web.session()
            verified_web.mobile_binding = credentials.binding
            self.store.save(verified_web)
            self._restore_hub(credentials)

    def test(self, *, refresh: bool = False, after_expiry: bool = False) -> None:
        credentials = self.tokens.load()
        if credentials is None:
            raise MobileFlowError("No paired mobile credentials exist.")
        session = self.store.load()
        if session is None or session.mobile_binding != credentials.binding:
            raise MobileFlowError(
                "Current web session is not bound to this pairing; refusing replacement."
            )
        if after_expiry:
            self._await_access_expiry(credentials)
            refresh = True
        if refresh:
            credentials = self._refresh(credentials)
        self._restore_hub(credentials)

    def renew(self) -> AuthSession:
        """Restore an existing, bound pairing without printing or creating a grant."""
        with self.store.locked():
            credentials = self.tokens.load()
            session = self.store.load()
            if credentials is None or session is None:
                raise AuthenticationExpiredError(
                    "Missing mobile/web session; explicit login is required."
                )
            if session.mobile_binding != credentials.binding:
                raise AuthenticationExpiredError(
                    "Mobile authorization is not bound to this login; refusing session replacement."
                )
            if credentials.revocation_acknowledged:
                raise AuthenticationExpiredError(
                    "Mobile authorization has a pending revocation; "
                    "complete it before reauthorizing."
                )
            return self._restore_hub(credentials, refresh_on_unauthorized=True)

    def revoke(self) -> None:
        credentials = self.tokens.load()
        if credentials is None:
            raise MobileFlowError("No paired mobile credentials exist.")
        if not credentials.revocation_acknowledged:
            response = self.transport.get(AUTH_ORIGIN + REVOKE_PATH, params={
                "refreshToken": credentials.refresh_token.get_secret_value(),
            }, headers=credentials.bearer())
            if response.status_code not in {200, 204, 400, 401}:
                raise MobileFlowError(
                    f"Revocation failed (HTTP {response.status_code}); local credentials retained."
                )
            if response.status_code in {200, 204} and response.content.strip():
                try:
                    result = response.json()
                except ValueError:
                    raise MobileFlowError(
                        "Revocation response is ambiguous; credentials retained."
                    ) from None
                if result is not True:
                    raise MobileFlowError("Revocation was not confirmed; credentials retained.")
            if response.status_code in {200, 204}:
                credentials.revocation_acknowledged = True
                self.tokens.save(credentials)
        # A successful revoke response alone is not proof. Repeated revocation
        # can also return 400/401 for an already unusable credential. Check that the old
        # refresh token is now rejected. If it unexpectedly rotates, preserve
        # the replacement so the newly granted credential is not stranded.
        check = self._refresh_request(credentials)
        if check.status_code == 200:
            self.tokens.save(self._read_tokens(check, credentials))
            raise MobileFlowError("Revoked token still renewed; rotated credentials retained.")
        try:
            rejected = check.json().get("error")
        except (ValueError, AttributeError):
            rejected = None
        confirmed_rejection = (
            rejected == "invalid_grant"
            or not check.content.strip()
            or (check.status_code == 400 and check.content.strip().lower() == b"bad request")
        )
        if check.status_code not in {400, 401} or not confirmed_rejection:
            raise MobileFlowError(
                "Revocation effectiveness is inconclusive "
                f"(HTTP {check.status_code}); credentials retained."
            )
        self.tokens.clear_after_revocation()

    def _await_access_expiry(self, credentials: MobileCredentials) -> None:
        """Wait out the advertised access-token lifetime and require Hub to reject it."""
        if credentials.expires_in is None or not 0 < credentials.expires_in <= 86400:
            raise MobileFlowError(
                "No supported access-token expiry available for an unattended test."
            )
        age = (datetime.now(UTC) - credentials.received_at).total_seconds()
        delay = max(0, credentials.expires_in - age) + 5
        print(
            f"Waiting {int(delay) + 1} seconds for advertised access-token expiry; "
            "the session lock remains held. No keepalive requests will be made.",
            flush=True,
        )
        time.sleep(delay)
        response = self.transport.get(API_ORIGIN + SSO_BRIDGE_PATH, headers=credentials.bearer())
        if response.status_code != 401:
            raise MobileFlowError(
                "Expiry test inconclusive: old access token returned "
                f"HTTP {response.status_code}, not 401; no expiry-renewal claim made."
            )
        print("Expired access token rejected with HTTP 401; testing refresh grant.", flush=True)

    def _refresh_request(self, credentials: MobileCredentials) -> httpx.Response:
        return self.transport.post(AUTH_ORIGIN + TOKEN_PATH, data={
            "refresh_token": credentials.refresh_token.get_secret_value(),
            "client_id": CLIENT_ID, "client_secret": "NONE",
            "grant_type": "refresh_token", "scope": SCOPE,
        })

    def _refresh(self, credentials: MobileCredentials) -> MobileCredentials:
        response = self._refresh_request(credentials)
        if response.status_code in {400, 401}:
            raise AuthenticationExpiredError(
                "Stored mobile authorization was rejected; "
                "explicit BankID reauthorization is required.",
                http_status=response.status_code,
            )
        rotated = self._read_tokens(response, credentials)
        self.tokens.save(rotated)
        return rotated

    def _restore_hub(
        self, credentials: MobileCredentials, *, refresh_on_unauthorized: bool = False
    ) -> AuthSession:
        """Exchange bearer access for a fresh, verified Hub web session of the same scope."""
        response = self.transport.get(API_ORIGIN + SSO_BRIDGE_PATH, headers=credentials.bearer())
        if response.status_code == 401 and refresh_on_unauthorized:
            credentials = self._refresh(credentials)
            response = self.transport.get(
                API_ORIGIN + SSO_BRIDGE_PATH, headers=credentials.bearer(),
            )
            if response.status_code == 401:
                raise AuthenticationExpiredError(
                    "Mobile access was rejected after one refresh; "
                    "explicit reauthorization is required.",
                    http_status=401,
                )
        self._require_status(response, 200, "mobile SSO bridge")
        url = self._approved_sign_in_url(response)
        with HubTransport() as web:
            # Never forward a bearer token into the web sign-in redirect chain.
            SamlFlow(web, emit_diagnostics=False).follow(web.request_auth("GET", url))
            web.verify_authenticated_read()
            if self._children_fingerprint(web) != credentials.children_fingerprint:
                raise MobileFlowError(
                    "New Hub session has a different linked-child scope; not saved."
                )
            session = web.session()
            session.mobile_binding = credentials.binding
            self.store.save(session)
            credentials.verified = True
            self.tokens.save(credentials)
            return session

    @staticmethod
    def _require_status(response: httpx.Response, status: int, stage: str) -> None:
        if response.status_code == status:
            return
        try:
            error = response.json().get("error")
        except (ValueError, AttributeError):
            error = None
        kind = error if isinstance(error, str) and error in KNOWN_OAUTH_ERRORS else (
            "unrecognized_or_absent"
        )
        raise MobileFlowError(
            f"Mobile flow stopped at {stage} (HTTP {response.status_code}, OAuth error: {kind})."
        )

    def _children_fingerprint(self, web: HubTransport) -> str:
        response = web.client.get(HUB_ORIGIN + "/", follow_redirects=False)
        self._require_status(response, 200, "child-scope verification")
        child_ids = sorted(parse_child_ids(response.text))
        if not child_ids:
            raise MobileFlowError("Cannot establish the linked-child scope; stopping mobile flow.")
        return hashlib.sha256(json.dumps(child_ids).encode()).hexdigest()

    def _authorization_reference(self, web: HubTransport) -> str:
        """Check Hub's discovery against the verified Swedish app configuration."""
        response = web.client.post(HUB_ORIGIN + DISCOVERY_PATH, content=b"", follow_redirects=False)
        self._require_status(response, 200, "discovery")
        try:
            payload = response.json()
            expected = {
                "authenticationUrl": (AUTH_ORIGIN, AUTH_PATH),
                "tokenUrl": (AUTH_ORIGIN, TOKEN_PATH),
                "revokeRefreshTokenUrl": (AUTH_ORIGIN, REVOKE_PATH),
                "apiUrl": (API_ORIGIN, ""),
            }
            for name, (origin, path) in expected.items():
                parsed = urlsplit(payload[name])
                if (
                    f"{parsed.scheme}://{parsed.netloc}" != origin
                    or parsed.fragment
                    or parsed.path.rstrip("/") != path
                ):
                    raise ValueError("Unexpected endpoint")
            if payload["clientId"] != CLIENT_ID:
                raise ValueError("Unexpected client")
            guids = parse_qs(urlsplit(payload["authenticationUrl"]).query).get("authGuid", [])
            if len(guids) != 1 or not guids[0]:
                raise ValueError("Missing authorization reference")
            return guids[0]
        except (ValueError, TypeError, KeyError, AttributeError):
            raise MobileFlowError(
                "Discovery did not match the verified Swedish app configuration."
            ) from None

    @staticmethod
    def _authorization_code(response: httpx.Response) -> str:
        try:
            callback = urlsplit(response.headers["location"])
            query = parse_qs(callback.query)
            if (
                callback.scheme != "infomentornotification"
                or callback.netloc.lower() != "oauth2callback"
                or callback.path not in {"", "/"}
                or callback.fragment
                or "error" in query
                or len(query.get("code", [])) != 1
            ):
                raise ValueError("Unexpected callback")
            return query["code"][0]
        except (KeyError, ValueError):
            raise MobileFlowError(
                "App authorization did not return the expected code callback."
            ) from None

    def _read_tokens(
        self, response: httpx.Response, previous: MobileCredentials,
    ) -> MobileCredentials:
        self._require_status(response, 200, "token exchange")
        try:
            payload = response.json()
            access, refresh = payload["access_token"], payload["refresh_token"]
            if not all(isinstance(value, str) and value.strip() for value in (access, refresh)):
                raise ValueError("Missing token")
            if str(payload.get("token_type", "Bearer")).lower() != "bearer":
                raise ValueError("Unexpected token type")
            expires = payload.get("expires_in")
            if expires is not None:
                expires = int(expires)
                if expires < 0:
                    raise ValueError("Invalid expiry")
            return MobileCredentials(
                access_token=access, refresh_token=refresh,
                device_id=previous.device_id, binding=previous.binding,
                children_fingerprint=previous.children_fingerprint,
                expires_in=expires, received_at=datetime.now(UTC), verified=False,
            )
        except (ValueError, TypeError, KeyError, AttributeError):
            raise MobileFlowError("Token response was unexpected; body withheld.") from None

    @staticmethod
    def _approved_sign_in_url(response: httpx.Response) -> str:
        try:
            url = response.json()
        except ValueError:
            url = response.text.strip()
        if not isinstance(url, str):
            raise MobileFlowError("SSO bridge did not return a sign-in URL; response withheld.")
        try:
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or parsed.netloc not in {"im.infomentor.se", "infomentor.se", "hub.infomentor.se"}
                or parsed.fragment or "\\" in url
                or any(char.isspace() or ord(char) < 32 for char in url)
            ):
                raise ValueError("Unapproved sign-in origin")
        except ValueError:
            raise MobileFlowError(
                "SSO bridge sign-in URL was not approved; not followed."
            ) from None
        return url
