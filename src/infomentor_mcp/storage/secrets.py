"""One secret in the OS keyring, with a private file fallback when no keyring is available."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import keyring
import keyring.errors

SERVICE_NAME = "infomentor-mcp"


class SecretStore:
    """Unlocked persistence; callers coordinate through the shared session lock."""

    def __init__(self, account: str, fallback_path: Path) -> None:
        self.account = account
        self.path = fallback_path

    def read(self) -> str | None:
        try:
            text = keyring.get_password(SERVICE_NAME, self.account)
        except keyring.errors.KeyringError:
            text = None
        if text is not None:
            return text
        try:
            return self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def write(self, text: str) -> None:
        try:
            keyring.set_password(SERVICE_NAME, self.account, text)
        except keyring.errors.KeyringError:
            pass
        else:
            self.path.unlink(missing_ok=True)
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.stem}-")
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def delete(self, *, strict: bool = False) -> None:
        """Remove both copies. Strict deletion refuses to claim success while an
        inaccessible keyring may still hold the secret."""
        try:
            keyring.delete_password(SERVICE_NAME, self.account)
        except keyring.errors.PasswordDeleteError:
            pass
        except keyring.errors.KeyringError:
            if strict and not self.path.exists():
                raise
        self.path.unlink(missing_ok=True)
