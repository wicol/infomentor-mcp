"""Separate, private storage for Hub mobile credentials."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from pydantic import BaseModel, Field, SecretStr

from infomentor_mcp.storage.secrets import SecretStore
from infomentor_mcp.storage.session import SessionStore

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

    def bearer(self) -> dict[str, str]:
        return {"Authorization": "Bearer " + self.access_token.get_secret_value()}

    def secret_json(self) -> str:
        """SecretStr redacts ordinary dumps; only the storage boundary serializes values."""
        data = self.model_dump(mode="json")
        data["access_token"] = self.access_token.get_secret_value()
        data["refresh_token"] = self.refresh_token.get_secret_value()
        return json.dumps(data)


class MobileCredentialStore:
    """A distinct keyring entry/file, coordinated by the web-session lock."""

    def __init__(self, web_store: SessionStore) -> None:
        self.web_store = web_store
        self.path = web_store.state_dir / "mobile-credentials.json"
        self._secret = SecretStore(MOBILE_ACCOUNT, self.path)

    def load(self) -> MobileCredentials | None:
        with self.web_store.locked():
            text = self._secret.read()
            return MobileCredentials.model_validate_json(text) if text else None

    def save(self, credentials: MobileCredentials) -> None:
        with self.web_store.locked():
            self._secret.write(credentials.secret_json())

    def clear_after_revocation(self) -> None:
        """Only the successful remote-revocation flow should call this method."""
        with self.web_store.locked():
            self._secret.delete(strict=True)
