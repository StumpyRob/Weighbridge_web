from __future__ import annotations

import base64
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from ..config import settings


class QzSigningConfigurationError(RuntimeError):
    pass


def _normalized_multiline_value(value: str | None) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    return normalized.replace("\\n", "\n")


def _candidate_dev_demo_file(filename: str) -> Path | None:
    if not bool(settings.dev_mode):
        return None

    desktop_candidates = (
        Path.home() / "Desktop",
        Path.home() / "OneDrive" / "Desktop",
    )
    for desktop in desktop_candidates:
        candidate = desktop / "QZ Tray Demo Cert" / filename
        if candidate.is_file():
            return candidate
    return None


def _load_text_value(
    *,
    inline_value: str | None,
    configured_path: str | None,
    default_demo_filename: str,
    label: str,
) -> str:
    normalized_inline = _normalized_multiline_value(inline_value)
    if normalized_inline:
        return normalized_inline

    configured = str(configured_path or "").strip()
    candidate_paths: list[Path] = []
    if configured:
        candidate_paths.append(Path(configured).expanduser())

    demo_candidate = _candidate_dev_demo_file(default_demo_filename)
    if demo_candidate is not None:
        candidate_paths.append(demo_candidate)

    for candidate in candidate_paths:
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8").strip()

    raise QzSigningConfigurationError(
        f"QZ signing {label} is not configured. "
        "Set QZ_CERTIFICATE_PATH/QZ_PRIVATE_KEY_PATH or QZ_CERTIFICATE_TEXT/QZ_PRIVATE_KEY_TEXT."
    )


def load_qz_certificate_text() -> str:
    return _load_text_value(
        inline_value=settings.qz_certificate_text,
        configured_path=settings.qz_certificate_path,
        default_demo_filename="digital-certificate.txt",
        label="certificate",
    )


def _load_qz_private_key_pem() -> str:
    return _load_text_value(
        inline_value=settings.qz_private_key_text,
        configured_path=settings.qz_private_key_path,
        default_demo_filename="private-key.pem",
        label="private key",
    )


def load_qz_private_key() -> RSAPrivateKey:
    pem_bytes = _load_qz_private_key_pem().encode("utf-8")
    private_key = serialization.load_pem_private_key(pem_bytes, password=None)
    if not isinstance(private_key, RSAPrivateKey):
        raise QzSigningConfigurationError("QZ signing private key must be an RSA private key.")
    return private_key


def sign_qz_message(message: str) -> str:
    payload = str(message or "")
    if not payload:
        raise ValueError("QZ request payload is required.")

    signature = load_qz_private_key().sign(
        payload.encode("utf-8"),
        padding.PKCS1v15(),
        hashes.SHA512(),
    )
    return base64.b64encode(signature).decode("ascii")
