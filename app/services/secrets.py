from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from ..config import settings


def _fernet() -> Fernet:
    raw_key = str(settings.app_encryption_key or "").strip()
    if not raw_key:
        raise RuntimeError("APP_ENCRYPTION_KEY is not configured.")
    try:
        return Fernet(raw_key.encode("ascii"))
    except Exception as exc:  # pragma: no cover - library validation path
        raise RuntimeError(
            "APP_ENCRYPTION_KEY must be a valid Fernet key."
        ) from exc


def encrypt_string(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    return _fernet().encrypt(text.encode("utf-8")).decode("ascii")


def decrypt_string(value: str) -> str:
    token = str(value or "").strip()
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Encrypted value could not be decrypted.") from exc
