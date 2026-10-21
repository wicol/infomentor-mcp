"""Separate, private storage for experimental Hub mobile credentials."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import keyring
from pydantic import BaseModel, Field, SecretStr

from infomentor_mcp.auth.session import SERVICE_NAME, SessionStore

MOBILE_ACCOUNT = "swedish-guardian-mobile"


class MobileCredentials(BaseModel):
    access_token: SecretStr
    refresh_token: SecretStr
    device_id: str
    binding: str
    children_fingerprint: str
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_in: int | None = None
    verified: bool = False
    revocation_acknowledged: bool = False


class MobileCredentialStore:
    """Use a distinct keyring entry/file, coordinated by the web-session lock."""

    def __init__(self, web_store: SessionStore) -> None:
        self.web_store = web_store
        self.path = web_store.fallback_path.with_name("mobile-credentials.json")

    def load(self) -> MobileCredentials | None:
        with self.web_store.locked():
            try:
                text = keyring.get_password(SERVICE_NAME, MOBILE_ACCOUNT)
            except keyring.errors.KeyringError:
                text = None
            if text is None:
                try:
                    text = self.path.read_text(encoding="utf-8")
                except FileNotFoundError:
                    return None
            return MobileCredentials.model_validate_json(text) if text else None

    def save(self, credentials: MobileCredentials) -> None:
        # SecretStr intentionally redacts ordinary dumps. Only this storage
        # boundary may serialize actual credential values.
        data = credentials.model_dump(mode="json")
        data["access_token"] = credentials.access_token.get_secret_value()
        data["refresh_token"] = credentials.refresh_token.get_secret_value()
        text = json.dumps(data)
        with self.web_store.locked():
            try:
                keyring.set_password(SERVICE_NAME, MOBILE_ACCOUNT, text)
            except keyring.errors.KeyringError:
                pass
            else:
                self.path.unlink(missing_ok=True)
                return
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(dir=self.path.parent, prefix=".mobile-")
            temporary = Path(name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(text)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(self.path)
            finally:
                temporary.unlink(missing_ok=True)

    def clear_after_revocation(self) -> None:
        """Only the successful remote-revocation flow should call this method."""
        with self.web_store.locked():
            try:
                keyring.delete_password(SERVICE_NAME, MOBILE_ACCOUNT)
            except keyring.errors.PasswordDeleteError:
                pass
            except keyring.errors.KeyringError:
                # Do not claim credentials are gone when the keyring is inaccessible.
                if self.path.exists():
                    self.path.unlink()
                    return
                raise
            self.path.unlink(missing_ok=True)
