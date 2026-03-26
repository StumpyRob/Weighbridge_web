from __future__ import annotations

import base64
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from ..config import settings


class QzSigningConfigurationError(RuntimeError):
    pass


_DEFAULT_QZ_MOUNT_DIRS = (
    Path("/data/qz"),
    Path("/config/qz"),
)


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


def _path_candidates(
    *,
    configured_path: str | None,
    env_aliases: tuple[str, ...],
    default_demo_filename: str,
) -> list[Path]:
    candidates: list[Path] = []
    seen: set[str] = set()

    raw_values = [str(configured_path or "").strip()]
    raw_values.extend(str(os.getenv(name, "") or "").strip() for name in env_aliases)

    railway_mount = str(os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "") or "").strip()
    if railway_mount:
        raw_values.append(str((Path(railway_mount).expanduser() / "qz" / default_demo_filename)))

    for mount_dir in _DEFAULT_QZ_MOUNT_DIRS:
        raw_values.append(str(mount_dir / default_demo_filename))

    demo_candidate = _candidate_dev_demo_file(default_demo_filename)
    if demo_candidate is not None:
        raw_values.append(str(demo_candidate))

    for raw in raw_values:
        if not raw:
            continue
        candidate = Path(raw).expanduser()
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)

    return candidates


def _missing_configuration_message(
    *,
    label: str,
    inline_env_aliases: tuple[str, ...],
    path_env_aliases: tuple[str, ...],
    default_demo_filename: str,
    candidate_paths: list[Path],
) -> str:
    inline_hint = " or ".join(inline_env_aliases)
    path_hint = " or ".join(path_env_aliases)
    mounted_locations = ", ".join(
        [
            f"$RAILWAY_VOLUME_MOUNT_PATH/qz/{default_demo_filename}",
            f"/data/qz/{default_demo_filename}",
            f"/config/qz/{default_demo_filename}",
        ]
    )
    tried_paths = ", ".join(str(path) for path in candidate_paths) or "(none)"
    return (
        f"QZ signing {label} is not configured. "
        f"Set {inline_hint} or {path_hint}, "
        f"or mount {default_demo_filename} at one of: {mounted_locations}. "
        f"Checked paths: {tried_paths}"
    )


def _load_text_value(
    *,
    inline_value: str | None,
    configured_path: str | None,
    path_env_aliases: tuple[str, ...],
    inline_env_aliases: tuple[str, ...],
    default_demo_filename: str,
    label: str,
) -> str:
    normalized_inline = _normalized_multiline_value(inline_value)
    if normalized_inline:
        return normalized_inline

    for env_name in inline_env_aliases:
        normalized_env_inline = _normalized_multiline_value(os.getenv(env_name, ""))
        if normalized_env_inline:
            return normalized_env_inline

    candidate_paths = _path_candidates(
        configured_path=configured_path,
        env_aliases=path_env_aliases,
        default_demo_filename=default_demo_filename,
    )

    for candidate in candidate_paths:
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8").strip()

    raise QzSigningConfigurationError(
        _missing_configuration_message(
            label=label,
            inline_env_aliases=inline_env_aliases,
            path_env_aliases=path_env_aliases,
            default_demo_filename=default_demo_filename,
            candidate_paths=candidate_paths,
        )
    )


def load_qz_certificate_text() -> str:
    certificate_text = _load_text_value(
        inline_value=settings.qz_certificate_text,
        configured_path=settings.qz_certificate_path,
        path_env_aliases=("QZ_CERTIFICATE_PATH", "QZ_CERTIFICATE_FILE"),
        inline_env_aliases=("QZ_CERTIFICATE_TEXT",),
        default_demo_filename="digital-certificate.txt",
        label="certificate",
    )
    if not certificate_text:
        raise QzSigningConfigurationError("QZ signing certificate is empty.")
    try:
        x509.load_pem_x509_certificate(certificate_text.encode("utf-8"))
    except Exception as exc:
        raise QzSigningConfigurationError(
            f"QZ signing certificate is invalid: {exc}"
        ) from exc
    return certificate_text


def _load_qz_private_key_pem() -> str:
    return _load_text_value(
        inline_value=settings.qz_private_key_text,
        configured_path=settings.qz_private_key_path,
        path_env_aliases=("QZ_PRIVATE_KEY_PATH", "QZ_PRIVATE_KEY_FILE"),
        inline_env_aliases=("QZ_PRIVATE_KEY_TEXT",),
        default_demo_filename="private-key.pem",
        label="private key",
    )


def load_qz_private_key() -> RSAPrivateKey:
    pem_bytes = _load_qz_private_key_pem().encode("utf-8")
    try:
        private_key = serialization.load_pem_private_key(pem_bytes, password=None)
    except Exception as exc:
        raise QzSigningConfigurationError(
            f"QZ signing private key is invalid: {exc}"
        ) from exc
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
