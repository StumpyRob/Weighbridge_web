from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import datetime

from cryptography import x509
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import PlatformSetting
from ..models.base import utcnow


class PlatformQzCredentialError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlatformQzCredentialState:
    certificate_configured: bool = False
    private_key_configured: bool = False
    certificate_updated_at: datetime | None = None
    private_key_updated_at: datetime | None = None


@dataclass(frozen=True)
class PlatformQzCredentialUpdateResult:
    state: PlatformQzCredentialState
    certificate_changed: bool = False
    certificate_cleared: bool = False
    private_key_changed: bool = False
    private_key_cleared: bool = False

    @property
    def any_changes(self) -> bool:
        return any(
            (
                self.certificate_changed,
                self.certificate_cleared,
                self.private_key_changed,
                self.private_key_cleared,
            )
        )


def normalize_qz_credential_text(value: str | None) -> str:
    normalized = str(value or "").replace("\r\n", "\n").strip()
    if not normalized:
        return ""
    return normalized.replace("\\n", "\n")


def validate_qz_certificate_text(certificate_text: str) -> None:
    if not certificate_text:
        raise PlatformQzCredentialError("QZ certificate is empty.")
    try:
        x509.load_pem_x509_certificate(certificate_text.encode("utf-8"))
    except Exception as exc:
        raise PlatformQzCredentialError(f"QZ certificate is invalid: {exc}") from exc


def validate_qz_private_key_pem(private_key_pem: str) -> None:
    if not private_key_pem:
        raise PlatformQzCredentialError("QZ private key is empty.")
    try:
        private_key = serialization.load_pem_private_key(
            private_key_pem.encode("utf-8"),
            password=None,
        )
    except Exception as exc:
        raise PlatformQzCredentialError(f"QZ private key is invalid: {exc}") from exc
    if not isinstance(private_key, RSAPrivateKey):
        raise PlatformQzCredentialError("QZ private key must be an RSA private key.")


def _platform_setting_row(db: Session) -> PlatformSetting | None:
    return db.execute(
        select(PlatformSetting).order_by(PlatformSetting.id.asc()).limit(1)
    ).scalars().first()


def _platform_setting_row_for_write(db: Session) -> PlatformSetting:
    row = _platform_setting_row(db)
    if row is None:
        row = PlatformSetting()
        db.add(row)
    return row


def _cipher() -> Fernet:
    secret = str(settings.effective_secret_key or "").strip()
    if not secret:
        raise PlatformQzCredentialError(
            "App secret key is not configured, so platform QZ credentials cannot be stored."
        )
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _encrypt_qz_credential(value: str) -> str:
    return _cipher().encrypt(value.encode("utf-8")).decode("ascii")


def _decrypt_qz_credential(value: str) -> str:
    try:
        payload = _cipher().decrypt(value.encode("ascii"))
    except InvalidToken as exc:
        raise PlatformQzCredentialError(
            "Stored QZ credentials could not be decrypted with the current app secret key."
        ) from exc
    return payload.decode("utf-8")


def get_platform_qz_credential_state(db: Session) -> PlatformQzCredentialState:
    row = _platform_setting_row(db)
    if row is None:
        return PlatformQzCredentialState()
    return PlatformQzCredentialState(
        certificate_configured=bool(str(row.qz_certificate_encrypted or "").strip()),
        private_key_configured=bool(str(row.qz_private_key_encrypted or "").strip()),
        certificate_updated_at=row.qz_certificate_updated_at,
        private_key_updated_at=row.qz_private_key_updated_at,
    )


def load_platform_qz_certificate_text(db: Session) -> str | None:
    row = _platform_setting_row(db)
    encrypted = str(getattr(row, "qz_certificate_encrypted", "") or "").strip()
    if not encrypted:
        return None
    certificate_text = normalize_qz_credential_text(_decrypt_qz_credential(encrypted))
    validate_qz_certificate_text(certificate_text)
    return certificate_text


def load_platform_qz_private_key_pem(db: Session) -> str | None:
    row = _platform_setting_row(db)
    encrypted = str(getattr(row, "qz_private_key_encrypted", "") or "").strip()
    if not encrypted:
        return None
    private_key_pem = normalize_qz_credential_text(_decrypt_qz_credential(encrypted))
    validate_qz_private_key_pem(private_key_pem)
    return private_key_pem


def save_platform_qz_credentials(
    db: Session,
    *,
    certificate_text: str | None = None,
    private_key_text: str | None = None,
    clear_certificate: bool = False,
    clear_private_key: bool = False,
) -> PlatformQzCredentialUpdateResult:
    normalized_certificate = normalize_qz_credential_text(certificate_text)
    normalized_private_key = normalize_qz_credential_text(private_key_text)
    if clear_certificate and normalized_certificate:
        raise PlatformQzCredentialError(
            "Choose either a replacement certificate or clear the saved certificate, not both."
        )
    if clear_private_key and normalized_private_key:
        raise PlatformQzCredentialError(
            "Choose either a replacement private key or clear the saved private key, not both."
        )
    if normalized_certificate:
        validate_qz_certificate_text(normalized_certificate)
    if normalized_private_key:
        validate_qz_private_key_pem(normalized_private_key)

    row = _platform_setting_row_for_write(db)
    now = utcnow()
    certificate_changed = False
    certificate_cleared = False
    private_key_changed = False
    private_key_cleared = False

    if clear_certificate:
        if str(row.qz_certificate_encrypted or "").strip():
            certificate_cleared = True
        row.qz_certificate_encrypted = None
        row.qz_certificate_updated_at = None
    elif normalized_certificate:
        row.qz_certificate_encrypted = _encrypt_qz_credential(normalized_certificate)
        row.qz_certificate_updated_at = now
        certificate_changed = True

    if clear_private_key:
        if str(row.qz_private_key_encrypted or "").strip():
            private_key_cleared = True
        row.qz_private_key_encrypted = None
        row.qz_private_key_updated_at = None
    elif normalized_private_key:
        row.qz_private_key_encrypted = _encrypt_qz_credential(normalized_private_key)
        row.qz_private_key_updated_at = now
        private_key_changed = True

    if certificate_changed or certificate_cleared or private_key_changed or private_key_cleared:
        row.qz_last_validated_at = None
        row.qz_last_validation_status = None
        row.qz_last_validation_summary = None

    db.flush()
    return PlatformQzCredentialUpdateResult(
        state=get_platform_qz_credential_state(db),
        certificate_changed=certificate_changed,
        certificate_cleared=certificate_cleared,
        private_key_changed=private_key_changed,
        private_key_cleared=private_key_cleared,
    )
