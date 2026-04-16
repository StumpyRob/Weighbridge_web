from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.services.secrets import decrypt_string, encrypt_string


def test_encrypt_decrypt_string_round_trip(monkeypatch) -> None:
    monkeypatch.setattr(
        settings,
        "app_encryption_key",
        Fernet.generate_key().decode("ascii"),
    )

    encrypted = encrypt_string("quickbooks-refresh-token")

    assert encrypted
    assert encrypted != "quickbooks-refresh-token"
    assert decrypt_string(encrypted) == "quickbooks-refresh-token"


def test_encrypt_string_requires_app_encryption_key(monkeypatch) -> None:
    monkeypatch.setattr(settings, "app_encryption_key", "")

    with pytest.raises(RuntimeError, match="APP_ENCRYPTION_KEY"):
        encrypt_string("secret-value")
