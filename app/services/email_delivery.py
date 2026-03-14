from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from .email_service import EmailAttachment, send_email_with_attachment


def _parse_recipients(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        values = [str(item).strip() for item in raw]
        return [item for item in values if item]
    text = str(raw).replace(";", ",")
    values = [item.strip() for item in text.split(",")]
    return [item for item in values if item]


def _attachment_filename(document_type: str, invoice_id: int | None, ticket_id: int | None) -> str:
    normalized = str(document_type or "").strip().upper()
    if normalized == "INVOICE" and invoice_id:
        return f"invoice-{invoice_id}.pdf"
    if normalized == "TICKET" and ticket_id:
        return f"ticket-{ticket_id}.txt"
    return "document-output.bin"


def _attachment_content_type(payload_content_type: str) -> str:
    normalized = str(payload_content_type or "").strip().upper()
    if normalized == "PDF":
        return "application/pdf"
    if normalized == "HTML":
        return "text/html"
    return "text/plain"


def send_delivery_email(
    *,
    config: dict,
    document_type: str,
    payload_bytes: bytes,
    payload_content_type: str,
    invoice_id: int | None,
    ticket_id: int | None,
    job_id: int,
    subject: str | None = None,
    body: str | None = None,
    db: Session | None = None,
) -> None:
    to_addresses = _parse_recipients(config.get("to"))
    cc_addresses = _parse_recipients(config.get("cc"))
    bcc_addresses = _parse_recipients(config.get("bcc"))
    if not to_addresses:
        raise ValueError("EMAIL_PDF destination requires at least one recipient.")

    resolved_subject = str(subject or config.get("email_subject_template") or "").strip()
    if not resolved_subject:
        resolved_subject = f"{str(document_type or '').title()} delivery (Job {job_id})"

    resolved_body = str(body or config.get("email_body_template") or "").strip()
    if not resolved_body:
        resolved_body = "Please find the attached document."

    result = send_email_with_attachment(
        subject=resolved_subject,
        text_body=resolved_body,
        to=to_addresses,
        cc=cc_addresses,
        bcc=bcc_addresses,
        attachment=EmailAttachment(
            filename=_attachment_filename(document_type, invoice_id, ticket_id),
            content_bytes=payload_bytes,
            content_type=_attachment_content_type(payload_content_type),
        ),
        db=db,
        config_overrides=config,
    )
    if not result.ok:
        raise RuntimeError(result.error or "Email send failed.")
