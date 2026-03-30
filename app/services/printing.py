from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.orm import Session

from ..models import PrintDestination, PrintJob, PrintTemplate
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

PRINT_CONTENT_TYPE_TEXT = "TEXT"
PRINT_CONTENT_TYPE_HTML = "HTML"
PRINT_CONTENT_TYPE_PDF = "PDF"
PRINT_CONTENT_MIME_TYPE_HTML = "text/html; charset=utf-8"
PRINT_CONTENT_MIME_TYPE_PDF = "application/pdf"

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


def _normalize_document_type(value: str | None) -> str:
    normalized = str(value or "").strip().upper()
    if normalized in {DOCUMENT_TYPE_TICKET, DOCUMENT_TYPE_INVOICE, DOCUMENT_TYPE_WTN}:
        return normalized
    return DOCUMENT_TYPE_TICKET


def _normalize_content_type(value: str | None) -> str:
    normalized = str(value or "").strip().upper()
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
    if normalized == PRINT_CONTENT_TYPE_HTML:
        return PRINT_CONTENT_MIME_TYPE_HTML
    if normalized == PRINT_CONTENT_TYPE_PDF:
        return PRINT_CONTENT_MIME_TYPE_PDF
    return NODE_HTTP_TEXT_MIME_TYPE


def resolve_job_payload(job: PrintJob) -> tuple[bytes, str, str]:
    payload_bytes, payload_source = _job_bytes_for_retry(job)
    rendered_content = (
        str(job.rendered_content or "")
        if payload_source == "text"
        else payload_bytes.decode("utf-8", errors="replace")
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
) -> PrintExecutionResult:
    normalized_delivery_type = _normalize_delivery_type(delivery_type)
    normalized_document_type = _normalize_document_type(document_type)
    normalized_content_type = _normalize_content_type(content_type)
    outbound_content = str(rendered_content or "")
    serialized_payload = payload_bytes or outbound_content.encode("utf-8")
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
        rendered_content=outbound_content,
        rendered_bytes_base64=_serialized_bytes(serialized_payload),
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
        if normalized_delivery_type == DELIVERY_TYPE_PRINT_NODE_HTTP:
            if normalized_content_type != PRINT_CONTENT_TYPE_TEXT:
                raise NotImplementedError("PRINT_NODE_HTTP currently supports TEXT payloads only.")
            serialized_payload = outbound_content.encode("utf-8")
            job.rendered_bytes_base64 = _serialized_bytes(serialized_payload)
            job.payload_format = PRINT_CONTENT_TYPE_TEXT
            job.payload_mime_type = NODE_HTTP_TEXT_MIME_TYPE

        if normalized_content_type == PRINT_CONTENT_TYPE_HTML:
            outbound_content, serialized_payload = _prepare_html_send_payload(
                outbound_content,
                base_url=base_url,
            )
            job.rendered_content = outbound_content
            job.rendered_bytes_base64 = _serialized_bytes(serialized_payload)

        if normalized_delivery_type == DELIVERY_TYPE_PRINT_AGENT_PULL:
            if normalized_content_type == PRINT_CONTENT_TYPE_TEXT:
                serialized_payload = payload_bytes or outbound_content.encode("utf-8")
                job.rendered_bytes_base64 = _serialized_bytes(serialized_payload)
            job.payload_format = normalized_content_type
            job.payload_mime_type = _payload_mime_type_for_content_type(normalized_content_type)
            job.status = PRINT_JOB_STATUS_PENDING
            db.commit()
            return PrintExecutionResult(job=job)

        if normalized_delivery_type == DELIVERY_TYPE_PRINT_LOCAL_BROWSER:
            _apply_job_delivery_success(job)
            db.commit()
            return PrintExecutionResult(
                job=job,
                browser_content=outbound_content,
                browser_content_type=normalized_content_type,
            )

        if normalized_delivery_type == DELIVERY_TYPE_EMAIL_PDF:
            sender = email_sender or send_delivery_email
            sender(
                config=dict(delivery_config or {}),
                document_type=normalized_document_type,
                payload_bytes=serialized_payload,
                payload_content_type=normalized_content_type,
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
            rendered_content=outbound_content,
            content_type=normalized_content_type,
            job_id=job.id,
            job_name=f"{job.document_type} print job {job.id}",
            payload_format=job.payload_format or "",
            payload_mime_type=job.payload_mime_type or "",
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
    payload_bytes, payload_source = _job_bytes_for_retry(job)
    rendered_content = (
        str(job.rendered_content or "")
        if payload_source == "text"
        else payload_bytes.decode("utf-8", errors="replace")
    )
    outbound_content = rendered_content
    payload_format, payload_mime_type = _payload_metadata_for_resend(job, rendered_content)
    normalized_content_type = payload_format

    if delivery_type != DELIVERY_TYPE_PRINT_AGENT_PULL:
        job.attempt_count = int(job.attempt_count or 0) + 1
    try:
        if delivery_type == DELIVERY_TYPE_PRINT_NODE_HTTP:
            if payload_format != PRINT_CONTENT_TYPE_TEXT:
                raise NotImplementedError("PRINT_NODE_HTTP currently supports TEXT payloads only.")
            job.payload_format = PRINT_CONTENT_TYPE_TEXT
            job.payload_mime_type = NODE_HTTP_TEXT_MIME_TYPE
            job.rendered_bytes_base64 = _serialized_bytes(payload_bytes)

        if normalized_content_type == PRINT_CONTENT_TYPE_HTML:
            outbound_content, payload_bytes = _prepare_html_send_payload(
                outbound_content,
                base_url=None,
            )
            job.rendered_content = outbound_content
            job.rendered_bytes_base64 = _serialized_bytes(payload_bytes)

        if delivery_type == DELIVERY_TYPE_PRINT_AGENT_PULL:
            job.payload_format = payload_format
            job.payload_mime_type = payload_mime_type or _payload_mime_type_for_content_type(
                payload_format
            )
            job.rendered_bytes_base64 = _serialized_bytes(payload_bytes)
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
                browser_content=outbound_content,
                browser_content_type=normalized_content_type,
            )

        if delivery_type == DELIVERY_TYPE_EMAIL_PDF:
            send_delivery_email(
                config=delivery_config,
                document_type=_normalize_document_type(job.document_type),
                payload_bytes=payload_bytes,
                payload_content_type=normalized_content_type,
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
            rendered_content=outbound_content,
            content_type=normalized_content_type,
            job_id=job.id,
            job_name=f"{str(job.document_type or '').strip().upper() or 'PRINT'} print job {job.id}",
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
    payload_bytes, payload_source = _job_bytes_for_retry(job)
    rendered_content = (
        str(job.rendered_content or "")
        if payload_source == "text"
        else payload_bytes.decode("utf-8", errors="replace")
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
