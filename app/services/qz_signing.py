from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path

from fastapi.responses import Response
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from ..config import settings
from ..security_hardening import apply_security_headers


class QzSigningConfigurationError(RuntimeError):
    pass


QZ_PUBLIC_DISABLED_MESSAGE = "Direct workstation printing is disabled."
QZ_PUBLIC_UNAVAILABLE_MESSAGE = "Direct workstation printing is not available."
_DEFAULT_QZ_MOUNT_DIRS = (
    Path("/data/qz"),
    Path("/config/qz"),
)
_QZ_SAMPLE_SIGN_PAYLOAD = '{"call":"qz.healthcheck","params":[],"timestamp":1}'


@dataclass(frozen=True)
class QzConfigSourceStatus:
    label: str
    configured: bool
    source_type: str
    source_label: str
    validation_status: str
    resolved_path: str | None = None
    checked_paths: tuple[str, ...] = ()
    summary: str = ""
    next_step: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.validation_status == "ok"


@dataclass(frozen=True)
class QzRouteStatus:
    path: str
    ok: bool
    status: str
    summary: str
    detail: str = ""


@dataclass(frozen=True)
class QzSigningDiagnostics:
    enabled: bool
    certificate: QzConfigSourceStatus
    private_key: QzConfigSourceStatus
    certificate_route: QzRouteStatus
    sign_route: QzRouteStatus
    csp_connect_src_ok: bool
    csp_detail: str
    likely_causes: tuple[str, ...]
    browser_requirements: tuple[str, ...]
    signing_operational: bool
    ready_for_tenants: bool


@dataclass(frozen=True)
class _QzPathCandidate:
    source_type: str
    source_label: str
    path: Path


@dataclass(frozen=True)
class _QzResolvedTextValue:
    value: str | None
    source_type: str
    source_label: str
    resolved_path: str | None
    checked_paths: tuple[str, ...]


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
) -> list[_QzPathCandidate]:
    candidates: list[_QzPathCandidate] = []
    seen: set[str] = set()

    raw_candidates: list[tuple[str, str, str]] = []
    configured_path_value = str(configured_path or "").strip()
    if configured_path_value:
        raw_candidates.append(
            ("app_config_path", "Application file path", configured_path_value)
        )

    for env_name in env_aliases:
        raw_value = str(os.getenv(env_name, "") or "").strip()
        if raw_value:
            raw_candidates.append(
                ("env_path", f"Environment file path ({env_name})", raw_value)
            )

    railway_mount = str(os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "") or "").strip()
    if railway_mount:
        raw_candidates.append(
            (
                "railway_mount",
                "Railway volume mount",
                str(Path(railway_mount).expanduser() / "qz" / default_demo_filename),
            )
        )

    for mount_dir in _DEFAULT_QZ_MOUNT_DIRS:
        raw_candidates.append(
            (
                "mounted_file",
                f"Mounted file ({mount_dir})",
                str(mount_dir / default_demo_filename),
            )
        )

    demo_candidate = _candidate_dev_demo_file(default_demo_filename)
    if demo_candidate is not None:
        raw_candidates.append(
            ("dev_demo_file", "Developer demo file", str(demo_candidate))
        )

    for source_type, source_label, raw_value in raw_candidates:
        candidate = Path(raw_value).expanduser()
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            _QzPathCandidate(
                source_type=source_type,
                source_label=source_label,
                path=candidate,
            )
        )

    return candidates


def _missing_configuration_message(
    *,
    label: str,
    inline_env_aliases: tuple[str, ...],
    path_env_aliases: tuple[str, ...],
    default_demo_filename: str,
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
    return (
        f"QZ signing {label} is not configured. "
        f"Set {inline_hint} or {path_hint}, "
        f"or mount {default_demo_filename} at one of: {mounted_locations}."
    )


def _missing_configuration_next_step() -> str:
    return "You need to provide a QZ certificate and private key in the server configuration."


def _invalid_configuration_next_step() -> str:
    return "Review the configured QZ certificate and private key in the server configuration."


def _resolve_text_value(
    *,
    inline_value: str | None,
    configured_path: str | None,
    path_env_aliases: tuple[str, ...],
    inline_env_aliases: tuple[str, ...],
    default_demo_filename: str,
) -> _QzResolvedTextValue:
    normalized_inline = _normalized_multiline_value(inline_value)
    if normalized_inline:
        return _QzResolvedTextValue(
            value=normalized_inline,
            source_type="app_config_text",
            source_label="Application inline value",
            resolved_path=None,
            checked_paths=(),
        )

    for env_name in inline_env_aliases:
        normalized_env_inline = _normalized_multiline_value(os.getenv(env_name, ""))
        if normalized_env_inline:
            return _QzResolvedTextValue(
                value=normalized_env_inline,
                source_type="env_text",
                source_label=f"Environment inline value ({env_name})",
                resolved_path=None,
                checked_paths=(),
            )

    candidate_paths = _path_candidates(
        configured_path=configured_path,
        env_aliases=path_env_aliases,
        default_demo_filename=default_demo_filename,
    )
    checked_paths = tuple(str(candidate.path) for candidate in candidate_paths)
    for candidate in candidate_paths:
        if candidate.path.is_file():
            return _QzResolvedTextValue(
                value=candidate.path.read_text(encoding="utf-8").strip(),
                source_type=candidate.source_type,
                source_label=candidate.source_label,
                resolved_path=str(candidate.path),
                checked_paths=checked_paths,
            )

    return _QzResolvedTextValue(
        value=None,
        source_type="missing",
        source_label="Missing",
        resolved_path=None,
        checked_paths=checked_paths,
    )


def _validate_certificate_text(certificate_text: str) -> None:
    if not certificate_text:
        raise QzSigningConfigurationError("QZ signing certificate is empty.")
    try:
        x509.load_pem_x509_certificate(certificate_text.encode("utf-8"))
    except Exception as exc:
        raise QzSigningConfigurationError(
            f"QZ signing certificate is invalid: {exc}"
        ) from exc


def _validate_private_key_pem(private_key_pem: str) -> None:
    if not private_key_pem:
        raise QzSigningConfigurationError("QZ signing private key is empty.")
    pem_bytes = private_key_pem.encode("utf-8")
    try:
        private_key = serialization.load_pem_private_key(pem_bytes, password=None)
    except Exception as exc:
        raise QzSigningConfigurationError(
            f"QZ signing private key is invalid: {exc}"
        ) from exc
    if not isinstance(private_key, RSAPrivateKey):
        raise QzSigningConfigurationError(
            "QZ signing private key must be an RSA private key."
        )


def _inspect_source_status(
    *,
    label: str,
    inline_value: str | None,
    configured_path: str | None,
    path_env_aliases: tuple[str, ...],
    inline_env_aliases: tuple[str, ...],
    default_demo_filename: str,
    validator,
) -> QzConfigSourceStatus:
    resolved = _resolve_text_value(
        inline_value=inline_value,
        configured_path=configured_path,
        path_env_aliases=path_env_aliases,
        inline_env_aliases=inline_env_aliases,
        default_demo_filename=default_demo_filename,
    )
    if resolved.value is None:
        return QzConfigSourceStatus(
            label=label,
            configured=False,
            source_type=resolved.source_type,
            source_label=resolved.source_label,
            validation_status="missing",
            checked_paths=resolved.checked_paths,
            summary=f"{label} is not configured.",
            next_step=_missing_configuration_next_step(),
            detail=_missing_configuration_message(
                label=label.lower(),
                inline_env_aliases=inline_env_aliases,
                path_env_aliases=path_env_aliases,
                default_demo_filename=default_demo_filename,
            ),
        )

    try:
        validator(resolved.value)
    except QzSigningConfigurationError as exc:
        return QzConfigSourceStatus(
            label=label,
            configured=True,
            source_type=resolved.source_type,
            source_label=resolved.source_label,
            validation_status="error",
            resolved_path=resolved.resolved_path,
            checked_paths=resolved.checked_paths,
            summary=f"{label} is configured but could not be validated.",
            next_step=_invalid_configuration_next_step(),
            detail=str(exc),
        )

    return QzConfigSourceStatus(
        label=label,
        configured=True,
        source_type=resolved.source_type,
        source_label=resolved.source_label,
        validation_status="ok",
        resolved_path=resolved.resolved_path,
        checked_paths=resolved.checked_paths,
        summary=f"{label} is configured.",
    )


def inspect_qz_certificate_source() -> QzConfigSourceStatus:
    return _inspect_source_status(
        label="Certificate",
        inline_value=settings.qz_certificate_text,
        configured_path=settings.qz_certificate_path,
        path_env_aliases=("QZ_CERTIFICATE_PATH", "QZ_CERTIFICATE_FILE"),
        inline_env_aliases=("QZ_CERTIFICATE_TEXT",),
        default_demo_filename="digital-certificate.txt",
        validator=_validate_certificate_text,
    )


def inspect_qz_private_key_source() -> QzConfigSourceStatus:
    return _inspect_source_status(
        label="Private Key",
        inline_value=settings.qz_private_key_text,
        configured_path=settings.qz_private_key_path,
        path_env_aliases=("QZ_PRIVATE_KEY_PATH", "QZ_PRIVATE_KEY_FILE"),
        inline_env_aliases=("QZ_PRIVATE_KEY_TEXT",),
        default_demo_filename="private-key.pem",
        validator=_validate_private_key_pem,
    )


def load_qz_certificate_text() -> str:
    resolved = _resolve_text_value(
        inline_value=settings.qz_certificate_text,
        configured_path=settings.qz_certificate_path,
        path_env_aliases=("QZ_CERTIFICATE_PATH", "QZ_CERTIFICATE_FILE"),
        inline_env_aliases=("QZ_CERTIFICATE_TEXT",),
        default_demo_filename="digital-certificate.txt",
    )
    if resolved.value is None:
        raise QzSigningConfigurationError(
            _missing_configuration_message(
                label="certificate",
                inline_env_aliases=("QZ_CERTIFICATE_TEXT",),
                path_env_aliases=("QZ_CERTIFICATE_PATH", "QZ_CERTIFICATE_FILE"),
                default_demo_filename="digital-certificate.txt",
            )
        )
    certificate_text = resolved.value.strip()
    _validate_certificate_text(certificate_text)
    return certificate_text


def _load_qz_private_key_pem() -> str:
    resolved = _resolve_text_value(
        inline_value=settings.qz_private_key_text,
        configured_path=settings.qz_private_key_path,
        path_env_aliases=("QZ_PRIVATE_KEY_PATH", "QZ_PRIVATE_KEY_FILE"),
        inline_env_aliases=("QZ_PRIVATE_KEY_TEXT",),
        default_demo_filename="private-key.pem",
    )
    if resolved.value is None:
        raise QzSigningConfigurationError(
            _missing_configuration_message(
                label="private key",
                inline_env_aliases=("QZ_PRIVATE_KEY_TEXT",),
                path_env_aliases=("QZ_PRIVATE_KEY_PATH", "QZ_PRIVATE_KEY_FILE"),
                default_demo_filename="private-key.pem",
            )
        )
    private_key_pem = resolved.value.strip()
    _validate_private_key_pem(private_key_pem)
    return private_key_pem


def load_qz_private_key() -> RSAPrivateKey:
    pem_bytes = _load_qz_private_key_pem().encode("utf-8")
    private_key = serialization.load_pem_private_key(pem_bytes, password=None)
    if not isinstance(private_key, RSAPrivateKey):
        raise QzSigningConfigurationError(
            "QZ signing private key must be an RSA private key."
        )
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


def qz_public_route_error_message(*, enabled: bool) -> str:
    return QZ_PUBLIC_UNAVAILABLE_MESSAGE if enabled else QZ_PUBLIC_DISABLED_MESSAGE


def _qz_csp_connect_src_ok() -> tuple[bool, str]:
    response = Response()
    apply_security_headers(response)
    csp = str(response.headers.get("Content-Security-Policy", "") or "")
    required_sources = (
        "wss://localhost:8181",
        "wss://localhost.qz.io:8181",
    )
    if all(source in csp for source in required_sources):
        return True, "CSP allows secure QZ Tray websocket endpoints."
    return False, "CSP is missing one or more secure QZ Tray websocket endpoints."


def _certificate_route_status(
    *,
    enabled: bool,
    certificate_status: QzConfigSourceStatus,
) -> QzRouteStatus:
    if not enabled:
        return QzRouteStatus(
            path="/qz/certificate",
            ok=False,
            status="warning",
            summary="Disabled",
            detail="Platform direct workstation printing is disabled.",
        )
    if certificate_status.ok:
        return QzRouteStatus(
            path="/qz/certificate",
            ok=True,
            status="ok",
            summary="OK",
            detail="Certificate route can return the configured certificate.",
        )
    return QzRouteStatus(
        path="/qz/certificate",
        ok=False,
        status="error",
        summary="Fail",
        detail=certificate_status.detail,
    )


def _sign_route_status(
    *,
    enabled: bool,
    private_key_status: QzConfigSourceStatus,
) -> QzRouteStatus:
    if not enabled:
        return QzRouteStatus(
            path="/qz/sign",
            ok=False,
            status="warning",
            summary="Disabled",
            detail="Platform direct workstation printing is disabled.",
        )
    if not private_key_status.ok:
        return QzRouteStatus(
            path="/qz/sign",
            ok=False,
            status="error",
            summary="Fail",
            detail=private_key_status.detail,
        )
    try:
        sign_qz_message(_QZ_SAMPLE_SIGN_PAYLOAD)
    except (ValueError, QzSigningConfigurationError) as exc:
        return QzRouteStatus(
            path="/qz/sign",
            ok=False,
            status="error",
            summary="Fail",
            detail=str(exc),
        )
    return QzRouteStatus(
        path="/qz/sign",
        ok=True,
        status="ok",
        summary="OK",
        detail="Sign route can produce SHA-512 signatures.",
    )


def build_qz_signing_diagnostics(*, enabled: bool) -> QzSigningDiagnostics:
    certificate_status = inspect_qz_certificate_source()
    private_key_status = inspect_qz_private_key_source()
    certificate_route = _certificate_route_status(
        enabled=enabled,
        certificate_status=certificate_status,
    )
    sign_route = _sign_route_status(
        enabled=enabled,
        private_key_status=private_key_status,
    )
    csp_connect_src_ok, csp_detail = _qz_csp_connect_src_ok()

    signing_operational = bool(enabled and certificate_route.ok and sign_route.ok)
    ready_for_tenants = bool(signing_operational and csp_connect_src_ok)

    likely_causes: list[str] = []
    if not enabled:
        likely_causes.append(
            "QZ printing is disabled at platform level."
        )
    if not certificate_status.ok and certificate_status.summary:
        likely_causes.append(certificate_status.summary)
    if not private_key_status.ok and private_key_status.summary:
        likely_causes.append(private_key_status.summary)
    if enabled and certificate_status.ok and private_key_status.ok and not sign_route.ok:
        likely_causes.append("Signing test failed.")
    if signing_operational and not csp_connect_src_ok:
        likely_causes.append("Browser compatibility checks need attention.")

    browser_requirements = (
        "Each workstation browser must have QZ Tray installed and running.",
        "Users must allow or trust this site inside QZ Tray before direct printing will work.",
        "If server checks pass but users still cannot print, the remaining issue is likely workstation or browser setup.",
    )

    return QzSigningDiagnostics(
        enabled=enabled,
        certificate=certificate_status,
        private_key=private_key_status,
        certificate_route=certificate_route,
        sign_route=sign_route,
        csp_connect_src_ok=csp_connect_src_ok,
        csp_detail=csp_detail,
        likely_causes=tuple(likely_causes),
        browser_requirements=browser_requirements,
        signing_operational=signing_operational,
        ready_for_tenants=ready_for_tenants,
    )
