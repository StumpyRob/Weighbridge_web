from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta
from html import escape as html_escape
from pathlib import Path
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..constants import CODE_MAX, DESC_MAX
from ..db import get_db
from ..models import (
    PrintJob,
    PrintProfile,
    PrintTemplate,
    Ticket,
    TicketStatusEnum,
    Yard,
)
from ..services.print_payload import build_ticket_print_payload
from ..services.print_render import render_from_content
from ..services.printing import (
    PRINT_CONTENT_TYPE_HTML,
    PRINT_CONTENT_TYPE_TEXT,
    PRINT_JOB_STATUS_FAILED,
    PRINT_JOB_STATUS_SENT,
    execute_rendered_print,
    render_profile_content,
    resolve_profile_transport,
    retry_print_job,
)
from ..templating import templates

router = APIRouter()

PRINT_PROFILE_PURPOSE_OPTIONS = (
    ("TICKET_THERMAL", "Ticket Thermal"),
    ("RECEIPT_THERMAL", "Receipt Thermal"),
    ("INVOICE_A4", "Invoice A4"),
    ("WTN_A4", "WTN A4"),
    ("TICKET_A4", "Ticket A4 (Legacy)"),
)
PRINT_PROFILE_TRANSPORT_OPTIONS = (
    ("NETWORK_RAW_9100", "Network RAW 9100"),
    ("USB_ESC_POS", "USB ESC/POS"),
    ("CUPS", "CUPS"),
    ("LOCAL_BROWSER", "Local Browser"),
    ("LOCAL_NODE_HTTP", "Local Print Node HTTP"),
)
PRINT_TEMPLATE_CONTENT_TYPE_OPTIONS = (
    (PRINT_CONTENT_TYPE_TEXT, "Text"),
    (PRINT_CONTENT_TYPE_HTML, "HTML"),
)
PRINT_PROFILE_TEMPLATE_NAME_MAX = 255
NETWORK_PORT_MIN = 1
NETWORK_PORT_MAX = 65535
LOCAL_NODE_TIMEOUT_MIN_MS = 1
LOCAL_NODE_TIMEOUT_MAX_MS = 120000

PRINT_PROFILE_PURPOSE_VALUES = {value for value, _ in PRINT_PROFILE_PURPOSE_OPTIONS}
PRINT_PROFILE_TRANSPORT_VALUES = {
    value for value, _ in PRINT_PROFILE_TRANSPORT_OPTIONS
}
PRINT_TEMPLATE_CONTENT_TYPE_VALUES = {
    value for value, _ in PRINT_TEMPLATE_CONTENT_TYPE_OPTIONS
}
PRINT_TEMPLATE_INSERT_TOKEN_GROUPS = (
    (
        "Ticket fields",
        (
            ("Ticket number", "{{ ticket.number }}"),
            ("Ticket date/time", "{{ ticket.datetime }}"),
            ("Ticket status", "{{ ticket.status }}"),
            ("Direction", "{{ ticket.direction }}"),
            ("Transaction type", "{{ ticket.transaction_type }}"),
            ("PO number", "{{ ticket.po_number }}"),
        ),
    ),
    (
        "Customer fields",
        (
            ("Customer name", "{{ customer.name }}"),
            ("Customer account", "{{ customer.account_code }}"),
        ),
    ),
    (
        "Weight fields",
        (
            ("Gross kg", "{{ weights.gross_kg }}"),
            ("Tare kg", "{{ weights.tare_kg }}"),
            ("Net kg", "{{ weights.net_kg }}"),
            ("Gross display", "{{ weights.gross_kg_display }}"),
            ("Tare display", "{{ weights.tare_kg_display }}"),
            ("Net display", "{{ weights.net_kg_display }}"),
        ),
    ),
    (
        "Pricing fields",
        (
            ("Quantity", "{{ pricing.qty }}"),
            ("Quantity display", "{{ pricing.qty_display }}"),
            ("Unit price", "{{ pricing.unit_price }}"),
            ("Unit price display", "{{ pricing.unit_price_display }}"),
            ("Total", "{{ pricing.total }}"),
            ("Total display", "{{ pricing.total_display }}"),
        ),
    ),
)

_DEFAULT_TEMPLATE_BY_PURPOSE_AND_TYPE = {
    ("TICKET_THERMAL", PRINT_CONTENT_TYPE_TEXT): "thermal_default.txt",
    ("RECEIPT_THERMAL", PRINT_CONTENT_TYPE_TEXT): "thermal_default.txt",
    ("TICKET_A4", PRINT_CONTENT_TYPE_HTML): "a4_default.html",
    ("INVOICE_A4", PRINT_CONTENT_TYPE_HTML): "a4_default.html",
    ("WTN_A4", PRINT_CONTENT_TYPE_HTML): "a4_default.html",
}
_DEFAULT_TEMPLATE_BY_TYPE = {
    PRINT_CONTENT_TYPE_TEXT: "thermal_default.txt",
    PRINT_CONTENT_TYPE_HTML: "a4_default.html",
}
_DEFAULT_TEMPLATE_FALLBACK_BY_TYPE = {
    PRINT_CONTENT_TYPE_TEXT: "Ticket: {{ ticket.number }}",
    PRINT_CONTENT_TYPE_HTML: "<html><body><h1>Ticket {{ ticket.number }}</h1></body></html>",
}

_TRANSPORT_RAW_KEYS = {"host", "port", "timeout_seconds"}
_TRANSPORT_LOCAL_NODE_KEYS = {"url", "api_key", "timeout_ms"}


def _is_truthy(value: str | None) -> bool:
    return str(value).lower() in {"1", "true", "yes", "on"}


def _normalize_optional_int(value: str | None) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _resolve_hide_inactive(request: Request, hide_inactive: int | None) -> int:
    if hide_inactive is not None:
        return 1 if hide_inactive else 0
    return 1


def _printing_redirect_url(
    request: Request,
    *,
    base_path: str,
    extra: dict[str, str] | None = None,
    include_saved: bool = True,
) -> str:
    params: dict[str, str] = {}
    if include_saved:
        params["saved"] = "1"
    q = request.query_params.get("q")
    hide_inactive = request.query_params.get("hide_inactive")
    if q:
        params["q"] = q
    if hide_inactive is not None:
        params["hide_inactive"] = hide_inactive
    if extra:
        params.update(extra)
    return f"{base_path}?{urlencode(params)}"


def _active_printing_tab(path: str) -> str:
    if "/templates" in path:
        return "templates"
    if "/jobs" in path:
        return "jobs"
    return "profiles"


def _lookup_print_templates(db: Session) -> list[PrintTemplate]:
    return list(
        db.execute(
            select(PrintTemplate)
            .where(PrintTemplate.is_active.is_(True))
            .order_by(PrintTemplate.code.asc())
        ).scalars()
    )


def _lookup_yards(db: Session) -> list[Yard]:
    return list(
        db.execute(
            select(Yard).where(Yard.is_active.is_(True)).order_by(Yard.code.asc())
        ).scalars()
    )


def _read_builtin_print_template(filename: str, fallback: str) -> str:
    candidate = Path(__file__).resolve().parents[1] / "templates" / "print" / filename
    if not candidate.is_file():
        return fallback
    try:
        content = candidate.read_text(encoding="utf-8")
    except OSError:
        return fallback
    return content or fallback


def _default_template_content(*, purpose: str, content_type: str) -> str:
    normalized_purpose = str(purpose or "").strip().upper()
    normalized_type = str(content_type or "").strip().upper()
    filename = _DEFAULT_TEMPLATE_BY_PURPOSE_AND_TYPE.get(
        (normalized_purpose, normalized_type)
    ) or _DEFAULT_TEMPLATE_BY_TYPE.get(normalized_type)
    fallback = _DEFAULT_TEMPLATE_FALLBACK_BY_TYPE.get(
        normalized_type,
        _DEFAULT_TEMPLATE_FALLBACK_BY_TYPE[PRINT_CONTENT_TYPE_TEXT],
    )
    if not filename:
        return fallback
    return _read_builtin_print_template(filename, fallback)


def _default_ticket_layout_map() -> dict[str, str]:
    layouts: dict[str, str] = {}
    for purpose, _ in PRINT_PROFILE_PURPOSE_OPTIONS:
        for content_type, _ in PRINT_TEMPLATE_CONTENT_TYPE_OPTIONS:
            layouts[f"{purpose}|{content_type}"] = _default_template_content(
                purpose=purpose,
                content_type=content_type,
            )
    for content_type, _ in PRINT_TEMPLATE_CONTENT_TYPE_OPTIONS:
        layouts[f"*|{content_type}"] = _default_template_content(
            purpose="",
            content_type=content_type,
        )
    return layouts


def _empty_profile_form() -> dict[str, object]:
    return {
        "code": "",
        "description": "",
        "purpose": "TICKET_THERMAL",
        "template_id": "",
        "template_name": "thermal_default.txt",
        "yard_id": "",
        "transport_mode": "NETWORK_RAW_9100",
        "transport_config": '{\n  "host": "127.0.0.1",\n  "port": 9100\n}',
        "raw_host": "127.0.0.1",
        "raw_port": "9100",
        "raw_timeout_seconds": "5",
        "node_url": "http://127.0.0.1:9123/print",
        "node_api_key": "",
        "node_timeout_ms": "5000",
        "is_default": False,
        "is_active": True,
    }


def _profile_to_form(profile: PrintProfile) -> dict[str, object]:
    config = profile.transport_config if isinstance(profile.transport_config, dict) else {}
    raw_host = str(config.get("host", "")).strip()
    raw_port = str(config.get("port", "")).strip()
    raw_timeout = str(config.get("timeout_seconds", "")).strip()
    node_url = str(config.get("url", "")).strip()
    node_api_key = str(config.get("api_key", "")).strip()
    node_timeout = str(config.get("timeout_ms", "")).strip()
    return {
        "code": profile.code,
        "description": profile.description or "",
        "purpose": profile.purpose,
        "template_id": str(profile.template_id or ""),
        "template_name": profile.template_name,
        "yard_id": str(profile.yard_id or ""),
        "transport_mode": profile.transport_mode,
        "transport_config": json.dumps(config, indent=2, sort_keys=True),
        "raw_host": raw_host,
        "raw_port": raw_port,
        "raw_timeout_seconds": raw_timeout,
        "node_url": node_url,
        "node_api_key": node_api_key,
        "node_timeout_ms": node_timeout,
        "is_default": bool(profile.is_default),
        "is_active": bool(profile.is_active),
    }


def _empty_template_form() -> dict[str, object]:
    return {
        "code": "",
        "description": "",
        "purpose": "TICKET_THERMAL",
        "content_type": PRINT_CONTENT_TYPE_TEXT,
        "content": "Ticket: {{ payload.ticket_no }}",
        "is_active": True,
    }


def _template_to_form(template: PrintTemplate) -> dict[str, object]:
    return {
        "code": template.code,
        "description": template.description or "",
        "purpose": template.purpose,
        "content_type": template.content_type,
        "content": template.content,
        "is_active": bool(template.is_active),
    }


def _minimal_ticket_payload() -> dict:
    return {
        "ticket_no": "PREVIEW-SAMPLE",
        "datetime_display": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "status": "COMPLETE",
        "direction": "INWARD",
        "transaction_type": "SALE",
        "walk_in_sale": False,
        "po_number": "",
        "vehicle": {"registration": "SAMPLE"},
        "customer": {"account_code": "DEMO", "name": "Sample Customer"},
        "product": {"code": "P-SAMPLE", "description": "Sample Product", "unit_name": "kg", "unit_type": "WEIGHT"},
        "weights": {
            "gross_kg_display": "10,000.000 kg",
            "tare_kg_display": "2,000.000 kg",
            "net_kg_display": "8,000.000 kg",
            "qty_display": "8,000.000",
            "unit_price_display": "£1.00",
            "total_display": "£8,000.00",
            "unit_price": 1.0,
            "total": 8000.0,
        },
        "logistics": {
            "haulier": "",
            "driver": "",
            "container": "",
            "destination": "",
            "carrier_licence_number": "",
        },
        "compliance": {
            "ewc_code_display": "",
            "ewc_code_6": "",
            "ewc_hazardous": False,
        },
    }


def _sample_payload_for_render(
    db: Session, ticket_id: int | None = None
) -> tuple[dict, Ticket | None]:
    ticket: Ticket | None = None
    if ticket_id:
        ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        ticket = (
            db.execute(
                select(Ticket)
                .where(Ticket.status == TicketStatusEnum.COMPLETE.value)
                .order_by(Ticket.datetime.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
    if ticket is None:
        return _minimal_ticket_payload(), None
    return build_ticket_print_payload(db, ticket), ticket


def _parse_transport_config_json(raw_transport_config: str) -> tuple[dict, str | None]:
    if not raw_transport_config:
        return {}, None
    try:
        parsed = json.loads(raw_transport_config)
    except json.JSONDecodeError:
        return {}, "Transport config must be valid JSON object syntax."
    if not isinstance(parsed, dict):
        return {}, "Transport config must be valid JSON object syntax."
    return parsed, None


def _parse_network_port(value: object) -> tuple[int | None, str | None]:
    text = str(value or "").strip()
    if not text:
        return None, "Host and port are required for Network RAW 9100 transport."
    try:
        port = int(text)
    except ValueError:
        return None, "Port must be a number between 1 and 65535."
    if port < NETWORK_PORT_MIN or port > NETWORK_PORT_MAX:
        return None, "Port must be a number between 1 and 65535."
    return port, None


def _parse_timeout_seconds(value: object) -> tuple[float | None, str | None]:
    text = str(value or "").strip()
    if not text:
        return None, None
    try:
        timeout = float(text)
    except ValueError:
        return None, "Timeout must be a positive number."
    if timeout <= 0:
        return None, "Timeout must be a positive number."
    return timeout, None


def _parse_timeout_ms(value: object) -> tuple[int | None, str | None]:
    text = str(value or "").strip()
    if not text:
        return None, None
    try:
        timeout_ms = int(text)
    except ValueError:
        return None, "Timeout (ms) must be a whole number."
    if timeout_ms < LOCAL_NODE_TIMEOUT_MIN_MS or timeout_ms > LOCAL_NODE_TIMEOUT_MAX_MS:
        return (
            None,
            f"Timeout (ms) must be between {LOCAL_NODE_TIMEOUT_MIN_MS} and {LOCAL_NODE_TIMEOUT_MAX_MS}.",
        )
    return timeout_ms, None


def _is_valid_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _merge_transport_extras(config: dict, excluded_keys: set[str]) -> dict:
    return {key: value for key, value in config.items() if key not in excluded_keys}


def _normalize_profile_transport_config(
    *,
    transport_mode: str,
    parsed_transport_config: dict,
    raw_host: str,
    raw_port: str,
    raw_timeout_seconds: str,
    node_url: str,
    node_api_key: str,
    node_timeout_ms: str,
) -> tuple[dict, str | None]:
    mode = str(transport_mode or "").strip().upper()
    parsed = dict(parsed_transport_config or {})

    if mode == "NETWORK_RAW_9100":
        host = raw_host or str(parsed.get("host", "")).strip()
        if not host:
            return {}, "Host and port are required for Network RAW 9100 transport."
        port, port_error = _parse_network_port(raw_port or parsed.get("port", ""))
        if port_error:
            return {}, port_error
        timeout_seconds, timeout_error = _parse_timeout_seconds(
            raw_timeout_seconds or parsed.get("timeout_seconds", "")
        )
        if timeout_error:
            return {}, timeout_error
        normalized = {"host": host, "port": int(port or 0)}
        if timeout_seconds is not None:
            normalized["timeout_seconds"] = timeout_seconds
        normalized.update(_merge_transport_extras(parsed, _TRANSPORT_RAW_KEYS))
        return normalized, None

    if mode == "LOCAL_NODE_HTTP":
        url = node_url or str(parsed.get("url", "")).strip()
        if not url:
            return {}, "URL is required for Local Print Node HTTP transport."
        if not _is_valid_http_url(url):
            return {}, "URL must be a valid http:// or https:// address."

        timeout_ms, timeout_error = _parse_timeout_ms(
            node_timeout_ms or parsed.get("timeout_ms", "")
        )
        if timeout_error:
            return {}, timeout_error
        normalized = {"url": url}
        if timeout_ms is not None:
            normalized["timeout_ms"] = timeout_ms
        else:
            normalized["timeout_ms"] = 5000

        api_key = node_api_key or str(parsed.get("api_key", "")).strip()
        if api_key:
            normalized["api_key"] = api_key

        normalized.update(_merge_transport_extras(parsed, _TRANSPORT_LOCAL_NODE_KEYS))
        return normalized, None

    if mode == "LOCAL_BROWSER":
        return {}, None

    return parsed, None


def _parse_profile_form(
    form_data,
    db: Session,
    *,
    existing_id: int | None = None,
) -> tuple[dict[str, object], str | None]:
    raw_code = str(form_data.get("code", ""))
    raw_description = str(form_data.get("description", ""))
    raw_template_name = str(form_data.get("template_name", ""))
    raw_transport_config = str(form_data.get("transport_config", "")).strip()

    code = re.sub(r"\s+", " ", raw_code.strip()).upper()
    description = re.sub(r"\s+", " ", raw_description.strip())
    purpose = str(form_data.get("purpose", "")).strip().upper()
    template_name = raw_template_name.strip()
    template_id = _normalize_optional_int(str(form_data.get("template_id", "")))
    yard_id = _normalize_optional_int(str(form_data.get("yard_id", "")))
    transport_mode = str(form_data.get("transport_mode", "")).strip().upper()
    raw_host = str(form_data.get("raw_host", "")).strip()
    raw_port = str(form_data.get("raw_port", "")).strip()
    raw_timeout_seconds = str(form_data.get("raw_timeout_seconds", "")).strip()
    node_url = str(form_data.get("node_url", "")).strip()
    node_api_key = str(form_data.get("node_api_key", "")).strip()
    node_timeout_ms = str(form_data.get("node_timeout_ms", "")).strip()
    is_default = _is_truthy(str(form_data.get("is_default", "")))
    is_active = _is_truthy(str(form_data.get("is_active", "")))

    parsed_transport_config, transport_json_error = _parse_transport_config_json(
        raw_transport_config
    )
    normalized_transport_config: dict = dict(parsed_transport_config)
    transport_config_error: str | None = None
    if transport_json_error is None:
        (
            normalized_transport_config,
            transport_config_error,
        ) = _normalize_profile_transport_config(
            transport_mode=transport_mode,
            parsed_transport_config=parsed_transport_config,
            raw_host=raw_host,
            raw_port=raw_port,
            raw_timeout_seconds=raw_timeout_seconds,
            node_url=node_url,
            node_api_key=node_api_key,
            node_timeout_ms=node_timeout_ms,
        )

    error: str | None = None
    if not code:
        error = "Code is required."
    elif len(code) > CODE_MAX:
        error = f"Code must be {CODE_MAX} characters or fewer."
    elif description and len(description) > DESC_MAX:
        error = f"Description must be {DESC_MAX} characters or fewer."
    elif not purpose or purpose not in PRINT_PROFILE_PURPOSE_VALUES:
        error = "Purpose is invalid."
    elif template_name and len(template_name) > PRINT_PROFILE_TEMPLATE_NAME_MAX:
        error = "Template name must be 255 characters or fewer."
    elif not template_id and not template_name:
        error = "Select a template or provide a legacy template filename."
    elif template_id and db.get(PrintTemplate, template_id) is None:
        error = "Selected template was not found."
    elif yard_id and db.get(Yard, yard_id) is None:
        error = "Selected yard was not found."
    elif transport_mode not in PRINT_PROFILE_TRANSPORT_VALUES:
        error = "Transport mode is invalid."
    elif transport_json_error:
        error = transport_json_error
    elif transport_config_error:
        error = transport_config_error
    else:
        duplicate_query = select(PrintProfile).where(
            func.lower(PrintProfile.code) == code.lower()
        )
        if existing_id is not None:
            duplicate_query = duplicate_query.where(PrintProfile.id != existing_id)
        duplicate = db.execute(duplicate_query).scalar_one_or_none()
        if duplicate:
            error = "Code already exists."

    normalized = {
        "code": code,
        "description": description,
        "purpose": purpose,
        "template_id": template_id,
        "template_name": template_name,
        "yard_id": yard_id,
        "transport_mode": transport_mode,
        "transport_config": (
            raw_transport_config
            if transport_json_error
            else json.dumps(normalized_transport_config, indent=2, sort_keys=True)
        ),
        "transport_config_dict": normalized_transport_config,
        "raw_host": raw_host,
        "raw_port": raw_port,
        "raw_timeout_seconds": raw_timeout_seconds,
        "node_url": node_url,
        "node_api_key": node_api_key,
        "node_timeout_ms": node_timeout_ms,
        "is_default": is_default,
        "is_active": is_active,
    }
    return normalized, error


def _parse_template_form(
    form_data,
    db: Session,
    *,
    existing_id: int | None = None,
) -> tuple[dict[str, object], str | None]:
    raw_code = str(form_data.get("code", ""))
    raw_description = str(form_data.get("description", ""))
    raw_content = str(form_data.get("content", ""))

    code = re.sub(r"\s+", " ", raw_code.strip()).upper()
    description = re.sub(r"\s+", " ", raw_description.strip())
    purpose = str(form_data.get("purpose", "")).strip().upper()
    content_type = str(form_data.get("content_type", "")).strip().upper()
    content = raw_content
    is_active = _is_truthy(str(form_data.get("is_active", "")))

    error: str | None = None
    if not code:
        error = "Code is required."
    elif len(code) > CODE_MAX:
        error = f"Code must be {CODE_MAX} characters or fewer."
    elif description and len(description) > DESC_MAX:
        error = f"Description must be {DESC_MAX} characters or fewer."
    elif purpose not in PRINT_PROFILE_PURPOSE_VALUES:
        error = "Purpose is invalid."
    elif content_type not in PRINT_TEMPLATE_CONTENT_TYPE_VALUES:
        error = "Content type is invalid."
    elif not content.strip():
        error = "Template content is required."
    else:
        duplicate_query = select(PrintTemplate).where(
            func.lower(PrintTemplate.code) == code.lower()
        )
        if existing_id is not None:
            duplicate_query = duplicate_query.where(PrintTemplate.id != existing_id)
        duplicate = db.execute(duplicate_query).scalar_one_or_none()
        if duplicate:
            error = "Code already exists."

    normalized = {
        "code": code,
        "description": description,
        "purpose": purpose,
        "content_type": content_type,
        "content": content,
        "is_active": is_active,
    }
    return normalized, error


def _clear_other_defaults_for_scope(
    db: Session,
    *,
    purpose: str,
    yard_id: int | None,
    keep_profile_id: int | None,
) -> None:
    query = select(PrintProfile).where(
        PrintProfile.purpose == purpose,
        PrintProfile.is_default.is_(True),
        PrintProfile.id != (keep_profile_id or -1),
    )
    if yard_id is None:
        query = query.where(PrintProfile.yard_id.is_(None))
    else:
        query = query.where(PrintProfile.yard_id == yard_id)
    for row in db.execute(query).scalars():
        row.is_default = False


def _validate_template_render(
    db: Session, *, content: str, ticket_id: int | None = None
) -> str | None:
    sample_payload, _ = _sample_payload_for_render(db, ticket_id=ticket_id)
    try:
        render_from_content(sample_payload, content)
    except Exception as exc:  # noqa: BLE001
        return f"Template render failed: {exc}"
    return None


def _latest_job_id_for_profile(db: Session, profile_id: int) -> int | None:
    row = db.execute(
        select(PrintJob.id)
        .where(PrintJob.profile_id == profile_id)
        .order_by(PrintJob.id.desc())
        .limit(1)
    ).first()
    return int(row[0]) if row else None


def _print_toast_profile_name(profile: PrintProfile) -> str:
    description = str(profile.description or "").strip()
    return description or str(profile.code or "").strip() or "Printer"


def _job_target_label(job: PrintJob, profile: PrintProfile | None) -> str:
    mode = str(job.transport_mode or "").strip().upper()
    config = (
        dict(job.transport_config_json)
        if isinstance(job.transport_config_json, dict)
        else {}
    )
    if not config and profile and isinstance(profile.transport_config, dict):
        config = dict(profile.transport_config)

    if mode == "LOCAL_BROWSER":
        return "Browser"

    if mode == "NETWORK_RAW_9100":
        host = str(config.get("host", "")).strip()
        port = str(config.get("port", "")).strip()
        if host and port:
            return f"{host}:{port}"
        if host:
            return host
        if port:
            return f":{port}"
        return "Network RAW 9100"

    if mode == "LOCAL_NODE_HTTP":
        raw_url = str(config.get("url", "")).strip()
        if raw_url:
            parsed = urlparse(raw_url)
            if parsed.netloc:
                path = parsed.path if parsed.path and parsed.path != "/" else ""
                return f"{parsed.netloc}{path}"
            return raw_url
        return "Local Print Node"

    return mode or "-"


def _job_error_summary(error: str | None, max_len: int = 60) -> str:
    text = re.sub(r"\s+", " ", str(error or "").strip())
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    return f"{text[: max_len - 3].rstrip()}..."


def _ticket_preview_label(ticket: Ticket | None) -> str:
    return ticket.ticket_no if ticket and ticket.ticket_no else "Sample payload"


def _inject_preview_banner_into_html(
    *,
    rendered_html: str,
    template_id: int,
    template_code: str,
    ticket_label: str,
) -> str:
    banner_html = (
        "<div style=\"font-family: system-ui, sans-serif; font-size: 12px; "
        "padding: 8px 12px; border-bottom: 1px solid #d1d5db; background: #f3f4f6; "
        "color: #111827;\">"
        f"Previewing with Ticket: <strong>{html_escape(ticket_label)}</strong>"
        f" &middot; Template: <strong>{html_escape(template_code)}</strong>"
        f" &middot; <a href=\"/admin/printing/templates/{template_id}/edit\">Back to template</a>"
        "</div>"
    )
    body_pattern = re.compile(r"<body[^>]*>", re.IGNORECASE)
    if body_pattern.search(rendered_html):
        return body_pattern.sub(lambda match: f"{match.group(0)}{banner_html}", rendered_html, count=1)

    return (
        "<!doctype html><html><head><meta charset=\"utf-8\" />"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />"
        "<title>Template Preview</title></head><body>"
        f"{banner_html}{rendered_html}</body></html>"
    )


def _next_profile_duplicate_code(db: Session, source_code: str) -> str:
    base = f"{source_code}_COPY"
    candidate = base
    suffix = 2
    while db.execute(
        select(PrintProfile.id).where(func.lower(PrintProfile.code) == candidate.lower())
    ).first():
        candidate = f"{base}{suffix}"
        suffix += 1
    return candidate


def _next_template_duplicate_code(db: Session, source_code: str) -> str:
    raw_source = str(source_code or "").strip() or "TEMPLATE"
    copy_index = 1
    while True:
        suffix = "_COPY" if copy_index == 1 else f"_COPY{copy_index}"
        max_prefix_len = max(CODE_MAX - len(suffix), 1)
        prefix = raw_source[:max_prefix_len].rstrip()
        if not prefix:
            prefix = raw_source[:max_prefix_len]
        candidate = f"{prefix}{suffix}"
        exists = db.execute(
            select(PrintTemplate.id).where(func.lower(PrintTemplate.code) == candidate.lower())
        ).first()
        if not exists:
            return candidate
        copy_index += 1


def _next_template_duplicate_description(
    db: Session,
    *,
    source_description: str | None,
    source_code: str,
) -> str:
    raw_base = str(source_description or source_code or "Template").strip()
    base = re.sub(r"\s+", " ", raw_base) or "Template"
    copy_index = 1
    while True:
        suffix = " (Copy)" if copy_index == 1 else f" (Copy {copy_index})"
        max_base_len = max(DESC_MAX - len(suffix), 1)
        prefix = base[:max_base_len].rstrip()
        if not prefix:
            prefix = base[:max_base_len]
        candidate = f"{prefix}{suffix}"
        exists = db.execute(
            select(PrintTemplate.id).where(
                func.lower(func.coalesce(PrintTemplate.description, ""))
                == candidate.lower()
            )
        ).first()
        if not exists:
            return candidate
        copy_index += 1


def _render_profile_list(
    request: Request,
    db: Session,
    *,
    q: str | None,
    hide_inactive: int,
    error: str | None = None,
) -> HTMLResponse:
    query = select(PrintProfile)
    if hide_inactive:
        query = query.where(PrintProfile.is_active.is_(True))
    if q:
        q_lower = q.strip().lower()
        query = query.where(
            func.lower(PrintProfile.code).contains(q_lower)
            | func.lower(func.coalesce(PrintProfile.description, "")).contains(q_lower)
            | func.lower(PrintProfile.purpose).contains(q_lower)
            | func.lower(func.coalesce(PrintProfile.template_name, "")).contains(q_lower)
            | func.lower(func.coalesce(PrintProfile.transport_mode, "")).contains(q_lower)
        )
    profiles = list(
        db.execute(
            query.order_by(
                PrintProfile.purpose.asc(),
                PrintProfile.yard_id.asc().nullsfirst(),
                PrintProfile.code.asc(),
            )
        ).scalars()
    )
    templates_by_id = {
        row.id: row for row in db.execute(select(PrintTemplate)).scalars()
    }
    yards_by_id = {row.id: row for row in db.execute(select(Yard)).scalars()}
    return templates.TemplateResponse(
        request,
        "admin/printing_profiles_list.html",
        {
            "request": request,
            "active_tab": _active_printing_tab(str(request.url.path)),
            "items": profiles,
            "templates_by_id": templates_by_id,
            "yards_by_id": yards_by_id,
            "q": q or "",
            "hide_inactive": bool(hide_inactive),
            "saved": request.query_params.get("saved") == "1",
            "receipts_wip_enabled": bool(settings.receipts_wip_enabled),
            "error": error,
        },
    )


def _render_profile_form(
    request: Request,
    db: Session,
    *,
    mode: str,
    form_data: dict[str, object],
    profile: PrintProfile | None,
    error: str | None,
    status_code: int = 200,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "admin/printing_profile_form.html",
        {
            "request": request,
            "active_tab": _active_printing_tab(str(request.url.path)),
            "mode": mode,
            "form_data": form_data,
            "profile": profile,
            "error": error,
            "purpose_options": PRINT_PROFILE_PURPOSE_OPTIONS,
            "transport_options": PRINT_PROFILE_TRANSPORT_OPTIONS,
            "template_options": _lookup_print_templates(db),
            "yard_options": _lookup_yards(db),
            "saved": request.query_params.get("saved") == "1",
        },
        status_code=status_code,
    )


def _render_template_list(
    request: Request,
    db: Session,
    *,
    q: str | None,
    hide_inactive: int,
    error: str | None = None,
) -> HTMLResponse:
    query = select(PrintTemplate)
    if hide_inactive:
        query = query.where(PrintTemplate.is_active.is_(True))
    if q:
        q_lower = q.strip().lower()
        query = query.where(
            func.lower(PrintTemplate.code).contains(q_lower)
            | func.lower(func.coalesce(PrintTemplate.description, "")).contains(q_lower)
            | func.lower(PrintTemplate.purpose).contains(q_lower)
        )
    items = list(db.execute(query.order_by(PrintTemplate.code.asc())).scalars())
    return templates.TemplateResponse(
        request,
        "admin/printing_templates_list.html",
        {
            "request": request,
            "active_tab": _active_printing_tab(str(request.url.path)),
            "items": items,
            "q": q or "",
            "hide_inactive": bool(hide_inactive),
            "saved": request.query_params.get("saved") == "1",
            "error": error,
        },
    )


def _render_template_form(
    request: Request,
    db: Session,
    *,
    mode: str,
    template: PrintTemplate | None,
    form_data: dict[str, object],
    error: str | None,
    status_code: int = 200,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "admin/printing_template_form.html",
        {
            "request": request,
            "active_tab": _active_printing_tab(str(request.url.path)),
            "mode": mode,
            "template": template,
            "form_data": form_data,
            "error": error,
            "saved": request.query_params.get("saved") == "1",
            "duplicated": request.query_params.get("duplicated") == "1",
            "purpose_options": PRINT_PROFILE_PURPOSE_OPTIONS,
            "content_type_options": PRINT_TEMPLATE_CONTENT_TYPE_OPTIONS,
            "insert_token_groups": PRINT_TEMPLATE_INSERT_TOKEN_GROUPS,
            "default_ticket_layouts_json": json.dumps(
                _default_ticket_layout_map()
            ).replace("</", "<\\/"),
            "preview_ticket_id": request.query_params.get("ticket_id", ""),
        },
        status_code=status_code,
    )


@router.get("/admin/printing")
def admin_printing_index() -> RedirectResponse:
    return RedirectResponse(url="/admin/printing/profiles", status_code=303)


@router.get("/admin/printing/profiles", response_class=HTMLResponse)
def admin_print_profiles_list(
    request: Request,
    q: str | None = None,
    hide_inactive: int | None = Query(None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    resolved_hide = _resolve_hide_inactive(request, hide_inactive)
    return _render_profile_list(request, db, q=q, hide_inactive=resolved_hide)


@router.get("/admin/printing/profiles/new", response_class=HTMLResponse)
def admin_print_profiles_new(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    return _render_profile_form(
        request,
        db,
        mode="new",
        form_data=_empty_profile_form(),
        profile=None,
        error=None,
    )


@router.post("/admin/printing/profiles/new", response_class=HTMLResponse)
async def admin_print_profiles_create(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    form = await request.form()
    normalized, error = _parse_profile_form(form, db)
    if error:
        return _render_profile_form(
            request,
            db,
            mode="new",
            form_data=normalized,
            profile=None,
            error=error,
            status_code=400,
        )
    profile = PrintProfile(
        code=str(normalized["code"]),
        description=str(normalized["description"]).strip() or None,
        purpose=str(normalized["purpose"]),
        template_id=normalized["template_id"],
        template_name=str(normalized["template_name"]),
        yard_id=normalized["yard_id"],
        transport_mode=str(normalized["transport_mode"]),
        transport_config=dict(normalized["transport_config_dict"]),
        is_default=bool(normalized["is_default"]),
        is_active=bool(normalized["is_active"]),
    )
    db.add(profile)
    db.flush()
    if profile.is_default:
        _clear_other_defaults_for_scope(
            db,
            purpose=profile.purpose,
            yard_id=profile.yard_id,
            keep_profile_id=profile.id,
        )
    db.commit()
    return RedirectResponse(
        url=_printing_redirect_url(request, base_path="/admin/printing/profiles"),
        status_code=303,
    )


@router.get("/admin/printing/profiles/{profile_id:int}/edit", response_class=HTMLResponse)
def admin_print_profiles_edit(
    profile_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    profile = db.get(PrintProfile, profile_id)
    if profile is None:
        return HTMLResponse("Print profile not found.", status_code=404)
    return _render_profile_form(
        request,
        db,
        mode="edit",
        form_data=_profile_to_form(profile),
        profile=profile,
        error=None,
    )


@router.post("/admin/printing/profiles/{profile_id:int}/edit", response_class=HTMLResponse)
async def admin_print_profiles_update(
    profile_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    profile = db.get(PrintProfile, profile_id)
    if profile is None:
        return HTMLResponse("Print profile not found.", status_code=404)
    form = await request.form()
    normalized, error = _parse_profile_form(form, db, existing_id=profile.id)
    if error:
        return _render_profile_form(
            request,
            db,
            mode="edit",
            form_data=normalized,
            profile=profile,
            error=error,
            status_code=400,
        )

    profile.code = str(normalized["code"])
    profile.description = str(normalized["description"]).strip() or None
    profile.purpose = str(normalized["purpose"])
    profile.template_id = normalized["template_id"]
    profile.template_name = str(normalized["template_name"])
    profile.yard_id = normalized["yard_id"]
    profile.transport_mode = str(normalized["transport_mode"])
    profile.transport_config = dict(normalized["transport_config_dict"])
    profile.is_default = bool(normalized["is_default"])
    profile.is_active = bool(normalized["is_active"])
    if profile.is_default:
        _clear_other_defaults_for_scope(
            db,
            purpose=profile.purpose,
            yard_id=profile.yard_id,
            keep_profile_id=profile.id,
        )
    db.commit()
    return RedirectResponse(
        url=_printing_redirect_url(
            request,
            base_path=f"/admin/printing/profiles/{profile.id}/edit",
        ),
        status_code=303,
    )


@router.post("/admin/printing/profiles/{profile_id:int}/deactivate")
def admin_print_profiles_deactivate(
    profile_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    profile = db.get(PrintProfile, profile_id)
    if profile is None:
        return HTMLResponse("Print profile not found.", status_code=404)
    profile.is_active = False
    db.commit()
    return RedirectResponse(
        url=_printing_redirect_url(request, base_path="/admin/printing/profiles"),
        status_code=303,
    )


@router.post("/admin/printing/profiles/{profile_id:int}/reactivate")
def admin_print_profiles_reactivate(
    profile_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    profile = db.get(PrintProfile, profile_id)
    if profile is None:
        return HTMLResponse("Print profile not found.", status_code=404)
    profile.is_active = True
    db.commit()
    return RedirectResponse(
        url=_printing_redirect_url(request, base_path="/admin/printing/profiles"),
        status_code=303,
    )


@router.post("/admin/printing/profiles/{profile_id:int}/duplicate")
def admin_print_profiles_duplicate(
    profile_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    source = db.get(PrintProfile, profile_id)
    if source is None:
        return HTMLResponse("Print profile not found.", status_code=404)

    duplicate = PrintProfile(
        code=_next_profile_duplicate_code(db, source.code),
        description=(f"{(source.description or source.code).strip()} (Copy)"),
        purpose=source.purpose,
        template_id=source.template_id,
        template_name=source.template_name,
        yard_id=source.yard_id,
        transport_mode=source.transport_mode,
        transport_config=(
            dict(source.transport_config)
            if isinstance(source.transport_config, dict)
            else {}
        ),
        is_default=False,
        is_active=bool(source.is_active),
    )
    db.add(duplicate)
    db.commit()
    return RedirectResponse(
        url=_printing_redirect_url(
            request,
            base_path=f"/admin/printing/profiles/{duplicate.id}/edit",
            extra={"duplicated": "1"},
        ),
        status_code=303,
    )


@router.get("/admin/printing/profiles/{profile_id:int}/preview", response_class=HTMLResponse)
def admin_print_profiles_preview(
    profile_id: int,
    request: Request,
    ticket_id: str | None = Query(None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    profile = db.get(PrintProfile, profile_id)
    if profile is None:
        return HTMLResponse("Print profile not found.", status_code=404)
    resolved_ticket_id = _normalize_optional_int(ticket_id)
    payload, ticket = _sample_payload_for_render(db, ticket_id=resolved_ticket_id)
    try:
        rendered = render_profile_content(db, payload=payload, profile=profile)
    except Exception as exc:  # noqa: BLE001
        return templates.TemplateResponse(
            request,
            "admin/printing_profile_preview_text.html",
            {
                "request": request,
                "profile": profile,
                "ticket": ticket,
                "rendered": "",
                "error": f"Template render failed: {exc}",
                "template_label": profile.template_name,
            },
            status_code=400,
        )

    if rendered.content_type == PRINT_CONTENT_TYPE_HTML:
        return HTMLResponse(rendered.rendered_content)

    return templates.TemplateResponse(
        request,
        "admin/printing_profile_preview_text.html",
        {
            "request": request,
            "profile": profile,
            "ticket": ticket,
            "rendered": rendered.rendered_content,
            "error": None,
            "template_label": rendered.template_label,
        },
    )


@router.post("/admin/printing/profiles/{profile_id:int}/test-print")
def admin_print_profiles_test_print(
    profile_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    profile = db.get(PrintProfile, profile_id)
    if profile is None:
        return HTMLResponse("Print profile not found.", status_code=404)
    return_to = request.query_params.get("return_to", "").strip().lower()
    base_path = (
        "/admin/printing/profiles"
        if return_to == "list"
        else f"/admin/printing/profiles/{profile.id}/edit"
    )
    profile_label = _print_toast_profile_name(profile)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rendered_content = "\n".join(
        [
            "WEIGHBRIDGE WEB TEST PRINT",
            f"Datetime: {now}",
            "App: weighbridge_web",
            "Version: dev",
            f"Profile: {profile.code}",
            f"Purpose: {profile.purpose}",
            f"Transport: {profile.transport_mode}",
            "",
            "If this page printed, transport is configured.",
        ]
    )
    try:
        _, transport_config = resolve_profile_transport(profile)
        result = execute_rendered_print(
            db,
            purpose=profile.purpose,
            rendered_content=rendered_content,
            content_type=PRINT_CONTENT_TYPE_TEXT,
            transport_mode=profile.transport_mode,
            transport_config=transport_config,
            profile_id=profile.id,
            template_id=profile.template_id,
            ticket_id=None,
            created_by_user_id=None,
        )
    except Exception as exc:  # noqa: BLE001
        job_id = _latest_job_id_for_profile(db, profile.id)
        extra = {
            "print_failed": "1",
            "print_error": str(exc) or "Test print failed.",
            "print_profile": profile_label,
        }
        if job_id is not None:
            extra["print_job_id"] = str(job_id)
        return RedirectResponse(
            url=_printing_redirect_url(
                request,
                base_path=base_path,
                extra=extra,
                include_saved=False,
            ),
            status_code=303,
        )
    extra = {
        "print_sent": "1",
        "print_profile": profile_label,
        "print_job_id": str(result.job.id),
    }
    return RedirectResponse(
        url=_printing_redirect_url(
            request,
            base_path=base_path,
            extra=extra,
            include_saved=False,
        ),
        status_code=303,
    )


@router.get("/admin/printing/templates", response_class=HTMLResponse)
def admin_print_templates_list(
    request: Request,
    q: str | None = None,
    hide_inactive: int | None = Query(None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    resolved_hide = _resolve_hide_inactive(request, hide_inactive)
    return _render_template_list(request, db, q=q, hide_inactive=resolved_hide)


@router.get("/admin/printing/templates/new", response_class=HTMLResponse)
def admin_print_templates_new(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _render_template_form(
        request,
        db,
        mode="new",
        template=None,
        form_data=_empty_template_form(),
        error=None,
    )


@router.post("/admin/printing/templates/new", response_class=HTMLResponse)
async def admin_print_templates_create(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    form = await request.form()
    normalized, error = _parse_template_form(form, db)
    if not error:
        error = _validate_template_render(db, content=str(normalized["content"]))
    if error:
        return _render_template_form(
            request,
            db,
            mode="new",
            template=None,
            form_data=normalized,
            error=error,
            status_code=400,
        )

    template = PrintTemplate(
        code=str(normalized["code"]),
        description=str(normalized["description"]).strip() or None,
        purpose=str(normalized["purpose"]),
        content_type=str(normalized["content_type"]),
        content=str(normalized["content"]),
        is_active=bool(normalized["is_active"]),
    )
    db.add(template)
    db.commit()
    return RedirectResponse(
        url=_printing_redirect_url(request, base_path="/admin/printing/templates"),
        status_code=303,
    )


@router.get("/admin/printing/templates/{template_id:int}/edit", response_class=HTMLResponse)
def admin_print_templates_edit(
    template_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    template = db.get(PrintTemplate, template_id)
    if template is None:
        return HTMLResponse("Print template not found.", status_code=404)
    return _render_template_form(
        request,
        db,
        mode="edit",
        template=template,
        form_data=_template_to_form(template),
        error=None,
    )


@router.post("/admin/printing/templates/{template_id:int}/duplicate")
def admin_print_templates_duplicate(
    template_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    source = db.get(PrintTemplate, template_id)
    if source is None:
        return HTMLResponse("Print template not found.", status_code=404)

    duplicate = PrintTemplate(
        code=_next_template_duplicate_code(db, source.code),
        description=_next_template_duplicate_description(
            db,
            source_description=source.description,
            source_code=source.code,
        ),
        purpose=source.purpose,
        content_type=source.content_type,
        content=source.content,
        is_active=bool(source.is_active),
    )
    db.add(duplicate)
    db.commit()
    return RedirectResponse(
        url=_printing_redirect_url(
            request,
            base_path=f"/admin/printing/templates/{duplicate.id}/edit",
            extra={"duplicated": "1"},
        ),
        status_code=303,
    )


@router.get(
    "/admin/printing/templates/{template_id:int}/preview",
    response_class=HTMLResponse,
)
def admin_print_templates_preview(
    template_id: int,
    request: Request,
    ticket_id: str | None = Query(None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    template = db.get(PrintTemplate, template_id)
    if template is None:
        return HTMLResponse("Print template not found.", status_code=404)

    resolved_ticket_id = _normalize_optional_int(ticket_id)
    payload, ticket = _sample_payload_for_render(db, ticket_id=resolved_ticket_id)
    ticket_label = _ticket_preview_label(ticket)
    try:
        rendered_content = render_from_content(payload, template.content)
    except Exception as exc:  # noqa: BLE001
        return templates.TemplateResponse(
            request,
            "admin/printing_template_preview_text.html",
            {
                "request": request,
                "template": template,
                "ticket_label": ticket_label,
                "rendered": "",
                "error": f"Template render failed: {exc}",
            },
            status_code=400,
        )

    if str(template.content_type or "").strip().upper() == PRINT_CONTENT_TYPE_HTML:
        preview_html = _inject_preview_banner_into_html(
            rendered_html=rendered_content,
            template_id=template.id,
            template_code=template.code,
            ticket_label=ticket_label,
        )
        return HTMLResponse(preview_html)

    return templates.TemplateResponse(
        request,
        "admin/printing_template_preview_text.html",
        {
            "request": request,
            "template": template,
            "ticket_label": ticket_label,
            "rendered": rendered_content,
            "error": None,
        },
    )


@router.post("/admin/printing/templates/{template_id:int}/edit", response_class=HTMLResponse)
async def admin_print_templates_update(
    template_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    template = db.get(PrintTemplate, template_id)
    if template is None:
        return HTMLResponse("Print template not found.", status_code=404)

    form = await request.form()
    normalized, error = _parse_template_form(form, db, existing_id=template.id)
    if not error:
        error = _validate_template_render(db, content=str(normalized["content"]))
    if error:
        return _render_template_form(
            request,
            db,
            mode="edit",
            template=template,
            form_data=normalized,
            error=error,
            status_code=400,
        )

    new_content = str(normalized["content"])
    template.code = str(normalized["code"])
    template.description = str(normalized["description"]).strip() or None
    template.purpose = str(normalized["purpose"])
    template.content_type = str(normalized["content_type"])
    template.content = new_content
    template.is_active = bool(normalized["is_active"])
    db.commit()
    return RedirectResponse(
        url=_printing_redirect_url(
            request,
            base_path=f"/admin/printing/templates/{template.id}/edit",
        ),
        status_code=303,
    )


@router.post("/admin/printing/templates/{template_id:int}/reset-default")
def admin_print_templates_reset_default(
    template_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    template = db.get(PrintTemplate, template_id)
    if template is None:
        return HTMLResponse("Print template not found.", status_code=404)

    default_content = _default_template_content(
        purpose=template.purpose,
        content_type=template.content_type,
    )
    if template.content != default_content:
        template.content = default_content
    db.commit()
    return RedirectResponse(
        url=_printing_redirect_url(
            request,
            base_path=f"/admin/printing/templates/{template.id}/edit",
        ),
        status_code=303,
    )


@router.post("/admin/printing/templates/{template_id:int}/deactivate")
def admin_print_templates_deactivate(
    template_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    template = db.get(PrintTemplate, template_id)
    if template is None:
        return HTMLResponse("Print template not found.", status_code=404)
    template.is_active = False
    db.commit()
    return RedirectResponse(
        url=_printing_redirect_url(request, base_path="/admin/printing/templates"),
        status_code=303,
    )


@router.post("/admin/printing/templates/{template_id:int}/reactivate")
def admin_print_templates_reactivate(
    template_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    template = db.get(PrintTemplate, template_id)
    if template is None:
        return HTMLResponse("Print template not found.", status_code=404)
    template.is_active = True
    db.commit()
    return RedirectResponse(
        url=_printing_redirect_url(request, base_path="/admin/printing/templates"),
        status_code=303,
    )


@router.get("/admin/printing/jobs", response_class=HTMLResponse)
def admin_print_jobs_list(
    request: Request,
    status: str | None = Query(None),
    purpose: str | None = Query(None),
    profile_id: int | None = Query(None),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    filters = []
    if status:
        filters.append(PrintJob.status == status.strip().upper())
    if purpose:
        filters.append(PrintJob.purpose == purpose.strip().upper())
    if profile_id:
        filters.append(PrintJob.profile_id == profile_id)
    if date_from:
        filters.append(PrintJob.created_at >= datetime.combine(date_from, time.min))
    if date_to:
        end_exclusive = datetime.combine(date_to + timedelta(days=1), time.min)
        filters.append(PrintJob.created_at < end_exclusive)

    query = select(PrintJob).where(and_(*filters)) if filters else select(PrintJob)
    items = list(
        db.execute(
            query.order_by(PrintJob.created_at.desc(), PrintJob.id.desc()).limit(300)
        ).scalars()
    )
    profiles = list(
        db.execute(select(PrintProfile).order_by(PrintProfile.code.asc())).scalars()
    )
    profiles_by_id = {row.id: row for row in profiles}
    template_ids = [int(row.template_id) for row in items if row.template_id]
    templates_by_id: dict[int, PrintTemplate] = {}
    if template_ids:
        templates_by_id = {
            row.id: row
            for row in db.execute(
                select(PrintTemplate).where(PrintTemplate.id.in_(template_ids))
            ).scalars()
        }
    ticket_ids = [int(row.ticket_id) for row in items if row.ticket_id]
    tickets_by_id: dict[int, Ticket] = {}
    if ticket_ids:
        tickets_by_id = {
            row.id: row
            for row in db.execute(
                select(Ticket).where(Ticket.id.in_(ticket_ids))
            ).scalars()
        }
    job_targets = {
        row.id: _job_target_label(
            row,
            profiles_by_id.get(int(row.profile_id))
            if row.profile_id
            else None,
        )
        for row in items
    }
    job_error_summaries = {row.id: _job_error_summary(row.last_error) for row in items}
    return templates.TemplateResponse(
        request,
        "admin/printing_jobs_list.html",
        {
            "request": request,
            "active_tab": _active_printing_tab(str(request.url.path)),
            "items": items,
            "profiles": profiles,
            "profiles_by_id": profiles_by_id,
            "templates_by_id": templates_by_id,
            "tickets_by_id": tickets_by_id,
            "job_targets": job_targets,
            "job_error_summaries": job_error_summaries,
            "status_options": [PRINT_JOB_STATUS_SENT, PRINT_JOB_STATUS_FAILED, "QUEUED"],
            "purpose_options": [value for value, _ in PRINT_PROFILE_PURPOSE_OPTIONS],
            "filters": {
                "status": status or "",
                "purpose": purpose or "",
                "profile_id": str(profile_id or ""),
                "date_from": date_from.isoformat() if date_from else "",
                "date_to": date_to.isoformat() if date_to else "",
            },
            "saved": request.query_params.get("saved") == "1",
        },
    )


@router.get("/admin/printing/jobs/{job_id:int}", response_class=HTMLResponse)
def admin_print_job_detail(
    job_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    job = db.get(PrintJob, job_id)
    if job is None:
        return HTMLResponse("Print job not found.", status_code=404)
    profile = db.get(PrintProfile, job.profile_id) if job.profile_id else None
    template = db.get(PrintTemplate, job.template_id) if job.template_id else None
    ticket = db.get(Ticket, job.ticket_id) if job.ticket_id else None
    return templates.TemplateResponse(
        request,
        "admin/printing_job_detail.html",
        {
            "request": request,
            "active_tab": _active_printing_tab(str(request.url.path)),
            "job": job,
            "profile": profile,
            "template": template,
            "ticket": ticket,
            "saved": request.query_params.get("saved") == "1",
            "retry_error": request.query_params.get("retry_error", ""),
        },
    )


@router.post("/admin/printing/jobs/{job_id:int}/retry")
def admin_print_job_retry(
    job_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    job = db.get(PrintJob, job_id)
    if job is None:
        return RedirectResponse(url="/admin/printing/jobs", status_code=303)
    try:
        retry_print_job(db, job)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(
            url=_printing_redirect_url(
                request,
                base_path=f"/admin/printing/jobs/{job.id}",
                extra={"retry_error": str(exc)},
            ),
            status_code=303,
        )
    return RedirectResponse(
        url=_printing_redirect_url(
            request,
            base_path=f"/admin/printing/jobs/{job.id}",
        ),
        status_code=303,
    )
