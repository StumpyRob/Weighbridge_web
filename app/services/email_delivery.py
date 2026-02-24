from __future__ import annotations

from email.message import EmailMessage
import smtplib
from typing import Any

from ..config import settings


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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
) -> None:
    to_addresses = _parse_recipients(config.get("to"))
    cc_addresses = _parse_recipients(config.get("cc"))
    bcc_addresses = _parse_recipients(config.get("bcc"))

    if not to_addresses:
        raise ValueError("EMAIL_PDF destination requires at least one recipient.")

    smtp_host = str(
        config.get("smtp_host") or getattr(settings, "smtp_host", "") or ""
    ).strip()
    smtp_port = _as_int(
        config.get("smtp_port") or getattr(settings, "smtp_port", 587),
        587,
    )
    smtp_username = str(
        config.get("smtp_username") or getattr(settings, "smtp_username", "") or ""
    ).strip()
    smtp_password = str(
        config.get("smtp_password") or getattr(settings, "smtp_password", "") or ""
    )
    smtp_use_tls = _as_bool(
        config.get("smtp_use_tls")
        if "smtp_use_tls" in config
        else getattr(settings, "smtp_use_tls", True),
        True,
    )
    smtp_use_ssl = _as_bool(
        config.get("smtp_use_ssl")
        if "smtp_use_ssl" in config
        else getattr(settings, "smtp_use_ssl", False),
        False,
    )

    from_email = str(
        config.get("from_email") or getattr(settings, "smtp_from_email", "") or ""
    ).strip()
    if not from_email:
        from_email = "no-reply@localhost"

    if not smtp_host:
        raise RuntimeError("SMTP host is not configured for EMAIL_PDF destination.")

    resolved_subject = str(subject or config.get("email_subject_template") or "").strip()
    if not resolved_subject:
        resolved_subject = f"{document_type.title()} delivery (Job {job_id})"

    resolved_body = str(body or config.get("email_body_template") or "").strip()
    if not resolved_body:
        resolved_body = "Please find the attached document."

    message = EmailMessage()
    message["Subject"] = resolved_subject
    message["From"] = from_email
    message["To"] = ", ".join(to_addresses)
    if cc_addresses:
        message["Cc"] = ", ".join(cc_addresses)
    message.set_content(resolved_body)

    payload_format = str(payload_content_type or "").strip().upper()
    filename = _attachment_filename(document_type, invoice_id, ticket_id)
    if payload_format == "PDF":
        maintype, subtype = "application", "pdf"
        if not filename.lower().endswith(".pdf"):
            filename = f"{filename}.pdf"
    elif payload_format == "HTML":
        maintype, subtype = "text", "html"
    else:
        maintype, subtype = "text", "plain"

    message.add_attachment(
        payload_bytes,
        maintype=maintype,
        subtype=subtype,
        filename=filename,
    )

    recipients = to_addresses + cc_addresses + bcc_addresses

    if smtp_use_ssl:
        smtp_client: smtplib.SMTP = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=20)
    else:
        smtp_client = smtplib.SMTP(smtp_host, smtp_port, timeout=20)

    try:
        smtp_client.ehlo()
        if smtp_use_tls and not smtp_use_ssl:
            smtp_client.starttls()
            smtp_client.ehlo()
        if smtp_username:
            smtp_client.login(smtp_username, smtp_password)
        smtp_client.send_message(message, from_addr=from_email, to_addrs=recipients)
    finally:
        smtp_client.quit()
