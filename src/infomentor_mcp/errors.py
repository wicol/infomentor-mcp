"""Errors safe to surface to CLI and MCP callers, each with a stable response code."""

from __future__ import annotations


class InfoMentorError(Exception):
    """Base error whose message is safe to show; never put secrets or school data in it."""

    code = "infomentor_request_failed"

    def __init__(
        self, message: str, *, code: str | None = None, http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.http_status = http_status

    def as_response(self) -> dict:
        return {"error": {"code": self.code, "message": str(self)}}


class InvalidInput(InfoMentorError, ValueError):
    """Rejected parameters, raised before any InfoMentor request is made."""

    code = "invalid_input"


class UnknownChild(InvalidInput):
    code = "invalid_child"


class ChangeTrackingError(InvalidInput):
    """Sanitized change-tracking parameter or state error."""


class SessionStorageError(InfoMentorError):
    code = "session_storage_error"

    def __init__(self) -> None:
        super().__init__("Cannot read or save local session.")


class UnsafeRedirectError(InfoMentorError):
    """An authentication redirect left the explicitly permitted domains."""


class AuthenticationExpiredError(InfoMentorError):
    """The stored hub session is expired or no longer accepted."""

    code = "authentication_required"


class UnexpectedInfoMentorResponse(InfoMentorError):
    """Hub responded successfully but not in the documented shape."""


class MobileFlowError(InfoMentorError):
    """Safe error with stage/status only, never request or response secrets."""

    code = "mobile_authorization_failed"


class MobileDiscoveryError(InfoMentorError):
    """A metadata-only error; never include upstream payloads or URLs."""
