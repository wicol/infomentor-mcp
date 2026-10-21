"""Keep all tests away from real sessions, OS keyrings, and school records."""

import keyring.errors
import pytest


@pytest.fixture(autouse=True)
def isolated_session_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    def unavailable(*args, **kwargs):
        raise keyring.errors.NoKeyringError("Synthetic test backend unavailable")

    monkeypatch.setattr("keyring.get_password", unavailable)
    monkeypatch.setattr("keyring.set_password", unavailable)
    monkeypatch.setattr("keyring.delete_password", unavailable)
