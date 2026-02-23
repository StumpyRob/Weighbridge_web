from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models.base import utcnow
from ..models import PrintJob, PrintProfile, PrintTemplate
from .print_render import load_template_source, render_from_content
from .print_transport import PrintMode, send as send_print_job

PRINT_JOB_STATUS_QUEUED = "QUEUED"
PRINT_JOB_STATUS_SENT = "SENT"
PRINT_JOB_STATUS_FAILED = "FAILED"

PRINT_CONTENT_TYPE_TEXT = "TEXT"
PRINT_CONTENT_TYPE_HTML = "HTML"

TRANSPORT_MODE_NETWORK_RAW_9100 = "NETWORK_RAW_9100"
TRANSPORT_MODE_USB_ESC_POS = "USB_ESC_POS"
TRANSPORT_MODE_CUPS = "CUPS"
TRANSPORT_MODE_LOCAL_BROWSER = "LOCAL_BROWSER"
TRANSPORT_MODE_LOCAL_NODE_HTTP = "LOCAL_NODE_HTTP"

PRINT_PROFILE_PURPOSE_INVOICE_PDF = "INVOICE_PDF"
LEGACY_PRINT_PROFILE_PURPOSE_INVOICE_A4 = "INVOICE_A4"
DEFAULT_INVOICE_PDF_PROFILE_CODE = "INVOICE_PDF_DEFAULT"
DEFAULT_INVOICE_PDF_PROFILE_DESCRIPTION = "Default invoice printer"
DEFAULT_INVOICE_PDF_TEMPLATE_NAME = "invoice_default.html"


@dataclass(slots=True)
class RenderedPrint:
    rendered_content: str
    content_type: str
    template_id: int | None
    template_label: str


@dataclass(slots=True)
class PrintExecutionResult:
    job: PrintJob
    browser_content: str | None = None
    browser_content_type: str | None = None


def _normalize_content_type(value: str | None) -> str:
    normalized = str(value or "").strip().upper()
    if normalized == PRINT_CONTENT_TYPE_HTML:
        return PRINT_CONTENT_TYPE_HTML
    return PRINT_CONTENT_TYPE_TEXT


def resolve_profile_transport(profile: PrintProfile) -> tuple[PrintMode, dict]:
    transport_mode = str(profile.transport_mode or "").strip().upper()
    transport_config = (
        dict(profile.transport_config)
        if isinstance(profile.transport_config, dict)
        else {}
    )
    if transport_mode == TRANSPORT_MODE_NETWORK_RAW_9100:
        return "network", transport_config
    if transport_mode == TRANSPORT_MODE_CUPS:
        return "cups", transport_config
    if transport_mode == TRANSPORT_MODE_USB_ESC_POS:
        return "usb", transport_config
    if transport_mode == TRANSPORT_MODE_LOCAL_BROWSER:
        return "local_browser", transport_config
    if transport_mode == TRANSPORT_MODE_LOCAL_NODE_HTTP:
        return "local_node_http", transport_config
    raise ValueError(f"Unsupported print transport mode: {profile.transport_mode}")


def _legacy_template_content_type(template_name: str) -> str:
    return (
        PRINT_CONTENT_TYPE_HTML
        if str(template_name or "").strip().lower().endswith(".html")
        else PRINT_CONTENT_TYPE_TEXT
    )


def resolve_profile_template(db: Session, profile: PrintProfile) -> tuple[str, str, int | None, str]:
    if profile.template_id:
        template = db.get(PrintTemplate, profile.template_id)
        if template is None:
            raise ValueError("Profile template not found.")
        return (
            template.content,
            _normalize_content_type(template.content_type),
            template.id,
            template.code,
        )

    template_name = str(profile.template_name or "").strip()
    if not template_name:
        raise ValueError("Profile template is not configured.")
    content = load_template_source(template_name)
    return (
        content,
        _legacy_template_content_type(template_name),
        None,
        template_name,
    )


def render_profile_content(
    db: Session,
    *,
    payload: dict[str, Any],
    profile: PrintProfile,
) -> RenderedPrint:
    template_content, content_type, template_id, template_label = resolve_profile_template(
        db, profile
    )
    rendered = render_from_content(payload, template_content)
    return RenderedPrint(
        rendered_content=rendered,
        content_type=content_type,
        template_id=template_id,
        template_label=template_label,
    )


def _encode_rendered_content(rendered_content: str) -> bytes:
    return rendered_content.encode("utf-8")


def _serialized_bytes(job_bytes: bytes | None) -> str | None:
    if not job_bytes:
        return None
    return base64.b64encode(job_bytes).decode("ascii")


def _job_bytes_for_retry(job: PrintJob) -> tuple[bytes, str]:
    if job.rendered_bytes_base64:
        try:
            decoded = base64.b64decode(job.rendered_bytes_base64.encode("ascii"))
        except (ValueError, TypeError):
            decoded = b""
        if decoded:
            return decoded, "bytes"
    content = str(job.rendered_content or "")
    return content.encode("utf-8"), "text"


def _content_type_from_rendered(rendered_content: str) -> str:
    return _normalize_content_type(
        PRINT_CONTENT_TYPE_HTML
        if rendered_content.lstrip().startswith("<")
        else PRINT_CONTENT_TYPE_TEXT
    )


def _apply_job_delivery_success(job: PrintJob) -> None:
    job.status = PRINT_JOB_STATUS_SENT
    job.last_error = None
    job.sent_at = utcnow()


def _apply_job_delivery_failure(job: PrintJob, exc: Exception) -> None:
    job.status = PRINT_JOB_STATUS_FAILED
    job.last_error = str(exc) or "Print delivery failed."


def _snapshot_transport_config_for_job(mode: str, transport_config: dict) -> dict:
    if str(mode or "").strip().upper() == TRANSPORT_MODE_LOCAL_BROWSER:
        return {}
    return dict(transport_config or {})


def execute_rendered_print(
    db: Session,
    *,
    purpose: str,
    rendered_content: str,
    content_type: str,
    transport_mode: str,
    transport_config: dict,
    profile_id: int | None = None,
    template_id: int | None = None,
    ticket_id: int | None = None,
    created_by_user_id: int | None = None,
) -> PrintExecutionResult:
    normalized_transport_mode = str(transport_mode or "").strip().upper()
    normalized_content_type = _normalize_content_type(content_type)
    payload_bytes = _encode_rendered_content(rendered_content)

    job = PrintJob(
        created_by_user_id=created_by_user_id,
        purpose=str(purpose or "").strip().upper(),
        profile_id=profile_id,
        template_id=template_id,
        ticket_id=ticket_id,
        transport_mode=normalized_transport_mode,
        transport_config_json=_snapshot_transport_config_for_job(
            normalized_transport_mode,
            transport_config,
        ),
        rendered_content=rendered_content,
        rendered_bytes_base64=_serialized_bytes(payload_bytes),
        status=PRINT_JOB_STATUS_QUEUED,
        attempt_count=0,
        last_error=None,
        sent_at=None,
    )
    db.add(job)
    db.flush()

    job.attempt_count = int(job.attempt_count or 0) + 1
    mode = str(normalized_transport_mode).upper()
    try:
        if mode == TRANSPORT_MODE_LOCAL_BROWSER:
            _apply_job_delivery_success(job)
            db.commit()
            return PrintExecutionResult(
                job=job,
                browser_content=rendered_content,
                browser_content_type=normalized_content_type,
            )

        send_print_job(
            payload_bytes,
            _transport_literal_from_profile_mode(mode),
            dict(transport_config or {}),
            purpose=job.purpose,
            rendered_content=rendered_content,
            content_type=normalized_content_type,
            job_id=job.id,
        )
        _apply_job_delivery_success(job)
        db.commit()
        return PrintExecutionResult(job=job)
    except (RuntimeError, ValueError, OSError, NotImplementedError) as exc:
        _apply_job_delivery_failure(job, exc)
        db.commit()
        raise


def retry_print_job(db: Session, job: PrintJob) -> PrintExecutionResult:
    mode = str(job.transport_mode or "").strip().upper()
    transport_config = (
        dict(job.transport_config_json)
        if isinstance(job.transport_config_json, dict)
        else {}
    )
    payload_bytes, payload_source = _job_bytes_for_retry(job)
    rendered_content = (
        str(job.rendered_content or "")
        if payload_source == "text"
        else payload_bytes.decode("utf-8", errors="replace")
    )
    normalized_content_type = _content_type_from_rendered(rendered_content)

    job.attempt_count = int(job.attempt_count or 0) + 1
    try:
        if mode == TRANSPORT_MODE_LOCAL_BROWSER:
            _apply_job_delivery_success(job)
            db.commit()
            return PrintExecutionResult(
                job=job,
                browser_content=rendered_content,
                browser_content_type=normalized_content_type,
            )
        send_print_job(
            payload_bytes,
            _transport_literal_from_profile_mode(mode),
            transport_config,
            purpose=str(job.purpose or ""),
            rendered_content=rendered_content,
            content_type=normalized_content_type,
            job_id=job.id,
        )
        _apply_job_delivery_success(job)
        db.commit()
        return PrintExecutionResult(job=job)
    except (RuntimeError, ValueError, OSError, NotImplementedError) as exc:
        _apply_job_delivery_failure(job, exc)
        db.commit()
        raise


def replay_print_job(
    db: Session,
    job: PrintJob,
    *,
    created_by_user_id: int | None = None,
) -> PrintExecutionResult:
    payload_bytes, payload_source = _job_bytes_for_retry(job)
    rendered_content = (
        str(job.rendered_content or "")
        if payload_source == "text"
        else payload_bytes.decode("utf-8", errors="replace")
    )
    content_type = _content_type_from_rendered(rendered_content)
    transport_config = (
        dict(job.transport_config_json)
        if isinstance(job.transport_config_json, dict)
        else {}
    )
    return execute_rendered_print(
        db,
        purpose=str(job.purpose or "").strip().upper(),
        rendered_content=rendered_content,
        content_type=content_type,
        transport_mode=str(job.transport_mode or "").strip().upper(),
        transport_config=transport_config,
        profile_id=job.profile_id,
        template_id=job.template_id,
        ticket_id=job.ticket_id,
        created_by_user_id=created_by_user_id,
    )


def _transport_literal_from_profile_mode(mode: str) -> PrintMode:
    normalized = str(mode or "").strip().upper()
    if normalized == TRANSPORT_MODE_NETWORK_RAW_9100:
        return "network"
    if normalized == TRANSPORT_MODE_CUPS:
        return "cups"
    if normalized == TRANSPORT_MODE_USB_ESC_POS:
        return "usb"
    if normalized == TRANSPORT_MODE_LOCAL_BROWSER:
        return "local_browser"
    if normalized == TRANSPORT_MODE_LOCAL_NODE_HTTP:
        return "local_node_http"
    raise ValueError(f"Unsupported print transport mode: {mode}")


def _next_profile_code(db: Session, base_code: str) -> str:
    normalized_base = str(base_code or "").strip().upper() or DEFAULT_INVOICE_PDF_PROFILE_CODE
    candidate = normalized_base
    suffix = 1
    while True:
        exists = db.execute(
            select(PrintProfile.id)
            .where(func.lower(PrintProfile.code) == candidate.lower())
            .limit(1)
        ).first()
        if not exists:
            return candidate
        suffix += 1
        candidate = f"{normalized_base}_{suffix}"


def migrate_legacy_invoice_a4_purposes(db: Session) -> int:
    updated = 0

    legacy_templates = list(
        db.execute(
            select(PrintTemplate).where(
                PrintTemplate.purpose == LEGACY_PRINT_PROFILE_PURPOSE_INVOICE_A4
            )
        ).scalars()
    )
    for template in legacy_templates:
        template.purpose = PRINT_PROFILE_PURPOSE_INVOICE_PDF
        updated += 1

    legacy_profiles = list(
        db.execute(
            select(PrintProfile).where(
                PrintProfile.purpose == LEGACY_PRINT_PROFILE_PURPOSE_INVOICE_A4
            )
        ).scalars()
    )
    for profile in legacy_profiles:
        profile.purpose = PRINT_PROFILE_PURPOSE_INVOICE_PDF
        updated += 1

    legacy_jobs = list(
        db.execute(
            select(PrintJob).where(
                PrintJob.purpose == LEGACY_PRINT_PROFILE_PURPOSE_INVOICE_A4
            )
        ).scalars()
    )
    for job in legacy_jobs:
        job.purpose = PRINT_PROFILE_PURPOSE_INVOICE_PDF
        updated += 1

    if updated:
        db.flush()
    return updated


def ensure_default_invoice_pdf_profile(db: Session) -> tuple[PrintProfile, bool]:
    changed = bool(migrate_legacy_invoice_a4_purposes(db))

    profiles = list(
        db.execute(
            select(PrintProfile)
            .where(
                PrintProfile.is_active.is_(True),
                PrintProfile.purpose == PRINT_PROFILE_PURPOSE_INVOICE_PDF,
            )
            .order_by(PrintProfile.is_default.desc(), PrintProfile.code.asc())
        ).scalars()
    )
    default_profile = next((row for row in profiles if row.is_default), None)
    if default_profile is not None:
        if changed:
            db.commit()
            db.refresh(default_profile)
        return default_profile, changed

    if profiles:
        selected = profiles[0]
        selected.is_default = True
        db.commit()
        db.refresh(selected)
        return selected, True

    created_profile = PrintProfile(
        code=_next_profile_code(db, DEFAULT_INVOICE_PDF_PROFILE_CODE),
        description=DEFAULT_INVOICE_PDF_PROFILE_DESCRIPTION,
        purpose=PRINT_PROFILE_PURPOSE_INVOICE_PDF,
        template_id=None,
        template_name=DEFAULT_INVOICE_PDF_TEMPLATE_NAME,
        yard_id=None,
        transport_mode=TRANSPORT_MODE_LOCAL_BROWSER,
        transport_config={},
        is_default=True,
        is_active=True,
    )
    db.add(created_profile)
    db.commit()
    db.refresh(created_profile)
    return created_profile, True
