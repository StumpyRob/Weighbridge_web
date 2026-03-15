from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
from email.utils import formataddr
import logging
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import normalize_email, validate_email
from ..models import PlatformSetting

logger = logging.getLogger(__name__)

EMAIL_PROVIDER_RESEND = "resend"
SUPPORTED_EMAIL_PROVIDERS = (EMAIL_PROVIDER_RESEND,)
DEFAULT_RESEND_TIMEOUT_SECONDS = 20
RESEND_SEND_EMAIL_URL = "https://api.resend.com/emails"
PLATFORM_EMAIL_AUDIT_FIELDS = (
    "email_provider",
    "resend_api_key_configured",
    "from_email",
    "from_display_name",
    "reply_to",
)


@dataclass(frozen=True)
class PlatformEmailSettingsState:
    email_provider: str = EMAIL_PROVIDER_RESEND
    resend_api_key: str = ""
    from_email: str = ""
    from_display_name: str = ""
    reply_to: str = ""

    @property
    def resend_api_key_configured(self) -> bool:
        return bool(self.resend_api_key)

    @property
    def provider_label(self) -> str:
        return "Resend"

    @property
    def is_ready(self) -> bool:
        return bool(
            self.email_provider == EMAIL_PROVIDER_RESEND
            and self.resend_api_key
            and self.from_email
        )

    def snapshot(self) -> dict[str, object]:
        data = asdict(self)
        data.pop("resend_api_key", None)
        data["resend_api_key_configured"] = self.resend_api_key_configured
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


def _coerce_email_provider(value: object, *, default: str) -> str:
    normalized = _clean_text(value).lower()
    if not normalized:
        return default
    if normalized not in SUPPORTED_EMAIL_PROVIDERS:
        raise ValueError("Email provider must be resend.")
    return normalized


def _read_email_provider(value: object, *, default: str) -> str:
    try:
        return _coerce_email_provider(value, default=default)
    except ValueError:
        return default


def _coerce_optional_email(value: object, *, label: str) -> str:
    normalized = normalize_email(value)
    if not normalized:
        return ""
    if not validate_email(normalized):
        raise ValueError(f"{label} must be a valid email address.")
    return normalized


def _read_optional_email(value: object, *, default: str = "") -> str:
    normalized = normalize_email(value)
    if not normalized:
        return default
    if not validate_email(normalized):
        return default
    return normalized


def platform_email_settings_defaults() -> PlatformEmailSettingsState:
    return PlatformEmailSettingsState(email_provider=EMAIL_PROVIDER_RESEND)


def validate_platform_email_settings(**values: object) -> PlatformEmailSettingsState:
    defaults = platform_email_settings_defaults()
    return PlatformEmailSettingsState(
        email_provider=_coerce_email_provider(
            values.get("email_provider"),
            default=defaults.email_provider,
        ),
        resend_api_key="",
        from_email=_coerce_optional_email(
            values.get("from_email"),
            label="From email address",
        ),
        from_display_name=_clean_text(values.get("from_display_name")),
        reply_to=_coerce_optional_email(
            values.get("reply_to"),
            label="Reply-to address",
        ),
    )


def get_platform_email_settings(db: Session) -> PlatformEmailSettingsState:
    defaults = platform_email_settings_defaults()
    row = _platform_setting_row(db)
    if row is None:
        return defaults

    return PlatformEmailSettingsState(
        email_provider=_read_email_provider(
            getattr(row, "email_provider", None),
            default=defaults.email_provider,
        ),
        resend_api_key=str(getattr(row, "resend_api_key", None) or ""),
        from_email=_read_optional_email(
            getattr(row, "from_email", None),
            default=defaults.from_email,
        ),
        from_display_name=(
            _clean_text(getattr(row, "from_display_name", None))
            or defaults.from_display_name
        ),
        reply_to=_read_optional_email(
            getattr(row, "reply_to", None),
            default=defaults.reply_to,
        ),
    )


def save_platform_email_settings(
    db: Session,
    settings_state: PlatformEmailSettingsState,
    *,
    resend_api_key: str | None = None,
) -> PlatformEmailSettingsState:
    row = _platform_setting_row(db)
    if row is None:
        row = PlatformSetting()
        db.add(row)

    row.email_provider = settings_state.email_provider or EMAIL_PROVIDER_RESEND
    row.from_email = settings_state.from_email or None
    row.from_display_name = settings_state.from_display_name or None
    row.reply_to = settings_state.reply_to or None
    if resend_api_key is not None:
        row.resend_api_key = resend_api_key or None
    db.flush()
    return get_platform_email_settings(db)


def platform_email_settings_snapshot(
    settings_state: PlatformEmailSettingsState,
) -> dict[str, object]:
    return settings_state.snapshot()


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


def _format_from_header(settings_state: PlatformEmailSettingsState) -> str:
    if settings_state.from_display_name:
        return formataddr((settings_state.from_display_name, settings_state.from_email))
    return settings_state.from_email


def _attachment_payloads(
    attachments: list[EmailAttachment] | None,
) -> list[dict[str, str]]:
    payloads: list[dict[str, str]] = []
    for attachment in attachments or []:
        payloads.append(
            {
                "filename": attachment.filename,
                "content": base64.b64encode(attachment.content_bytes).decode("ascii"),
                "content_type": _clean_text(attachment.content_type)
                or "application/octet-stream",
            }
        )
    return payloads


def _friendly_error(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "Email request timed out."
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = int(getattr(getattr(exc, "response", None), "status_code", 0) or 0)
        if status_code in {401, 403}:
            return "Resend API authentication failed."
        if status_code == 422:
            return "Resend rejected the email payload."
        if status_code == 429:
            return "Resend rate limit exceeded."
        if status_code >= 500:
            return "Resend service is unavailable."
        if status_code >= 400:
            return "Resend rejected the email request."
    if isinstance(exc, httpx.RequestError):
        return "Email service request failed."
    return "Email send failed."


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
) -> EmailSendResult:
    transport = platform_email_settings_defaults()
    try:
        to_addresses = _parse_recipients(to)
        cc_addresses = _parse_recipients(cc)
        bcc_addresses = _parse_recipients(bcc)
        if not to_addresses:
            raise ValueError("At least one recipient email address is required.")
        _validate_recipients(to_addresses, label="To")
        _validate_recipients(cc_addresses, label="Cc")
        _validate_recipients(bcc_addresses, label="Bcc")

        if db is None:
            transport = platform_email_settings_defaults()
        else:
            transport = get_platform_email_settings(db)

        if transport.email_provider != EMAIL_PROVIDER_RESEND:
            raise ValueError("Email provider must be resend.")
        if not transport.resend_api_key:
            raise ValueError("Resend API key is not configured.")
        if not transport.from_email:
            raise ValueError("From email address is not configured.")

        payload: dict[str, object] = {
            "from": _format_from_header(transport),
            "to": to_addresses,
            "subject": _clean_text(subject) or "Weighbridge Web email",
            "text": str(text_body or "").strip() or " ",
        }
        if cc_addresses:
            payload["cc"] = cc_addresses
        if bcc_addresses:
            payload["bcc"] = bcc_addresses
        if transport.reply_to:
            payload["reply_to"] = transport.reply_to
        if html_body:
            payload["html"] = str(html_body)
        attachment_payloads = _attachment_payloads(attachments)
        if attachment_payloads:
            payload["attachments"] = attachment_payloads

        response = httpx.post(
            RESEND_SEND_EMAIL_URL,
            headers={"Authorization": f"Bearer {transport.resend_api_key}"},
            json=payload,
            timeout=DEFAULT_RESEND_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return EmailSendResult(ok=True)
    except ValueError as exc:
        return EmailSendResult(ok=False, error=str(exc))
    except httpx.HTTPStatusError as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        response_text = _clean_text(getattr(getattr(exc, "response", None), "text", ""))[:500]
        logger.warning(
            "Outbound email send rejected: provider=%s status=%s from_email=%s response=%s",
            transport.email_provider,
            status_code,
            transport.from_email,
            response_text,
        )
        return EmailSendResult(ok=False, error=_friendly_error(exc))
    except httpx.RequestError as exc:
        logger.warning(
            "Outbound email send request failed: provider=%s from_email=%s error=%s",
            transport.email_provider,
            transport.from_email,
            exc.__class__.__name__,
        )
        return EmailSendResult(ok=False, error=_friendly_error(exc))
    except Exception as exc:
        logger.exception(
            "Outbound email send failed: provider=%s from_email=%s",
            transport.email_provider,
            transport.from_email,
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
    )
