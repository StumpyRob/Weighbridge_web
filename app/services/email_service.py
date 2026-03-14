from __future__ import annotations

from dataclasses import asdict, dataclass
from email.message import EmailMessage
from email.utils import formataddr
import logging
import smtplib
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import normalize_email, validate_email
from ..models import PlatformSetting

logger = logging.getLogger(__name__)

SMTP_SECURITY_NONE = "none"
SMTP_SECURITY_STARTTLS = "starttls"
SMTP_SECURITY_SSL = "ssl"
SMTP_SECURITY_OPTIONS = (
    SMTP_SECURITY_STARTTLS,
    SMTP_SECURITY_SSL,
    SMTP_SECURITY_NONE,
)
DEFAULT_SMTP_PORT = 587
DEFAULT_SMTP_TIMEOUT_SECONDS = 20
PLATFORM_EMAIL_AUDIT_FIELDS = (
    "smtp_host",
    "smtp_port",
    "smtp_username",
    "smtp_security",
    "smtp_from_email",
    "smtp_from_display_name",
    "smtp_reply_to",
    "smtp_password_configured",
)


@dataclass(frozen=True)
class PlatformEmailSettingsState:
    smtp_host: str = ""
    smtp_port: int = DEFAULT_SMTP_PORT
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_security: str = SMTP_SECURITY_STARTTLS
    smtp_from_email: str = ""
    smtp_from_display_name: str = ""
    smtp_reply_to: str = ""

    @property
    def smtp_password_configured(self) -> bool:
        return bool(self.smtp_password)

    @property
    def is_ready(self) -> bool:
        return bool(self.smtp_host and self.smtp_from_email)

    def snapshot(self) -> dict[str, object]:
        data = asdict(self)
        data.pop("smtp_password", None)
        data["smtp_password_configured"] = self.smtp_password_configured
        return data

@dataclass(frozen=True)
class EmailAttachment:
    filename: str
    content_bytes: bytes
    content_type: str = "application/octet-stream"


@dataclass(frozen=True)
class EmailSendResult:
    ok: bool
    error: str | None = None


def _platform_setting_row(db: Session) -> PlatformSetting | None:
    return db.execute(
        select(PlatformSetting).order_by(PlatformSetting.id.asc()).limit(1)
    ).scalars().first()


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _coerce_port(value: object, *, default: int) -> int:
    text = _clean_text(value)
    if not text:
        return default
    try:
        port = int(text)
    except (TypeError, ValueError) as exc:
        raise ValueError("SMTP port must be a whole number.") from exc
    if port < 1 or port > 65535:
        raise ValueError("SMTP port must be between 1 and 65535.")
    return port

def _coerce_security_mode(value: object, *, default: str) -> str:
    normalized = _clean_text(value).lower()
    if not normalized:
        return default
    if normalized not in SMTP_SECURITY_OPTIONS:
        raise ValueError("Select a valid SMTP security mode.")
    return normalized


def _coerce_optional_email(value: object, *, label: str) -> str:
    normalized = normalize_email(value)
    if not normalized:
        return ""
    if not validate_email(normalized):
        raise ValueError(f"{label} must be a valid email address.")
    return normalized


def platform_email_settings_defaults() -> PlatformEmailSettingsState:
    return PlatformEmailSettingsState(
        smtp_security=SMTP_SECURITY_STARTTLS,
    )


def validate_platform_email_settings(**values: object) -> PlatformEmailSettingsState:
    defaults = platform_email_settings_defaults()
    return PlatformEmailSettingsState(
        smtp_host=_clean_text(values.get("smtp_host")),
        smtp_port=_coerce_port(values.get("smtp_port"), default=defaults.smtp_port),
        smtp_username=_clean_text(values.get("smtp_username")),
        smtp_password="",
        smtp_security=_coerce_security_mode(
            values.get("smtp_security"),
            default=defaults.smtp_security,
        ),
        smtp_from_email=_coerce_optional_email(
            values.get("smtp_from_email"),
            label="From email address",
        ),
        smtp_from_display_name=_clean_text(values.get("smtp_from_display_name")),
        smtp_reply_to=_coerce_optional_email(
            values.get("smtp_reply_to"),
            label="Reply-to address",
        ),
    )


def get_platform_email_settings(db: Session) -> PlatformEmailSettingsState:
    defaults = platform_email_settings_defaults()
    row = _platform_setting_row(db)
    if row is None:
        return defaults

    return PlatformEmailSettingsState(
        smtp_host=_clean_text(getattr(row, "smtp_host", None)) or defaults.smtp_host,
        smtp_port=_coerce_port(getattr(row, "smtp_port", None), default=defaults.smtp_port),
        smtp_username=_clean_text(getattr(row, "smtp_username", None)) or defaults.smtp_username,
        smtp_password=str(getattr(row, "smtp_password", None) or "") or defaults.smtp_password,
        smtp_security=_coerce_security_mode(
            getattr(row, "smtp_security", None),
            default=defaults.smtp_security,
        ),
        smtp_from_email=_coerce_optional_email(
            getattr(row, "smtp_from_email", None) or defaults.smtp_from_email,
            label="From email address",
        ),
        smtp_from_display_name=(
            _clean_text(getattr(row, "smtp_from_display_name", None))
            or defaults.smtp_from_display_name
        ),
        smtp_reply_to=_coerce_optional_email(
            getattr(row, "smtp_reply_to", None) or defaults.smtp_reply_to,
            label="Reply-to address",
        ),
    )


def save_platform_email_settings(
    db: Session,
    settings_state: PlatformEmailSettingsState,
    *,
    smtp_password: str | None = None,
    clear_smtp_password: bool = False,
) -> PlatformEmailSettingsState:
    row = _platform_setting_row(db)
    if row is None:
        row = PlatformSetting()
        db.add(row)

    row.smtp_host = settings_state.smtp_host or None
    row.smtp_port = int(settings_state.smtp_port)
    row.smtp_username = settings_state.smtp_username or None
    row.smtp_from_email = settings_state.smtp_from_email or None
    row.smtp_from_display_name = settings_state.smtp_from_display_name or None
    row.smtp_reply_to = settings_state.smtp_reply_to or None
    row.smtp_security = settings_state.smtp_security or None
    if clear_smtp_password:
        row.smtp_password = None
    elif smtp_password is not None:
        row.smtp_password = smtp_password or None
    db.flush()
    return get_platform_email_settings(db)


def platform_email_settings_snapshot(settings_state: PlatformEmailSettingsState) -> dict[str, object]:
    return settings_state.snapshot()

def _resolve_transport_settings(
    db: Session | None,
    *,
    config_overrides: Mapping[str, Any] | None = None,
) -> PlatformEmailSettingsState:
    overrides = config_overrides or {}
    base = get_platform_email_settings(db) if db is not None else platform_email_settings_defaults()
    return PlatformEmailSettingsState(
        smtp_host=_clean_text(overrides.get("smtp_host")) or base.smtp_host,
        smtp_port=_coerce_port(overrides.get("smtp_port"), default=base.smtp_port),
        smtp_username=_clean_text(overrides.get("smtp_username")) or base.smtp_username,
        smtp_password=str(overrides.get("smtp_password") or "") or base.smtp_password,
        smtp_security=_coerce_security_mode(
            overrides.get("smtp_security"),
            default=base.smtp_security,
        ),
        smtp_from_email=_coerce_optional_email(
            overrides.get("from_email") or base.smtp_from_email,
            label="From email address",
        ),
        smtp_from_display_name=(
            _clean_text(overrides.get("from_display_name"))
            or base.smtp_from_display_name
        ),
        smtp_reply_to=_coerce_optional_email(
            overrides.get("reply_to") or base.smtp_reply_to,
            label="Reply-to address",
        ),
    )


def _parse_recipients(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        values = [normalize_email(item) for item in raw]
        return [item for item in values if item]
    text = str(raw).replace(";", ",")
    values = [normalize_email(item) for item in text.split(",")]
    return [item for item in values if item]


def _validate_recipients(addresses: list[str], *, label: str) -> None:
    for address in addresses:
        if not validate_email(address):
            raise ValueError(f"{label} includes an invalid email address.")


def _attachment_parts(content_type: str) -> tuple[str, str]:
    raw = _clean_text(content_type).lower()
    if "/" not in raw:
        return "application", "octet-stream"
    maintype, subtype = raw.split("/", 1)
    maintype = maintype or "application"
    subtype = subtype or "octet-stream"
    return maintype, subtype


def _friendly_error(exc: Exception) -> str:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "SMTP authentication failed."
    if isinstance(exc, smtplib.SMTPConnectError):
        return "SMTP connection failed."
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "SMTP rejected the recipient address."
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return "SMTP server disconnected unexpectedly."
    if isinstance(exc, TimeoutError):
        return "SMTP connection timed out."
    return "Email send failed."


def _format_from_header(settings_state: PlatformEmailSettingsState) -> str:
    if settings_state.smtp_from_display_name:
        return formataddr((settings_state.smtp_from_display_name, settings_state.smtp_from_email))
    return settings_state.smtp_from_email


def send_email(
    *,
    subject: str,
    text_body: str,
    to: list[str] | tuple[str, ...] | str,
    db: Session | None = None,
    html_body: str | None = None,
    cc: list[str] | tuple[str, ...] | str | None = None,
    bcc: list[str] | tuple[str, ...] | str | None = None,
    attachments: list[EmailAttachment] | None = None,
    config_overrides: Mapping[str, Any] | None = None,
) -> EmailSendResult:
    transport = PlatformEmailSettingsState()
    try:
        to_addresses = _parse_recipients(to)
        cc_addresses = _parse_recipients(cc)
        bcc_addresses = _parse_recipients(bcc)
        if not to_addresses:
            raise ValueError("At least one recipient email address is required.")
        _validate_recipients(to_addresses, label="To")
        _validate_recipients(cc_addresses, label="Cc")
        _validate_recipients(bcc_addresses, label="Bcc")

        transport = _resolve_transport_settings(db, config_overrides=config_overrides)
        if not transport.smtp_host:
            raise ValueError("SMTP host is not configured.")
        if not transport.smtp_from_email:
            raise ValueError("From email address is not configured.")

        message = EmailMessage()
        message["Subject"] = _clean_text(subject) or "Weighbridge Web email"
        message["From"] = _format_from_header(transport)
        message["To"] = ", ".join(to_addresses)
        if cc_addresses:
            message["Cc"] = ", ".join(cc_addresses)
        if transport.smtp_reply_to:
            message["Reply-To"] = transport.smtp_reply_to

        resolved_text_body = str(text_body or "").strip() or " "
        message.set_content(resolved_text_body)
        if html_body:
            message.add_alternative(str(html_body), subtype="html")

        for attachment in attachments or []:
            maintype, subtype = _attachment_parts(attachment.content_type)
            message.add_attachment(
                attachment.content_bytes,
                maintype=maintype,
                subtype=subtype,
                filename=attachment.filename,
            )

        recipients = to_addresses + cc_addresses + bcc_addresses
        smtp_client: smtplib.SMTP
        if transport.smtp_security == SMTP_SECURITY_SSL:
            smtp_client = smtplib.SMTP_SSL(
                transport.smtp_host,
                transport.smtp_port,
                timeout=DEFAULT_SMTP_TIMEOUT_SECONDS,
            )
        else:
            smtp_client = smtplib.SMTP(
                transport.smtp_host,
                transport.smtp_port,
                timeout=DEFAULT_SMTP_TIMEOUT_SECONDS,
            )

        try:
            smtp_client.ehlo()
            if transport.smtp_security == SMTP_SECURITY_STARTTLS:
                smtp_client.starttls()
                smtp_client.ehlo()
            if transport.smtp_username:
                smtp_client.login(transport.smtp_username, transport.smtp_password)
            smtp_client.send_message(
                message,
                from_addr=transport.smtp_from_email,
                to_addrs=recipients,
            )
        finally:
            try:
                smtp_client.quit()
            except Exception:
                pass
        return EmailSendResult(ok=True)
    except ValueError as exc:
        return EmailSendResult(ok=False, error=str(exc))
    except Exception as exc:
        logger.exception(
            "Outbound email send failed: host=%s port=%s security=%s",
            transport.smtp_host,
            transport.smtp_port,
            transport.smtp_security,
        )
        return EmailSendResult(ok=False, error=_friendly_error(exc))


def send_email_with_attachment(
    *,
    subject: str,
    text_body: str,
    to: list[str] | tuple[str, ...] | str,
    attachment: EmailAttachment,
    db: Session | None = None,
    html_body: str | None = None,
    cc: list[str] | tuple[str, ...] | str | None = None,
    bcc: list[str] | tuple[str, ...] | str | None = None,
    config_overrides: Mapping[str, Any] | None = None,
) -> EmailSendResult:
    return send_email(
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        to=to,
        cc=cc,
        bcc=bcc,
        attachments=[attachment],
        db=db,
        config_overrides=config_overrides,
    )
