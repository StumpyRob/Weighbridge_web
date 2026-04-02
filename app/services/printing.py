from __future__ import annotations

import base64
from dataclasses import dataclass
from html import escape
import re
from typing import Any, Callable

from sqlalchemy.orm import Session

from ..models import Invoice, PrintDestination, PrintJob, PrintTemplate, Ticket
from ..models.base import utcnow
from .email_delivery import send_delivery_email
from .print_render import render_from_content
from .print_transport import (
    NODE_HTTP_TEXT_MIME_TYPE,
    PrintMode,
    PrintTransportResult,
    send as send_print_job,
)

PRINT_JOB_STATUS_QUEUED = "QUEUED"
PRINT_JOB_STATUS_PENDING = "PENDING"
PRINT_JOB_STATUS_IN_PROGRESS = "IN_PROGRESS"
PRINT_JOB_STATUS_SENT = "SENT"
PRINT_JOB_STATUS_FAILED = "FAILED"
PRINT_JOB_TRIGGER_SOURCE_MANUAL = "MANUAL"
PRINT_JOB_TRIGGER_SOURCE_AUTO_ON_COMPLETE = "AUTO_ON_COMPLETE"

PRINT_CONTENT_TYPE_TEXT = "TEXT"
PRINT_CONTENT_TYPE_RAW = "RAW"
PRINT_CONTENT_TYPE_HTML = "HTML"
PRINT_CONTENT_TYPE_PDF = "PDF"
PRINT_CONTENT_MIME_TYPE_HTML = "text/html; charset=utf-8"
PRINT_CONTENT_MIME_TYPE_PDF = "application/pdf"
PRINT_CONTENT_MIME_TYPE_RAW = "application/octet-stream"

DOCUMENT_TYPE_TICKET = "TICKET"
DOCUMENT_TYPE_INVOICE = "INVOICE"
DOCUMENT_TYPE_WTN = "WTN"

DELIVERY_TYPE_PRINT_LOCAL_BROWSER = "PRINT_LOCAL_BROWSER"
DELIVERY_TYPE_PRINT_NETWORK_RAW_9100 = "PRINT_NETWORK_RAW_9100"
DELIVERY_TYPE_PRINT_NODE_HTTP = "PRINT_NODE_HTTP"
DELIVERY_TYPE_PRINT_AGENT_PULL = "PRINT_AGENT_PULL"
DELIVERY_TYPE_EMAIL_PDF = "EMAIL_PDF"


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


@dataclass(slots=True)
class PreparedPayload:
    transport_content: str
    payload_bytes: bytes
    payload_format: str
    payload_mime_type: str


def _normalize_document_type(value: str | None) -> str:
    normalized = str(value or "").strip().upper()
    if normalized in {DOCUMENT_TYPE_TICKET, DOCUMENT_TYPE_INVOICE, DOCUMENT_TYPE_WTN}:
        return normalized
    return DOCUMENT_TYPE_TICKET


def _normalize_content_type(value: str | None) -> str:
    normalized = str(value or "").strip().upper()
    if normalized == PRINT_CONTENT_TYPE_RAW:
        return PRINT_CONTENT_TYPE_RAW
    if normalized == PRINT_CONTENT_TYPE_HTML:
        return PRINT_CONTENT_TYPE_HTML
    if normalized == PRINT_CONTENT_TYPE_PDF:
        return PRINT_CONTENT_TYPE_PDF
    return PRINT_CONTENT_TYPE_TEXT


def _normalize_delivery_type(value: str | None) -> str:
    normalized = str(value or "").strip().upper()
    mapping = {
        "LOCAL_BROWSER": DELIVERY_TYPE_PRINT_LOCAL_BROWSER,
        "NETWORK_RAW_9100": DELIVERY_TYPE_PRINT_NETWORK_RAW_9100,
        "LOCAL_NODE_HTTP": DELIVERY_TYPE_PRINT_NODE_HTTP,
        DELIVERY_TYPE_PRINT_AGENT_PULL: DELIVERY_TYPE_PRINT_AGENT_PULL,
        DELIVERY_TYPE_PRINT_LOCAL_BROWSER: DELIVERY_TYPE_PRINT_LOCAL_BROWSER,
        DELIVERY_TYPE_PRINT_NETWORK_RAW_9100: DELIVERY_TYPE_PRINT_NETWORK_RAW_9100,
        DELIVERY_TYPE_PRINT_NODE_HTTP: DELIVERY_TYPE_PRINT_NODE_HTTP,
        DELIVERY_TYPE_EMAIL_PDF: DELIVERY_TYPE_EMAIL_PDF,
    }
    return mapping.get(normalized, DELIVERY_TYPE_PRINT_LOCAL_BROWSER)


def _normalize_print_job_trigger_source(value: str | None) -> str:
    normalized = str(value or "").strip().upper()
    if normalized == PRINT_JOB_TRIGGER_SOURCE_AUTO_ON_COMPLETE:
        return PRINT_JOB_TRIGGER_SOURCE_AUTO_ON_COMPLETE
    return PRINT_JOB_TRIGGER_SOURCE_MANUAL


def resolve_destination_template(
    db: Session,
    destination: PrintDestination,
) -> tuple[str, str, int, str]:
    template = db.get(PrintTemplate, int(destination.template_id or 0))
    if template is None:
        raise ValueError("Destination template not found.")
    if not bool(template.is_active):
        raise ValueError("Destination template is inactive.")
    if _normalize_document_type(template.document_type) != _normalize_document_type(
        destination.document_type
    ):
        raise ValueError("Template document type does not match destination.")

    return (
        str(template.content or ""),
        _normalize_content_type(template.format),
        int(template.id),
        str(template.code or template.description or f"Template {template.id}"),
    )


def render_destination_content(
    db: Session,
    *,
    payload: dict[str, Any],
    destination: PrintDestination,
) -> RenderedPrint:
    template_content, content_type, template_id, template_label = resolve_destination_template(
        db,
        destination,
    )
    rendered = render_from_content(payload, template_content, db=db)
    return RenderedPrint(
        rendered_content=rendered,
        content_type=content_type,
        template_id=template_id,
        template_label=template_label,
    )


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
        PRINT_CONTENT_TYPE_HTML if rendered_content.lstrip().startswith("<") else PRINT_CONTENT_TYPE_TEXT
    )


def _payload_metadata_for_resend(job: PrintJob, rendered_content: str) -> tuple[str, str]:
    stored_payload_format = str(job.payload_format or "").strip().upper()
    if stored_payload_format:
        payload_format = _normalize_content_type(stored_payload_format)
    else:
        payload_format = _content_type_from_rendered(rendered_content)

    payload_mime_type = str(job.payload_mime_type or "").strip()
    if not payload_mime_type:
        payload_mime_type = _payload_mime_type_for_content_type(payload_format)

    return payload_format, payload_mime_type


def _payload_mime_type_for_content_type(content_type: str) -> str:
    normalized = _normalize_content_type(content_type)
    if normalized == PRINT_CONTENT_TYPE_RAW:
        return PRINT_CONTENT_MIME_TYPE_RAW
    if normalized == PRINT_CONTENT_TYPE_HTML:
        return PRINT_CONTENT_MIME_TYPE_HTML
    if normalized == PRINT_CONTENT_TYPE_PDF:
        return PRINT_CONTENT_MIME_TYPE_PDF
    return NODE_HTTP_TEXT_MIME_TYPE


def _rendered_document_html(rendered_content: str, content_type: str) -> str:
    normalized = _normalize_content_type(content_type)
    if normalized == PRINT_CONTENT_TYPE_HTML:
        return rendered_content
    if normalized == PRINT_CONTENT_TYPE_RAW:
        encoded = base64.b64encode(rendered_content.encode("utf-8")).decode("ascii")
        return (
            "<html><body><pre>"
            f"RAW payload preview unavailable.\n\nBase64 preview:\n{escape(encoded)}"
            "</pre></body></html>"
        )
    return f"<html><body><pre>{escape(rendered_content)}</pre></body></html>"


def _render_pdf_payload_bytes(
    rendered_content: str,
    *,
    content_type: str,
    base_url: str | None,
) -> bytes:
    from .pdf import render_html_pdf_bytes

    normalized = _normalize_content_type(content_type)
    if normalized == PRINT_CONTENT_TYPE_PDF:
        raise ValueError("PDF payload bytes are required when content_type is PDF.")

    rendered_html = _rendered_document_html(rendered_content, normalized)
    return render_html_pdf_bytes(
        rendered_html,
        base_url=base_url,
        allow_fallback=False,
        include_fallback_warning=False,
        enforce_print_safe=normalized == PRINT_CONTENT_TYPE_HTML,
        enforce_single_page=normalized == PRINT_CONTENT_TYPE_HTML,
    )


def _prepare_delivery_payload(
    *,
    rendered_content: str,
    content_type: str,
    delivery_type: str,
    payload_bytes: bytes | None,
    base_url: str | None,
) -> PreparedPayload:
    normalized_delivery_type = _normalize_delivery_type(delivery_type)
    normalized_content_type = _normalize_content_type(content_type)
    transport_content = str(rendered_content or "")
    resolved_payload_bytes = payload_bytes or transport_content.encode("utf-8")
    resolved_payload_format = normalized_content_type
    resolved_payload_mime_type = _payload_mime_type_for_content_type(normalized_content_type)

    if normalized_content_type == PRINT_CONTENT_TYPE_HTML and normalized_delivery_type in {
        DELIVERY_TYPE_PRINT_LOCAL_BROWSER,
        DELIVERY_TYPE_PRINT_NETWORK_RAW_9100,
        DELIVERY_TYPE_PRINT_NODE_HTTP,
    }:
        transport_content, resolved_payload_bytes = _prepare_html_send_payload(
            transport_content,
            base_url=base_url,
        )

    if normalized_delivery_type == DELIVERY_TYPE_EMAIL_PDF and normalized_content_type != PRINT_CONTENT_TYPE_PDF:
        resolved_payload_bytes = _render_pdf_payload_bytes(
            transport_content,
            content_type=normalized_content_type,
            base_url=base_url,
        )
        resolved_payload_format = PRINT_CONTENT_TYPE_PDF
        resolved_payload_mime_type = PRINT_CONTENT_MIME_TYPE_PDF
    elif normalized_delivery_type == DELIVERY_TYPE_PRINT_AGENT_PULL:
        if normalized_content_type == PRINT_CONTENT_TYPE_PDF:
            resolved_payload_bytes = payload_bytes or resolved_payload_bytes
            resolved_payload_format = PRINT_CONTENT_TYPE_PDF
            resolved_payload_mime_type = PRINT_CONTENT_MIME_TYPE_PDF
        elif normalized_content_type == PRINT_CONTENT_TYPE_RAW:
            resolved_payload_bytes = payload_bytes or resolved_payload_bytes
            resolved_payload_format = PRINT_CONTENT_TYPE_RAW
            resolved_payload_mime_type = _payload_mime_type_for_content_type(PRINT_CONTENT_TYPE_RAW)
        else:
            resolved_payload_bytes = _render_pdf_payload_bytes(
                transport_content,
                content_type=normalized_content_type,
                base_url=base_url,
            )
            resolved_payload_format = PRINT_CONTENT_TYPE_PDF
            resolved_payload_mime_type = PRINT_CONTENT_MIME_TYPE_PDF
    elif normalized_delivery_type == DELIVERY_TYPE_PRINT_NODE_HTTP:
        if normalized_content_type == PRINT_CONTENT_TYPE_HTML:
            resolved_payload_bytes = _render_pdf_payload_bytes(
                transport_content,
                content_type=normalized_content_type,
                base_url=base_url,
            )
            resolved_payload_format = PRINT_CONTENT_TYPE_PDF
            resolved_payload_mime_type = PRINT_CONTENT_MIME_TYPE_PDF
        elif normalized_content_type == PRINT_CONTENT_TYPE_PDF:
            resolved_payload_bytes = payload_bytes or resolved_payload_bytes
            resolved_payload_format = PRINT_CONTENT_TYPE_PDF
            resolved_payload_mime_type = PRINT_CONTENT_MIME_TYPE_PDF
        elif normalized_content_type == PRINT_CONTENT_TYPE_RAW:
            resolved_payload_bytes = payload_bytes or resolved_payload_bytes
            resolved_payload_format = PRINT_CONTENT_TYPE_RAW
            resolved_payload_mime_type = _payload_mime_type_for_content_type(PRINT_CONTENT_TYPE_RAW)

    return PreparedPayload(
        transport_content=transport_content,
        payload_bytes=resolved_payload_bytes,
        payload_format=resolved_payload_format,
        payload_mime_type=resolved_payload_mime_type,
    )


def _safe_filename_token(value: str | None, *, fallback: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip("-")
    return normalized or fallback


def _payload_file_extension(payload_format: str, payload_mime_type: str | None = None) -> str:
    normalized_format = _normalize_content_type(payload_format)
    normalized_mime_type = str(payload_mime_type or "").strip().lower()
    if normalized_format == PRINT_CONTENT_TYPE_PDF or normalized_mime_type == PRINT_CONTENT_MIME_TYPE_PDF:
        return "pdf"
    if normalized_format == PRINT_CONTENT_TYPE_HTML or normalized_mime_type.startswith("text/html"):
        return "html"
    if normalized_format == PRINT_CONTENT_TYPE_RAW:
        return "bin"
    return "txt"


def resolve_document_filename(
    db: Session,
    *,
    document_type: str,
    ticket_id: int | None = None,
    invoice_id: int | None = None,
    payload_format: str,
    payload_mime_type: str | None = None,
) -> str:
    normalized_document_type = _normalize_document_type(document_type)
    extension = _payload_file_extension(payload_format, payload_mime_type)

    if normalized_document_type == DOCUMENT_TYPE_INVOICE:
        invoice = db.get(Invoice, invoice_id) if invoice_id else None
        token = _safe_filename_token(
            str(getattr(invoice, "invoice_no", "") or ""),
            fallback=f"invoice-{invoice_id or 'job'}",
        )
        return f"Invoice-{token}.{extension}"

    if normalized_document_type == DOCUMENT_TYPE_WTN:
        ticket = db.get(Ticket, ticket_id) if ticket_id else None
        token = _safe_filename_token(
            str(getattr(ticket, "ticket_no", "") or ""),
            fallback=f"wtn-{ticket_id or 'job'}",
        )
        return f"WTN-{token}.{extension}"

    ticket = db.get(Ticket, ticket_id) if ticket_id else None
    token = _safe_filename_token(
        str(getattr(ticket, "ticket_no", "") or ""),
        fallback=f"ticket-{ticket_id or 'job'}",
    )
    return f"Ticket-{token}.{extension}"


def resolve_job_document_filename(db: Session, job: PrintJob) -> str:
    payload_format = str(job.payload_format or "").strip() or _content_type_from_rendered(
        str(job.rendered_content or "")
    )
    return resolve_document_filename(
        db,
        document_type=str(job.document_type or ""),
        ticket_id=int(job.ticket_id) if job.ticket_id else None,
        invoice_id=int(job.invoice_id) if job.invoice_id else None,
        payload_format=payload_format,
        payload_mime_type=str(job.payload_mime_type or "").strip() or None,
    )


def _job_rendered_content_for_resend(
    job: PrintJob,
    *,
    payload_bytes: bytes,
    payload_format: str,
) -> str:
    stored_rendered_content = str(job.rendered_content or "")
    if stored_rendered_content:
        return stored_rendered_content
    normalized_payload_format = _normalize_content_type(payload_format)
    if normalized_payload_format in {
        PRINT_CONTENT_TYPE_TEXT,
        PRINT_CONTENT_TYPE_HTML,
    }:
        return payload_bytes.decode("utf-8", errors="replace")
    return ""


def resolve_job_payload(job: PrintJob) -> tuple[bytes, str, str]:
    payload_bytes, _ = _job_bytes_for_retry(job)
    rendered_content = _job_rendered_content_for_resend(
        job,
        payload_bytes=payload_bytes,
        payload_format=str(job.payload_format or ""),
    )
    payload_format, payload_mime_type = _payload_metadata_for_resend(job, rendered_content)
    return payload_bytes, payload_format, payload_mime_type


def _apply_job_delivery_success(job: PrintJob) -> None:
    job.status = PRINT_JOB_STATUS_SENT
    job.last_error = None
    job.sent_at = utcnow()


def _apply_job_delivery_failure(job: PrintJob, exc: Exception) -> None:
    job.status = PRINT_JOB_STATUS_FAILED
    job.last_error = str(exc) or "Delivery failed."


def _snapshot_delivery_config_for_job(delivery_type: str, delivery_config: dict) -> dict:
    normalized = _normalize_delivery_type(delivery_type)
    if normalized == DELIVERY_TYPE_PRINT_LOCAL_BROWSER:
        return {}
    snapshot = dict(delivery_config or {})
    if normalized == DELIVERY_TYPE_PRINT_NODE_HTTP and snapshot.get("api_key"):
        snapshot["api_key"] = "REDACTED"
    return snapshot


def _effective_delivery_config_for_job(db: Session, job: PrintJob) -> dict[str, Any]:
    snapshot = (
        dict(job.delivery_config_json)
        if isinstance(job.delivery_config_json, dict)
        else {}
    )
    delivery_type = _normalize_delivery_type(job.delivery_type)
    if delivery_type != DELIVERY_TYPE_PRINT_NODE_HTTP:
        return snapshot

    if str(snapshot.get("api_key", "")).strip().upper() == "REDACTED":
        snapshot.pop("api_key", None)

    destination_id = int(job.destination_id or 0)
    if destination_id <= 0:
        return snapshot

    destination = db.get(PrintDestination, destination_id)
    if destination is None or not isinstance(destination.delivery_config, dict):
        return snapshot

    live_config = dict(destination.delivery_config or {})
    merged = dict(live_config)
    for key, value in snapshot.items():
        if key == "api_key":
            continue
        merged[key] = value

    live_api_key = str(live_config.get("api_key", "")).strip()
    if live_api_key:
        merged["api_key"] = live_api_key
    else:
        merged.pop("api_key", None)

    return merged


def _apply_transport_result(job: PrintJob, result: PrintTransportResult | None) -> None:
    if result is None:
        return
    if result.provider_job_ref is not None:
        job.provider_job_ref = result.provider_job_ref
    if result.provider_response_json is not None:
        job.provider_response_json = result.provider_response_json


def _apply_transport_exception_metadata(job: PrintJob, exc: Exception) -> None:
    provider_job_ref = getattr(exc, "provider_job_ref", None)
    if provider_job_ref is not None:
        job.provider_job_ref = provider_job_ref
    provider_response_json = getattr(exc, "provider_response_json", None)
    if provider_response_json is not None:
        job.provider_response_json = provider_response_json


def _delivery_type_to_send_mode(delivery_type: str) -> PrintMode:
    normalized = _normalize_delivery_type(delivery_type)
    if normalized == DELIVERY_TYPE_PRINT_NETWORK_RAW_9100:
        return "network"
    if normalized == DELIVERY_TYPE_PRINT_LOCAL_BROWSER:
        return "local_browser"
    if normalized == DELIVERY_TYPE_PRINT_NODE_HTTP:
        return "local_node_http"
    raise ValueError(f"Unsupported destination delivery type: {delivery_type}")


def _prepare_html_send_payload(
    rendered_content: str,
    *,
    base_url: str | None,
) -> tuple[str, bytes]:
    from .pdf import ensure_single_page_html, prepare_html_for_print_output

    safe_html = prepare_html_for_print_output(rendered_content)
    ensure_single_page_html(safe_html, base_url=base_url)
    serialized = safe_html.encode("utf-8")
    return safe_html, serialized


def execute_rendered_print(
    db: Session,
    *,
    document_type: str,
    rendered_content: str,
    content_type: str,
    delivery_type: str,
    delivery_config: dict,
    destination_id: int | None = None,
    template_id: int | None = None,
    ticket_id: int | None = None,
    invoice_id: int | None = None,
    created_by_user_id: int | None = None,
    payload_bytes: bytes | None = None,
    base_url: str | None = None,
    email_subject: str | None = None,
    email_body: str | None = None,
    email_sender: Callable[..., None] | None = None,
    trigger_source: str = PRINT_JOB_TRIGGER_SOURCE_MANUAL,
) -> PrintExecutionResult:
    normalized_delivery_type = _normalize_delivery_type(delivery_type)
    normalized_document_type = _normalize_document_type(document_type)
    normalized_content_type = _normalize_content_type(content_type)
    normalized_trigger_source = _normalize_print_job_trigger_source(trigger_source)
    stored_rendered_content = str(rendered_content or "")
    prepared_payload = _prepare_delivery_payload(
        rendered_content=stored_rendered_content,
        content_type=normalized_content_type,
        delivery_type=normalized_delivery_type,
        payload_bytes=payload_bytes,
        base_url=base_url,
    )
    transport_content = prepared_payload.transport_content
    serialized_payload = prepared_payload.payload_bytes
    initial_status = (
        PRINT_JOB_STATUS_PENDING
        if normalized_delivery_type == DELIVERY_TYPE_PRINT_AGENT_PULL
        else PRINT_JOB_STATUS_QUEUED
    )

    job = PrintJob(
        created_by_user_id=created_by_user_id,
        document_type=normalized_document_type,
        destination_id=destination_id,
        template_id=template_id,
        ticket_id=ticket_id,
        invoice_id=invoice_id,
        delivery_type=normalized_delivery_type,
        delivery_config_json=_snapshot_delivery_config_for_job(
            normalized_delivery_type,
            delivery_config,
        ),
        rendered_content=stored_rendered_content,
        rendered_bytes_base64=_serialized_bytes(serialized_payload),
        payload_format=prepared_payload.payload_format,
        payload_mime_type=prepared_payload.payload_mime_type,
        trigger_source=normalized_trigger_source,
        status=initial_status,
        attempt_count=0,
        last_error=None,
        sent_at=None,
    )
    db.add(job)
    db.flush()

    if normalized_delivery_type != DELIVERY_TYPE_PRINT_AGENT_PULL:
        job.attempt_count = int(job.attempt_count or 0) + 1
    try:
        if normalized_delivery_type == DELIVERY_TYPE_PRINT_AGENT_PULL:
            job.status = PRINT_JOB_STATUS_PENDING
            db.commit()
            return PrintExecutionResult(job=job)

        if normalized_delivery_type == DELIVERY_TYPE_PRINT_LOCAL_BROWSER:
            _apply_job_delivery_success(job)
            db.commit()
            return PrintExecutionResult(
                job=job,
                browser_content=transport_content,
                browser_content_type=job.payload_format,
            )

        if normalized_delivery_type == DELIVERY_TYPE_EMAIL_PDF:
            sender = email_sender or send_delivery_email
            sender(
                config=dict(delivery_config or {}),
                document_type=normalized_document_type,
                payload_bytes=serialized_payload,
                payload_content_type=job.payload_format or prepared_payload.payload_format,
                invoice_id=invoice_id,
                ticket_id=ticket_id,
                job_id=job.id,
                subject=email_subject,
                body=email_body,
                db=db,
            )
            _apply_job_delivery_success(job)
            db.commit()
            return PrintExecutionResult(job=job)

        transport_result = send_print_job(
            serialized_payload,
            _delivery_type_to_send_mode(normalized_delivery_type),
            dict(delivery_config or {}),
            document_type=job.document_type,
            rendered_content=transport_content,
            content_type=job.payload_format or prepared_payload.payload_format,
            job_id=job.id,
            job_name=f"{job.document_type} print job {job.id}",
            document_filename=resolve_document_filename(
                db,
                document_type=normalized_document_type,
                ticket_id=ticket_id,
                invoice_id=invoice_id,
                payload_format=job.payload_format or prepared_payload.payload_format,
                payload_mime_type=job.payload_mime_type or prepared_payload.payload_mime_type,
            ),
            payload_format=job.payload_format or prepared_payload.payload_format,
            payload_mime_type=job.payload_mime_type or prepared_payload.payload_mime_type,
        )
        if normalized_delivery_type == DELIVERY_TYPE_PRINT_NODE_HTTP and isinstance(
            transport_result, PrintTransportResult
        ):
            _apply_transport_result(job, transport_result)
        _apply_job_delivery_success(job)
        db.commit()
        return PrintExecutionResult(job=job)
    except (RuntimeError, ValueError, OSError, NotImplementedError) as exc:
        _apply_transport_exception_metadata(job, exc)
        _apply_job_delivery_failure(job, exc)
        db.commit()
        raise


def retry_print_job(db: Session, job: PrintJob) -> PrintExecutionResult:
    delivery_type = _normalize_delivery_type(job.delivery_type)
    delivery_config = _effective_delivery_config_for_job(db, job)
    payload_bytes, _ = _job_bytes_for_retry(job)
    payload_format, payload_mime_type = _payload_metadata_for_resend(
        job,
        _job_rendered_content_for_resend(
            job,
            payload_bytes=payload_bytes,
            payload_format=str(job.payload_format or ""),
        ),
    )
    rendered_content = _job_rendered_content_for_resend(
        job,
        payload_bytes=payload_bytes,
        payload_format=payload_format,
    )
    prepared_payload = _prepare_delivery_payload(
        rendered_content=rendered_content,
        content_type=payload_format,
        delivery_type=delivery_type,
        payload_bytes=payload_bytes,
        base_url=None,
    )
    transport_content = prepared_payload.transport_content
    payload_bytes = prepared_payload.payload_bytes
    payload_format = prepared_payload.payload_format
    payload_mime_type = prepared_payload.payload_mime_type

    if delivery_type != DELIVERY_TYPE_PRINT_AGENT_PULL:
        job.attempt_count = int(job.attempt_count or 0) + 1
    try:
        job.rendered_content = rendered_content
        job.rendered_bytes_base64 = _serialized_bytes(payload_bytes)
        job.payload_format = payload_format
        job.payload_mime_type = payload_mime_type

        if delivery_type == DELIVERY_TYPE_PRINT_AGENT_PULL:
            job.provider_job_ref = None
            job.provider_response_json = None
            job.last_error = None
            job.sent_at = None
            job.agent_id = None
            job.status = PRINT_JOB_STATUS_PENDING
            db.commit()
            return PrintExecutionResult(job=job)

        if delivery_type == DELIVERY_TYPE_PRINT_LOCAL_BROWSER:
            _apply_job_delivery_success(job)
            db.commit()
            return PrintExecutionResult(
                job=job,
                browser_content=transport_content,
                browser_content_type=payload_format,
            )

        if delivery_type == DELIVERY_TYPE_EMAIL_PDF:
            send_delivery_email(
                config=delivery_config,
                document_type=_normalize_document_type(job.document_type),
                payload_bytes=payload_bytes,
                payload_content_type=payload_format,
                invoice_id=job.invoice_id,
                ticket_id=job.ticket_id,
                job_id=job.id,
                subject=None,
                body=None,
                db=db,
            )
            _apply_job_delivery_success(job)
            db.commit()
            return PrintExecutionResult(job=job)

        transport_result = send_print_job(
            payload_bytes,
            _delivery_type_to_send_mode(delivery_type),
            delivery_config,
            document_type=str(job.document_type or ""),
            rendered_content=transport_content,
            content_type=payload_format,
            job_id=job.id,
            job_name=f"{str(job.document_type or '').strip().upper() or 'PRINT'} print job {job.id}",
            document_filename=resolve_job_document_filename(db, job),
            payload_format=job.payload_format or payload_format or "",
            payload_mime_type=job.payload_mime_type or payload_mime_type or "",
        )
        if delivery_type == DELIVERY_TYPE_PRINT_NODE_HTTP and isinstance(
            transport_result, PrintTransportResult
        ):
            _apply_transport_result(job, transport_result)
        _apply_job_delivery_success(job)
        db.commit()
        return PrintExecutionResult(job=job)
    except (RuntimeError, ValueError, OSError, NotImplementedError) as exc:
        _apply_transport_exception_metadata(job, exc)
        _apply_job_delivery_failure(job, exc)
        db.commit()
        raise


def replay_print_job(
    db: Session,
    job: PrintJob,
    *,
    created_by_user_id: int | None = None,
) -> PrintExecutionResult:
    payload_bytes, _ = _job_bytes_for_retry(job)
    rendered_content = _job_rendered_content_for_resend(
        job,
        payload_bytes=payload_bytes,
        payload_format=str(job.payload_format or ""),
    )
    payload_format, _payload_mime_type = _payload_metadata_for_resend(job, rendered_content)
    content_type = payload_format
    delivery_config = _effective_delivery_config_for_job(db, job)
    return execute_rendered_print(
        db,
        document_type=str(job.document_type or "").strip().upper(),
        rendered_content=rendered_content,
        content_type=content_type,
        delivery_type=str(job.delivery_type or "").strip().upper(),
        delivery_config=delivery_config,
        destination_id=job.destination_id,
        template_id=job.template_id,
        ticket_id=job.ticket_id,
        invoice_id=job.invoice_id,
        created_by_user_id=created_by_user_id,
        payload_bytes=payload_bytes,
    )
