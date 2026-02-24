from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.orm import Session

from ..models import PrintDestination, PrintJob, PrintTemplate
from ..models.base import utcnow
from .email_delivery import send_delivery_email
from .print_render import render_from_content
from .print_transport import PrintMode, send as send_print_job

PRINT_JOB_STATUS_QUEUED = "QUEUED"
PRINT_JOB_STATUS_SENT = "SENT"
PRINT_JOB_STATUS_FAILED = "FAILED"

PRINT_CONTENT_TYPE_TEXT = "TEXT"
PRINT_CONTENT_TYPE_HTML = "HTML"
PRINT_CONTENT_TYPE_PDF = "PDF"

DOCUMENT_TYPE_TICKET = "TICKET"
DOCUMENT_TYPE_INVOICE = "INVOICE"
DOCUMENT_TYPE_WTN = "WTN"

DELIVERY_TYPE_PRINT_LOCAL_BROWSER = "PRINT_LOCAL_BROWSER"
DELIVERY_TYPE_PRINT_NETWORK_RAW_9100 = "PRINT_NETWORK_RAW_9100"
DELIVERY_TYPE_PRINT_NODE_HTTP = "PRINT_NODE_HTTP"
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
        DELIVERY_TYPE_PRINT_LOCAL_BROWSER: DELIVERY_TYPE_PRINT_LOCAL_BROWSER,
        DELIVERY_TYPE_PRINT_NETWORK_RAW_9100: DELIVERY_TYPE_PRINT_NETWORK_RAW_9100,
        DELIVERY_TYPE_PRINT_NODE_HTTP: DELIVERY_TYPE_PRINT_NODE_HTTP,
        DELIVERY_TYPE_EMAIL_PDF: DELIVERY_TYPE_EMAIL_PDF,
    }
    return mapping.get(normalized, DELIVERY_TYPE_PRINT_LOCAL_BROWSER)


def resolve_destination_transport(destination: PrintDestination) -> tuple[PrintMode, dict]:
    delivery_type = _normalize_delivery_type(destination.delivery_type)
    delivery_config = (
        dict(destination.delivery_config)
        if isinstance(destination.delivery_config, dict)
        else {}
    )
    if delivery_type == DELIVERY_TYPE_PRINT_NETWORK_RAW_9100:
        return "network", delivery_config
    if delivery_type == DELIVERY_TYPE_PRINT_LOCAL_BROWSER:
        return "local_browser", delivery_config
    if delivery_type == DELIVERY_TYPE_PRINT_NODE_HTTP:
        return "local_node_http", delivery_config
    raise ValueError(f"Unsupported destination delivery type: {destination.delivery_type}")


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
    return dict(delivery_config or {})


def _delivery_type_to_send_mode(delivery_type: str) -> PrintMode:
    normalized = _normalize_delivery_type(delivery_type)
    if normalized == DELIVERY_TYPE_PRINT_NETWORK_RAW_9100:
        return "network"
    if normalized == DELIVERY_TYPE_PRINT_LOCAL_BROWSER:
        return "local_browser"
    if normalized == DELIVERY_TYPE_PRINT_NODE_HTTP:
        return "local_node_http"
    raise ValueError(f"Unsupported destination delivery type: {delivery_type}")


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
    email_subject: str | None = None,
    email_body: str | None = None,
    email_sender: Callable[..., None] | None = None,
) -> PrintExecutionResult:
    normalized_delivery_type = _normalize_delivery_type(delivery_type)
    normalized_document_type = _normalize_document_type(document_type)
    normalized_content_type = _normalize_content_type(content_type)
    serialized_payload = payload_bytes or str(rendered_content or "").encode("utf-8")

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
        rendered_content=rendered_content,
        rendered_bytes_base64=_serialized_bytes(serialized_payload),
        status=PRINT_JOB_STATUS_QUEUED,
        attempt_count=0,
        last_error=None,
        sent_at=None,
    )
    db.add(job)
    db.flush()

    job.attempt_count = int(job.attempt_count or 0) + 1
    try:
        if normalized_delivery_type == DELIVERY_TYPE_PRINT_LOCAL_BROWSER:
            _apply_job_delivery_success(job)
            db.commit()
            return PrintExecutionResult(
                job=job,
                browser_content=rendered_content,
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
            )
            _apply_job_delivery_success(job)
            db.commit()
            return PrintExecutionResult(job=job)

        send_print_job(
            serialized_payload,
            _delivery_type_to_send_mode(normalized_delivery_type),
            dict(delivery_config or {}),
            document_type=job.document_type,
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
    delivery_type = _normalize_delivery_type(job.delivery_type)
    delivery_config = (
        dict(job.delivery_config_json)
        if isinstance(job.delivery_config_json, dict)
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
        if delivery_type == DELIVERY_TYPE_PRINT_LOCAL_BROWSER:
            _apply_job_delivery_success(job)
            db.commit()
            return PrintExecutionResult(
                job=job,
                browser_content=rendered_content,
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
            )
            _apply_job_delivery_success(job)
            db.commit()
            return PrintExecutionResult(job=job)

        send_print_job(
            payload_bytes,
            _delivery_type_to_send_mode(delivery_type),
            delivery_config,
            document_type=str(job.document_type or ""),
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
    delivery_config = (
        dict(job.delivery_config_json)
        if isinstance(job.delivery_config_json, dict)
        else {}
    )
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
