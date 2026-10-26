"""Renew an expired Hub session without BankID."""

from __future__ import annotations

from infomentor_mcp.auth.mobile import MobileAuthorization
from infomentor_mcp.auth.saml import SamlFlow
from infomentor_mcp.errors import AuthenticationExpiredError, InfoMentorError
from infomentor_mcp.hub.transport import HubTransport
from infomentor_mcp.storage.session import AuthSession, SessionStore


class Renewal:
    """Use the bound mobile authorization when there is one, else Stockholm SSO cookies.

    Never starts a BankID order; both paths persist the renewed session themselves.
    """

    def __init__(self, store: SessionStore) -> None:
        self.store = store

    def renew(self, current: AuthSession) -> AuthSession:
        if current.mobile_binding is not None:
            try:
                with MobileAuthorization(self.store) as authorization:
                    return authorization.renew()
            except (ValueError, OSError):
                raise InfoMentorError(
                    "Cannot read or persist mobile authorization; details withheld."
                ) from None
        with HubTransport(current.stockholm_sso_only()) as sso:
            if not SamlFlow(sso, emit_diagnostics=False).resume_stockholm_sso():
                raise AuthenticationExpiredError(
                    "InfoMentor and Stockholm SSO sessions have expired; run auth login again."
                )
            sso.verify_authenticated_read()
            renewed = sso.session()
            self.store.save(renewed)
            return renewed
