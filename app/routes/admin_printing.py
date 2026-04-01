from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ..audit import diff as audit_diff
from ..audit import log as audit_log
from ..auth import user_display_name
from ..constants import CODE_MAX, DESC_MAX
from ..db import get_db
from ..models import (
    Invoice,
    PrintAgent,
    PrintAgentPairing,
    PrintDestination,
    PrintJob,
    PrintTemplate,
    Ticket,
    User,
)
from ..permissions import PERM_MANAGE_SETTINGS, require_permission
from ..services.print_payload import (
    build_print_payload,
)
from ..services.print_render import render_from_content
from ..services.printing import (
    DELIVERY_TYPE_EMAIL_PDF,
    DELIVERY_TYPE_PRINT_LOCAL_BROWSER,
    DELIVERY_TYPE_PRINT_NETWORK_RAW_9100,
    DELIVERY_TYPE_PRINT_NODE_HTTP,
    DELIVERY_TYPE_PRINT_AGENT_PULL,
    DOCUMENT_TYPE_INVOICE,
    DOCUMENT_TYPE_TICKET,
    DOCUMENT_TYPE_WTN,
    PRINT_CONTENT_TYPE_HTML,
    PRINT_CONTENT_TYPE_TEXT,
    PRINT_JOB_TRIGGER_SOURCE_AUTO_ON_COMPLETE,
    PRINT_JOB_TRIGGER_SOURCE_MANUAL,
    PRINT_JOB_STATUS_FAILED,
    PRINT_JOB_STATUS_IN_PROGRESS,
    PRINT_JOB_STATUS_PENDING,
    PRINT_JOB_STATUS_QUEUED,
    PRINT_JOB_STATUS_SENT,
    retry_print_job,
)
from ..services.print_agents import (
    PrintAgentPairingError,
    complete_print_agent_pairing as complete_print_agent_pairing_session,
    is_print_agent_pairing_expired,
    normalize_print_agent_printers,
    revoke_print_agent,
    PRINT_AGENT_STATUS_REVOKED,
)
from ..templating import templates
from ..tenancy import request_tenant_id


def _require_admin_user(request: Request) -> None:
    require_permission(request, PERM_MANAGE_SETTINGS)


router = APIRouter(dependencies=[Depends(_require_admin_user)])

DOCUMENT_TYPE_OPTIONS = (
    (DOCUMENT_TYPE_TICKET, "Ticket"),
    (DOCUMENT_TYPE_INVOICE, "Invoice"),
    (DOCUMENT_TYPE_WTN, "WTN"),
)
DOCUMENT_TYPE_VALUES = {value for value, _ in DOCUMENT_TYPE_OPTIONS}
DELIVERY_TYPE_OPTIONS = (
    (DELIVERY_TYPE_PRINT_LOCAL_BROWSER, "Print: Local Browser"),
    (DELIVERY_TYPE_PRINT_NETWORK_RAW_9100, "Print: Network RAW 9100"),
    (DELIVERY_TYPE_PRINT_NODE_HTTP, "Print: Site Agent HTTP"),
    (DELIVERY_TYPE_PRINT_AGENT_PULL, "Print: Agent Pull"),
    (DELIVERY_TYPE_EMAIL_PDF, "Email: PDF"),
)
DELIVERY_TYPE_VALUES = {value for value, _ in DELIVERY_TYPE_OPTIONS}
TEMPLATE_FORMAT_OPTIONS = (
    (PRINT_CONTENT_TYPE_TEXT, "Text"),
    (PRINT_CONTENT_TYPE_HTML, "HTML"),
)
TEMPLATE_FORMAT_VALUES = {value for value, _ in TEMPLATE_FORMAT_OPTIONS}
SYSTEM_TEMPLATE_LOCK_ERROR = (
    "System templates cannot be edited. Duplicate to customise."
)
DESTINATION_DELETE_DEFAULT_ACTIVE_ERROR = (
    "Default active destinations cannot be deleted."
)
DESTINATION_DELETE_IN_USE_ERROR = (
    "Destination is in use by one or more jobs and cannot be deleted."
)
_MANAGED_DELIVERY_CONFIG_KEYS = {
    "agent_id",
    "api_key",
    "attach_pdf",
    "auto_print_on_complete",
    "bcc",
    "body_template",
    "cc",
    "copies",
    "email_body_template",
    "email_subject_template",
    "host",
    "port",
    "printer_name",
    "subject_template",
    "timeout_ms",
    "timeout_seconds",
    "to",
    "url",
}
JOB_STATUS_FILTER_OPTIONS = (
    (PRINT_JOB_STATUS_SENT, "Sent"),
    (PRINT_JOB_STATUS_FAILED, "Failed"),
    (PRINT_JOB_STATUS_PENDING, "Pending"),
    (PRINT_JOB_STATUS_IN_PROGRESS, "In Progress"),
    (PRINT_JOB_STATUS_QUEUED, "Queued"),
)


def _active_printing_tab(path: str) -> str:
    if "/agents" in path:
        return "agents"
    if "/templates" in path:
        return "templates"
    if "/jobs" in path:
        return "jobs"
    return "destinations"


def _is_truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _resolve_hide_inactive(raw: int | None) -> int:
    if raw is None:
        return 1
    return 1 if raw else 0


def _parse_optional_int(value: str | None) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _resolve_show_flag(raw: object, *, default: int = 0) -> int:
    if raw is None:
        return 1 if default else 0
    return 1 if _is_truthy(str(raw)) else 0


def _format_admin_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.strftime("%d/%m/%Y %H:%M:%S")


def _print_job_trigger_source_label(value: str | None) -> str:
    normalized = str(value or "").strip().upper()
    if normalized == PRINT_JOB_TRIGGER_SOURCE_AUTO_ON_COMPLETE:
        return "Auto on complete"
    if normalized == PRINT_JOB_TRIGGER_SOURCE_MANUAL:
        return "Manual"
    return normalized or "Manual"


def _print_job_printer_or_target(job: PrintJob) -> str:
    config = (
        dict(job.delivery_config_json)
        if isinstance(job.delivery_config_json, dict)
        else {}
    )
    printer_name = str(config.get("printer_name", "")).strip()
    if printer_name:
        return printer_name
    host = str(config.get("host", "")).strip()
    if host:
        port = _parse_optional_int(str(config.get("port", "")).strip())
        return f"{host}:{port}" if port is not None else host
    if str(job.delivery_type or "").strip().upper() == DELIVERY_TYPE_PRINT_LOCAL_BROWSER:
        return "Browser dialog"
    return "-"


def _print_job_can_retry(job: PrintJob) -> bool:
    return str(job.status or "").strip().upper() == PRINT_JOB_STATUS_FAILED


def _print_job_retry_success_message(job: PrintJob) -> str:
    status = str(job.status or "").strip().upper()
    if status == PRINT_JOB_STATUS_PENDING:
        return f"Retry queued for print job #{job.id}."
    if status == PRINT_JOB_STATUS_SENT:
        return f"Retry succeeded for print job #{job.id}."
    return f"Retry updated print job #{job.id} to {status or 'UNKNOWN'}."


def _print_job_retry_redirect_url(
    *,
    job_id: int,
    return_to: str,
    success_message: str = "",
    error_message: str = "",
) -> str:
    normalized_return_to = str(return_to or "").strip().lower()
    if normalized_return_to == "list":
        base_path = "/admin/printing/jobs"
    else:
        base_path = f"/admin/printing/jobs/{job_id}"
    params: dict[str, str] = {}
    if success_message:
        params["retry_success_message"] = success_message
    if error_message:
        params["retry_error_message"] = error_message
    if params:
        return f"{base_path}?{urlencode(params)}"
    return base_path


def _normalize_document_type(value: str | None) -> str:
    normalized = str(value or "").strip().upper()
    if normalized in DOCUMENT_TYPE_VALUES:
        return normalized
    return DOCUMENT_TYPE_TICKET


def _normalize_format(value: str | None) -> str:
    normalized = str(value or "").strip().upper()
    if normalized in TEMPLATE_FORMAT_VALUES:
        return normalized
    return PRINT_CONTENT_TYPE_TEXT


def _normalize_delivery_type(value: str | None) -> str:
    normalized = str(value or "").strip().upper()
    if normalized in DELIVERY_TYPE_VALUES:
        return normalized
    return DELIVERY_TYPE_PRINT_LOCAL_BROWSER


def _current_user_id(request: Request) -> int | None:
    current_user = getattr(getattr(request, "state", None), "current_user", None)
    user_id = getattr(current_user, "id", None)
    try:
        return int(user_id) if user_id is not None else None
    except (TypeError, ValueError):
        return None


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
    for key in ("q", "hide_inactive", "document_type"):
        value = request.query_params.get(key)
        if value:
            params[key] = value
    if extra:
        params.update(extra)
    if not params:
        return base_path
    return f"{base_path}?{urlencode(params)}"


def _template_order_key(row: PrintTemplate) -> tuple[str, str]:
    return (
        str(row.document_type or ""),
        str(row.code or ""),
    )


def _template_list_order_key(row: PrintTemplate) -> tuple[int, str, str, int]:
    return (
        1 if bool(row.is_system) else 0,
        str(row.document_type or ""),
        str(row.code or ""),
        int(row.id or 0),
    )


def _destination_has_jobs(db: Session, destination_id: int) -> bool:
    return (
        db.execute(
            select(PrintJob.id)
            .where(PrintJob.destination_id == destination_id)
            .limit(1)
        ).first()
        is not None
    )


def _print_destination_snapshot(
    destination: PrintDestination | None,
    *,
    template: PrintTemplate | None = None,
) -> dict[str, object]:
    if destination is None:
        return {}
    resolved_template = template
    return {
        "name": str(destination.name or "").strip() or None,
        "description": str(destination.description or "").strip() or None,
        "document_type": str(destination.document_type or "").strip() or None,
        "template_id": destination.template_id,
        "template_code": (
            str(resolved_template.code or "").strip() or None
            if resolved_template is not None
            else None
        ),
        "delivery_type": str(destination.delivery_type or "").strip() or None,
        "auto_print_on_complete": bool(
            isinstance(destination.delivery_config, dict)
            and destination.delivery_config.get("auto_print_on_complete")
        ),
        "is_default": bool(destination.is_default),
        "is_active": bool(destination.is_active),
    }


def _print_template_snapshot(template: PrintTemplate | None) -> dict[str, object]:
    if template is None:
        return {}
    return {
        "code": str(template.code or "").strip() or None,
        "description": str(template.description or "").strip() or None,
        "document_type": str(template.document_type or "").strip() or None,
        "format": str(template.format or "").strip() or None,
        "is_system": bool(template.is_system),
        "is_active": bool(template.is_active),
    }


def _destination_delete_block_error(
    destination: PrintDestination,
    *,
    has_jobs: bool,
) -> str | None:
    if bool(destination.is_default) and bool(destination.is_active):
        return DESTINATION_DELETE_DEFAULT_ACTIVE_ERROR
    if has_jobs:
        return DESTINATION_DELETE_IN_USE_ERROR
    return None


def _destination_to_form(
    destination: PrintDestination | None = None,
) -> dict[str, str | bool]:
    config = (
        dict(destination.delivery_config)
        if destination and isinstance(destination.delivery_config, dict)
        else {}
    )
    return {
        "name": str(destination.name if destination else ""),
        "description": str(
            destination.description if destination and destination.description else ""
        ),
        "document_type": str(
            destination.document_type if destination else DOCUMENT_TYPE_TICKET
        ),
        "template_id": str(
            destination.template_id if destination and destination.template_id else ""
        ),
        "delivery_type": str(
            destination.delivery_type if destination else DELIVERY_TYPE_PRINT_LOCAL_BROWSER
        ),
        "delivery_config": json.dumps(config, indent=2, sort_keys=True),
        "raw_host": str(config.get("host", "")),
        "raw_port": str(config.get("port", 9100)),
        "raw_timeout_seconds": str(config.get("timeout_seconds", 5)),
        "node_url": str(config.get("url", "")),
        "node_api_key": str(config.get("api_key", "")),
        "node_timeout_ms": str(config.get("timeout_ms", 5000)),
        "node_printer_name": str(config.get("printer_name", "")),
        "node_copies": str(config.get("copies", 1)),
        "pull_agent_id": str(config.get("agent_id", "")),
        "pull_printer_name": str(config.get("printer_name", "")),
        "pull_printer_name_manual": str(config.get("printer_name", "")),
        "pull_copies": str(config.get("copies", 1)),
        "email_to": str(config.get("to", "")),
        "email_cc": str(config.get("cc", "")),
        "email_bcc": str(config.get("bcc", "")),
        "email_subject_template": str(
            config.get("email_subject_template", config.get("subject_template", ""))
        ),
        "email_body_template": str(
            config.get("email_body_template", config.get("body_template", ""))
        ),
        "attach_pdf": bool(config.get("attach_pdf", True)),
        "auto_print_on_complete": bool(config.get("auto_print_on_complete", False)),
        "is_default": bool(destination.is_default) if destination else False,
        "is_active": bool(destination.is_active) if destination else True,
    }


def _resolve_pull_printer_form_value(form: dict[str, object]) -> str:
    printer_name = str(form.get("pull_printer_name", "")).strip()
    if printer_name:
        return printer_name
    return str(form.get("pull_printer_name_manual", "")).strip()


def _sync_pull_printer_form_fields(form_data: dict[str, str | bool]) -> None:
    resolved_printer_name = _resolve_pull_printer_form_value(form_data)
    manual_value = str(form_data.get("pull_printer_name_manual", "")).strip()
    form_data["pull_printer_name"] = resolved_printer_name
    form_data["pull_printer_name_manual"] = manual_value or resolved_printer_name


def _normalized_printer_name_key(value: str | None) -> str:
    return str(value or "").strip().casefold()


def _print_agent_has_synced_printer(agent: PrintAgent | None, printer_name: str | None) -> bool:
    normalized_printer_name = _normalized_printer_name_key(printer_name)
    if not normalized_printer_name:
        return False
    return any(
        _normalized_printer_name_key(str(item.get("name", ""))) == normalized_printer_name
        for item in normalize_print_agent_printers(
            getattr(agent, "printers_json", None) if agent is not None else None
        )
    )


def _print_agent_printer_options(
    agent: PrintAgent | None,
) -> list[dict[str, object]]:
    printers = normalize_print_agent_printers(
        getattr(agent, "printers_json", None) if agent is not None else None
    )
    options: list[dict[str, object]] = []
    for printer in printers:
        name = str(printer.get("name", "")).strip()
        if not name:
            continue
        status_bits = []
        if bool(printer.get("is_default")):
            status_bits.append("default")
        if bool(printer.get("is_online")):
            status_bits.append("online")
        label = name if not status_bits else f"{name} ({', '.join(status_bits)})"
        options.append(
            {
                "name": name,
                "label": label,
                "is_default": bool(printer.get("is_default")),
                "is_online": bool(printer.get("is_online")),
            }
        )
    return options


def _print_agent_printer_inventory(
    print_agents: list[PrintAgent],
    *,
    selected_agent_id: str = "",
    selected_printer_name: str = "",
) -> dict[str, dict[str, object]]:
    inventory: dict[str, dict[str, object]] = {}
    for agent in print_agents:
        synced_printers = normalize_print_agent_printers(agent.printers_json)
        selected_printer_missing = (
            str(agent.id) == str(selected_agent_id)
            and bool(synced_printers)
            and not _print_agent_has_synced_printer(agent, selected_printer_name)
            and bool(str(selected_printer_name or "").strip())
        )
        inventory[str(agent.id)] = {
            "printer_count": len(synced_printers),
            "printers": _print_agent_printer_options(agent),
            "has_synced_printers": bool(synced_printers),
            "selected_printer_missing": selected_printer_missing,
            "synced_at_label": _format_admin_datetime(agent.printers_synced_at),
        }
    return inventory


def _parsed_delivery_config_json(form: dict[str, str]) -> dict[str, object]:
    raw_json = str(form.get("delivery_config", "")).strip()
    if not raw_json:
        return {}
    parsed = json.loads(raw_json)
    if isinstance(parsed, dict):
        return dict(parsed)
    return {}


def _template_to_form(template: PrintTemplate | None = None) -> dict[str, str | bool]:
    return {
        "code": str(template.code if template and template.code else ""),
        "description": str(template.description if template and template.description else ""),
        "document_type": str(
            template.document_type if template else DOCUMENT_TYPE_TICKET
        ),
        "format": str(template.format if template else PRINT_CONTENT_TYPE_TEXT),
        "content": str(template.content if template else ""),
        "is_active": bool(template.is_active) if template else True,
    }


def _delivery_config_from_form(
    form: dict[str, str],
    delivery_type: str,
    *,
    document_type: str,
) -> dict:
    normalized = _normalize_delivery_type(delivery_type)
    config = _parsed_delivery_config_json(form)
    for key in _MANAGED_DELIVERY_CONFIG_KEYS:
        config.pop(key, None)
    if normalized == DELIVERY_TYPE_PRINT_NETWORK_RAW_9100:
        host = str(form.get("raw_host", "")).strip()
        if host:
            config["host"] = host
        port = _parse_optional_int(form.get("raw_port"))
        if port is not None:
            config["port"] = port
        timeout_raw = str(form.get("raw_timeout_seconds", "")).strip()
        if timeout_raw:
            config["timeout_seconds"] = float(timeout_raw)
    elif normalized == DELIVERY_TYPE_PRINT_NODE_HTTP:
        url = str(form.get("node_url", "")).strip()
        if url:
            config["url"] = url
        api_key = str(form.get("node_api_key", "")).strip()
        if api_key:
            config["api_key"] = api_key
        timeout_ms = _parse_optional_int(form.get("node_timeout_ms"))
        if timeout_ms is not None:
            config["timeout_ms"] = timeout_ms
        printer_name = str(form.get("node_printer_name", "")).strip()
        if printer_name:
            config["printer_name"] = printer_name
        copies = _parse_optional_int(form.get("node_copies"))
        if copies is not None:
            config["copies"] = copies
    elif normalized == DELIVERY_TYPE_PRINT_AGENT_PULL:
        agent_id = str(form.get("pull_agent_id", "")).strip()
        if agent_id:
            config["agent_id"] = agent_id
        printer_name = _resolve_pull_printer_form_value(form)
        if printer_name:
            config["printer_name"] = printer_name
        copies = _parse_optional_int(form.get("pull_copies"))
        if copies is not None:
            config["copies"] = copies
    elif normalized == DELIVERY_TYPE_EMAIL_PDF:
        to_value = str(form.get("email_to", "")).strip()
        if to_value:
            config["to"] = to_value
        cc_value = str(form.get("email_cc", "")).strip()
        if cc_value:
            config["cc"] = cc_value
        bcc_value = str(form.get("email_bcc", "")).strip()
        if bcc_value:
            config["bcc"] = bcc_value
        subject_template = str(form.get("email_subject_template", "")).strip()
        if subject_template:
            config["subject_template"] = subject_template
        body_template = str(form.get("email_body_template", "")).strip()
        if body_template:
            config["body_template"] = body_template
        config["attach_pdf"] = _is_truthy(form.get("attach_pdf"))

    if document_type == DOCUMENT_TYPE_TICKET and _is_truthy(
        str(form.get("auto_print_on_complete", ""))
    ):
        config["auto_print_on_complete"] = True
    else:
        config.pop("auto_print_on_complete", None)
    return config


def _validate_print_destination(
    db: Session,
    *,
    template: PrintTemplate | None,
    document_type: str,
    delivery_type: str,
    delivery_config: dict,
    errors: list[str],
) -> None:
    if template is None:
        return

    if delivery_type == DELIVERY_TYPE_EMAIL_PDF and document_type != DOCUMENT_TYPE_INVOICE:
        errors.append("EMAIL_PDF destinations are only supported for Invoice.")

    if delivery_type == DELIVERY_TYPE_PRINT_AGENT_PULL:
        if _normalize_format(getattr(template, "format", None)) != PRINT_CONTENT_TYPE_TEXT:
            errors.append("PRINT_AGENT_PULL destinations currently support TEXT templates only.")
        agent_id = str(delivery_config.get("agent_id", "")).strip()
        agent = db.get(PrintAgent, agent_id) if agent_id else None
        if not agent_id:
            errors.append("PRINT_AGENT_PULL destination requires an assigned agent.")
        elif agent is None:
            errors.append("Assigned print agent was not found.")
        printer_name = str(delivery_config.get("printer_name", "")).strip()
        if not printer_name:
            errors.append("PRINT_AGENT_PULL destination requires a printer name.")
        elif agent is not None:
            synced_printers = normalize_print_agent_printers(agent.printers_json)
            if synced_printers and not _print_agent_has_synced_printer(agent, printer_name):
                errors.append(
                    "Selected print agent printer is no longer synced. Choose a current printer and save again."
                )
        copies_raw = delivery_config.get("copies")
        try:
            copies = int(copies_raw)
        except (TypeError, ValueError):
            copies = None
        if copies is None or copies < 1:
            errors.append("PRINT_AGENT_PULL destination requires copies >= 1.")
        return

    if delivery_type != DELIVERY_TYPE_PRINT_NODE_HTTP:
        return

    if _normalize_format(getattr(template, "format", None)) != PRINT_CONTENT_TYPE_TEXT:
        errors.append("PRINT_NODE_HTTP destinations currently support TEXT templates only.")

    url = str(delivery_config.get("url", "")).strip()
    if not url:
        errors.append("PRINT_NODE_HTTP destination requires a URL.")

    printer_name = str(delivery_config.get("printer_name", "")).strip()
    if not printer_name:
        errors.append("PRINT_NODE_HTTP destination requires a printer name.")

    copies_raw = delivery_config.get("copies")
    try:
        copies = int(copies_raw)
    except (TypeError, ValueError):
        copies = None
    if copies is None or copies < 1:
        errors.append("PRINT_NODE_HTTP destination requires copies >= 1.")


def _set_unique_default_destination(
    db: Session,
    *,
    document_type: str,
    destination_id: int | None,
) -> None:
    rows = db.execute(
        select(PrintDestination).where(
            PrintDestination.document_type == document_type,
            PrintDestination.is_default.is_(True),
            PrintDestination.is_active.is_(True),
        )
    ).scalars()
    for row in rows:
        if destination_id is not None and int(row.id) == int(destination_id):
            continue
        row.is_default = False


@router.get("/admin/printing")
def admin_printing_root() -> RedirectResponse:
    return RedirectResponse(url="/admin/printing/destinations", status_code=303)


@router.get("/admin/printing/agents", response_class=HTMLResponse)
def admin_print_agents(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _print_agents_page_response(request, db=db)


@router.post("/admin/printing/agents/pair", response_class=HTMLResponse)
async def admin_print_agents_pair(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    form = await request.form()
    pairing_code = str(form.get("pairing_code", "")).strip()
    agent_name = str(form.get("agent_name", "")).strip()
    form_data = {
        "pairing_code": pairing_code,
        "agent_name": agent_name,
    }
    if not pairing_code:
        return _print_agents_page_response(
            request,
            db=db,
            form_data=form_data,
            error="Pairing code is required.",
            status_code=400,
        )

    try:
        pairing, agent = complete_print_agent_pairing_session(
            db,
            pairing_code=pairing_code,
            paired_by_user_id=_current_user_id(request),
            agent_name=agent_name,
        )
    except PrintAgentPairingError as exc:
        return _print_agents_page_response(
            request,
            db=db,
            form_data=form_data,
            error=exc.message,
            status_code=400 if exc.status_code == 404 else exc.status_code,
        )

    audit_log(
        db,
        request,
        action="PAIR",
        entity_type="print_agent",
        entity_id=agent.id,
        summary=f"Paired print agent {agent.name or agent.id}",
        details={
            "pairing_id": pairing.id,
            "requested_name": pairing.requested_name,
            "paired_name": pairing.paired_name,
            "status": pairing.status,
        },
    )
    db.commit()
    return RedirectResponse(
        url=f"/admin/printing/agents?paired=1&agent_id={agent.id}",
        status_code=303,
    )


@router.post("/admin/printing/agents/pairings/{pairing_id}/cancel")
def admin_print_agents_cancel_pairing(
    pairing_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    tenant_id = request_tenant_id(request)
    pairing = db.get(PrintAgentPairing, str(pairing_id or "").strip())
    if pairing is None or int(getattr(pairing, "tenant_id", 0) or 0) != tenant_id:
        return _print_agents_redirect(request, error="Print agent pairing was not found.")
    if not _pairing_can_cancel(pairing) or pairing.print_agent_id:
        return _print_agents_redirect(
            request,
            error="Only pending pairing sessions can be canceled."
        )

    pairing_label = pairing.requested_name or pairing.id
    audit_log(
        db,
        request,
        action="DELETE",
        entity_type="print_agent_pairing",
        entity_id=pairing.id,
        summary=f"Canceled print agent pairing {pairing_label}",
        details={
            "requested_name": pairing.requested_name,
            "status": pairing.status,
            "expires_at": pairing.expires_at.isoformat() if pairing.expires_at else None,
        },
    )
    db.delete(pairing)
    db.commit()
    return _print_agents_redirect(
        request,
        success=f"Canceled print agent pairing {pairing_label}.",
    )


@router.post("/admin/printing/agents/pairings/cancel-expired")
def admin_print_agents_cancel_expired_pairings(
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    tenant_id = request_tenant_id(request)
    pairings = list(
        db.execute(
            select(PrintAgentPairing).where(
                PrintAgentPairing.tenant_id == tenant_id,
                PrintAgentPairing.status == "PENDING",
            )
        ).scalars()
    )
    expired_pairings = [row for row in pairings if is_print_agent_pairing_expired(row)]
    if not expired_pairings:
        return _print_agents_redirect(
            request,
            success="No expired pending print agent pairings to cancel.",
        )

    count = len(expired_pairings)
    sample_ids = [row.id for row in expired_pairings[:10]]
    for pairing in expired_pairings:
        db.delete(pairing)
    audit_log(
        db,
        request,
        action="DELETE",
        entity_type="print_agent_pairing",
        entity_id=None,
        summary=f"Canceled {count} expired print agent pairing(s)",
        details={
            "count": count,
            "pairing_ids": sample_ids,
            "tenant_id": tenant_id,
        },
    )
    db.commit()
    return _print_agents_redirect(
        request,
        success=f"Canceled {count} expired pending print agent pairing(s).",
    )


@router.post("/admin/printing/agents/{agent_id}/revoke")
def admin_print_agents_revoke(
    agent_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    tenant_id = request_tenant_id(request)
    agent = db.get(PrintAgent, str(agent_id or "").strip())
    if agent is None or int(getattr(agent, "tenant_id", 0) or 0) != tenant_id:
        return _print_agents_redirect(request, error="Print agent was not found.")
    if str(agent.status or "").strip().upper() == PRINT_AGENT_STATUS_REVOKED:
        return _print_agents_redirect(
            request,
            success=f"Print agent {agent.name or agent.id} is already revoked."
        )

    blocking_destinations = _pull_destinations_for_agent(
        db,
        tenant_id=tenant_id,
        agent_id=agent.id,
    )
    if blocking_destinations:
        destination_names = ", ".join(row.name for row in blocking_destinations[:3])
        return _print_agents_redirect(
            request,
            error=(
                f"Cannot revoke print agent {agent.name or agent.id} while assigned to "
                f"PRINT_AGENT_PULL destination(s): {destination_names}."
            )
        )

    previous_status = str(agent.status or "").strip().upper() or None
    revoke_print_agent(agent)
    audit_log(
        db,
        request,
        action="REVOKE",
        entity_type="print_agent",
        entity_id=agent.id,
        summary=f"Revoked print agent {agent.name or agent.id}",
        details={
            "previous_status": previous_status,
            "new_status": agent.status,
        },
    )
    db.commit()
    return _print_agents_redirect(
        request,
        success=f"Revoked print agent {agent.name or agent.id}.",
    )


@router.get("/admin/printing/destinations", response_class=HTMLResponse)
def admin_print_destinations_list(
    request: Request,
    q: str | None = Query(None),
    document_type: str | None = Query(None),
    hide_inactive: int | None = Query(None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    resolved_hide = _resolve_hide_inactive(hide_inactive)
    normalized_document_type = _normalize_document_type(document_type) if document_type else ""

    filters = []
    if q:
        search = f"%{q.strip()}%"
        filters.append(
            or_(
                PrintDestination.name.ilike(search),
                PrintDestination.description.ilike(search),
            )
        )
    if normalized_document_type:
        filters.append(PrintDestination.document_type == normalized_document_type)
    if resolved_hide:
        filters.append(PrintDestination.is_active.is_(True))

    query = select(PrintDestination).where(and_(*filters)) if filters else select(PrintDestination)
    items = list(
        db.execute(
            query.order_by(PrintDestination.document_type.asc(), PrintDestination.name.asc())
        ).scalars()
    )

    template_ids = [int(item.template_id) for item in items if item.template_id]
    templates_by_id: dict[int, PrintTemplate] = {}
    if template_ids:
        templates_by_id = {
            row.id: row
            for row in db.execute(
                select(PrintTemplate).where(PrintTemplate.id.in_(template_ids))
            ).scalars()
        }

    destination_delete_errors: dict[int, str] = {}
    if items:
        destination_ids = [int(item.id) for item in items]
        job_rows = db.execute(
            select(PrintJob.destination_id, func.count(PrintJob.id))
            .where(PrintJob.destination_id.in_(destination_ids))
            .group_by(PrintJob.destination_id)
        ).all()
        job_counts_by_destination = {
            int(destination_id): int(count)
            for destination_id, count in job_rows
            if destination_id is not None
        }
        for item in items:
            has_jobs = job_counts_by_destination.get(int(item.id), 0) > 0
            block_error = _destination_delete_block_error(item, has_jobs=has_jobs)
            destination_delete_errors[int(item.id)] = block_error or ""

    return templates.TemplateResponse(
        request,
        "admin/printing_destinations_list.html",
        {
            "request": request,
            "active_tab": _active_printing_tab(str(request.url.path)),
            "items": items,
            "templates_by_id": templates_by_id,
            "q": q or "",
            "document_type": normalized_document_type,
            "document_type_options": DOCUMENT_TYPE_OPTIONS,
            "hide_inactive": bool(resolved_hide),
            "error": request.query_params.get("error", ""),
            "saved": request.query_params.get("saved") == "1",
            "destination_delete_errors": destination_delete_errors,
        },
    )


def _destination_form_response(
    request: Request,
    *,
    mode: str,
    destination: PrintDestination | None,
    form_data: dict[str, str | bool],
    error: str,
    status_code: int = 200,
    db: Session,
) -> HTMLResponse:
    tenant_id = request_tenant_id(request)
    _sync_pull_printer_form_fields(form_data)
    template_options = sorted(
        list(db.execute(select(PrintTemplate)).scalars()),
        key=_template_order_key,
    )
    can_delete_destination = False
    destination_delete_error = ""
    print_agents = list(
        db.execute(
            select(PrintAgent)
            .where(
                PrintAgent.tenant_id == tenant_id,
                PrintAgent.status != PRINT_AGENT_STATUS_REVOKED,
            )
            .order_by(
                func.coalesce(PrintAgent.name, "").asc(),
                PrintAgent.id.asc(),
            )
        ).scalars()
    )
    if destination is not None:
        has_jobs = _destination_has_jobs(db, int(destination.id))
        destination_delete_error = (
            _destination_delete_block_error(destination, has_jobs=has_jobs) or ""
        )
        can_delete_destination = not bool(destination_delete_error)
    selected_pull_agent_id = str(form_data.get("pull_agent_id", "")).strip()
    selected_pull_printer_name = str(form_data.get("pull_printer_name", "")).strip()
    pull_agent_printer_inventory = _print_agent_printer_inventory(
        print_agents,
        selected_agent_id=selected_pull_agent_id,
        selected_printer_name=selected_pull_printer_name,
    )
    selected_pull_agent_inventory = pull_agent_printer_inventory.get(
        selected_pull_agent_id,
        {
            "printer_count": 0,
            "printers": [],
            "has_synced_printers": False,
            "selected_printer_missing": False,
            "synced_at_label": "",
        },
    )
    return templates.TemplateResponse(
        request,
        "admin/printing_destination_form.html",
        {
            "request": request,
            "active_tab": _active_printing_tab(str(request.url.path)),
            "mode": mode,
            "destination": destination,
            "form_data": form_data,
            "error": error,
            "document_type_options": DOCUMENT_TYPE_OPTIONS,
            "delivery_type_options": DELIVERY_TYPE_OPTIONS,
            "print_agents": print_agents,
            "template_options": template_options,
            "saved": request.query_params.get("saved") == "1",
            "can_delete_destination": can_delete_destination,
            "destination_delete_error": destination_delete_error,
            "pull_agent_printer_inventory": pull_agent_printer_inventory,
            "pull_selected_agent_printer_options": selected_pull_agent_inventory["printers"],
            "pull_selected_agent_has_synced_printers": selected_pull_agent_inventory[
                "has_synced_printers"
            ],
            "pull_selected_agent_selected_printer_missing": selected_pull_agent_inventory[
                "selected_printer_missing"
            ],
            "pull_selected_agent_printer_count": selected_pull_agent_inventory[
                "printer_count"
            ],
            "pull_selected_agent_synced_at_label": selected_pull_agent_inventory[
                "synced_at_label"
            ],
        },
        status_code=status_code,
    )


def _pairing_display_status(pairing: PrintAgentPairing) -> str:
    status = str(pairing.status or "").strip().upper() or "-"
    if status != "EXCHANGED" and is_print_agent_pairing_expired(pairing):
        return "EXPIRED"
    return status


def _pairing_can_cancel(pairing: PrintAgentPairing) -> bool:
    return str(pairing.status or "").strip().upper() == "PENDING"


def _pull_destinations_for_agent(
    db: Session,
    *,
    tenant_id: int,
    agent_id: str,
) -> list[PrintDestination]:
    destinations = list(
        db.execute(
            select(PrintDestination).where(
                PrintDestination.tenant_id == tenant_id,
                PrintDestination.delivery_type == DELIVERY_TYPE_PRINT_AGENT_PULL,
            )
        ).scalars()
    )
    return [
        row
        for row in destinations
        if str((row.delivery_config or {}).get("agent_id", "")).strip() == str(agent_id)
    ]


def _print_agents_page_response(
    request: Request,
    *,
    db: Session,
    form_data: dict[str, str] | None = None,
    error: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    tenant_id = request_tenant_id(request)
    show_revoked = bool(
        _resolve_show_flag(request.query_params.get("show_revoked"), default=0)
    )
    show_expired_pairings = bool(
        _resolve_show_flag(
            request.query_params.get("show_expired_pairings"),
            default=0,
        )
    )
    paired_agents = list(
        db.execute(
            select(PrintAgent)
            .where(PrintAgent.tenant_id == tenant_id)
            .order_by(
                func.coalesce(PrintAgent.name, "").asc(),
                PrintAgent.id.asc(),
            )
        ).scalars()
    )
    paired_agents.sort(
        key=lambda row: (
            1 if str(row.status or "").strip().upper() == PRINT_AGENT_STATUS_REVOKED else 0,
            str(row.name or "").lower(),
            str(row.id or "").lower(),
        )
    )
    hidden_revoked_count = sum(
        1
        for item in paired_agents
        if str(item.status or "").strip().upper() == PRINT_AGENT_STATUS_REVOKED
    )
    visible_agents = (
        paired_agents
        if show_revoked
        else [
            item
            for item in paired_agents
            if str(item.status or "").strip().upper() != PRINT_AGENT_STATUS_REVOKED
        ]
    )
    agent_rows = []
    for item in visible_agents:
        blocking_destinations = _pull_destinations_for_agent(
            db,
            tenant_id=tenant_id,
            agent_id=item.id,
        )
        blocking_names = [row.name for row in blocking_destinations]
        synced_printers = normalize_print_agent_printers(item.printers_json)
        can_revoke = (
            str(item.status or "").strip().upper() != PRINT_AGENT_STATUS_REVOKED
            and not blocking_names
        )
        revoke_block_reason = ""
        if not can_revoke and blocking_names:
            joined_names = ", ".join(blocking_names[:3])
            if len(blocking_names) > 3:
                joined_names = f"{joined_names}, ..."
            revoke_block_reason = f"Assigned to {joined_names}"
        agent_rows.append(
            {
                "agent": item,
                "can_revoke": can_revoke,
                "revoke_block_reason": revoke_block_reason,
                "printer_count": len(synced_printers),
                "known_printers": _print_agent_printer_options(item),
                "printers_synced_at_label": _format_admin_datetime(item.printers_synced_at),
            }
        )

    pending_pairings = list(
        db.execute(
            select(PrintAgentPairing)
            .where(
                PrintAgentPairing.tenant_id == tenant_id,
                PrintAgentPairing.status != "EXCHANGED",
            )
            .order_by(PrintAgentPairing.created_at.desc(), PrintAgentPairing.id.desc())
        ).scalars()
    )
    pending_pairing_rows = []
    expired_pending_count = 0
    for pairing in pending_pairings:
        display_status = _pairing_display_status(pairing)
        is_expired_pending = display_status == "EXPIRED" and _pairing_can_cancel(pairing)
        if is_expired_pending:
            expired_pending_count += 1
            if not show_expired_pairings:
                continue
        pending_pairing_rows.append(
            {
                "pairing": pairing,
                "display_status": display_status,
                "can_cancel": _pairing_can_cancel(pairing),
            }
        )
    pending_pairing_rows.sort(
        key=lambda row: row["pairing"].created_at or datetime.min,
        reverse=True,
    )
    pending_pairing_rows.sort(
        key=lambda row: 1 if row["display_status"] == "EXPIRED" else 0
    )

    success = str(request.query_params.get("success_message", "")).strip()
    if not success and request.query_params.get("paired") == "1":
        agent_id = str(request.query_params.get("agent_id", "")).strip()
        paired_agent = db.get(PrintAgent, agent_id) if agent_id else None
        if paired_agent is not None:
            success = f"Paired print agent {paired_agent.name or paired_agent.id}."
        else:
            success = "Print agent paired."
    resolved_error = error or str(request.query_params.get("error_message", "")).strip()

    resolved_form_data = {
        "pairing_code": str((form_data or {}).get("pairing_code", "")),
        "agent_name": str((form_data or {}).get("agent_name", "")),
    }
    return templates.TemplateResponse(
        request,
        "admin/printing_agents.html",
        {
            "request": request,
            "active_tab": _active_printing_tab(str(request.url.path)),
            "agent_rows": agent_rows,
            "pending_pairing_rows": pending_pairing_rows,
            "form_data": resolved_form_data,
            "error": resolved_error,
            "success": success,
            "show_revoked": show_revoked,
            "show_expired_pairings": show_expired_pairings,
            "hidden_revoked_count": hidden_revoked_count,
            "expired_pending_count": expired_pending_count,
        },
        status_code=status_code,
    )


def _print_agents_redirect(
    request: Request,
    *,
    success: str = "",
    error: str = "",
) -> RedirectResponse:
    params: dict[str, str] = {}
    if success:
        params["success_message"] = success
    if error:
        params["error_message"] = error
    if _resolve_show_flag(request.query_params.get("show_revoked"), default=0):
        params["show_revoked"] = "1"
    if _resolve_show_flag(request.query_params.get("show_expired_pairings"), default=0):
        params["show_expired_pairings"] = "1"
    url = "/admin/printing/agents"
    if params:
        url = f"{url}?{urlencode(params)}"
    return RedirectResponse(url=url, status_code=303)


@router.get("/admin/printing/destinations/new", response_class=HTMLResponse)
def admin_print_destinations_new(
    request: Request,
    document_type: str | None = Query(None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    form_data = _destination_to_form()
    if document_type:
        form_data["document_type"] = _normalize_document_type(document_type)
    return _destination_form_response(
        request,
        mode="new",
        destination=None,
        form_data=form_data,
        error="",
        db=db,
    )


@router.post("/admin/printing/destinations/new", response_class=HTMLResponse)
async def admin_print_destinations_create(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    form = await request.form()
    incoming = {key: str(value) for key, value in form.multi_items()}

    name = str(incoming.get("name", "")).strip()
    description = str(incoming.get("description", "")).strip() or None
    document_type = _normalize_document_type(incoming.get("document_type", ""))
    template_id = _parse_optional_int(incoming.get("template_id"))
    delivery_type = _normalize_delivery_type(incoming.get("delivery_type", ""))
    is_default = _is_truthy(incoming.get("is_default"))
    is_active = _is_truthy(incoming.get("is_active", "1"))

    errors: list[str] = []
    if not name:
        errors.append("Destination name is required.")
    elif len(name) > CODE_MAX:
        errors.append(f"Destination name must be {CODE_MAX} characters or fewer.")
    if description and len(description) > DESC_MAX:
        errors.append(f"Description must be {DESC_MAX} characters or fewer.")

    template = db.get(PrintTemplate, template_id) if template_id else None
    if template is None:
        errors.append("Template is required.")
    elif str(template.document_type or "").strip().upper() != document_type:
        errors.append("Template document type must match destination document type.")

    if name and db.execute(
        select(PrintDestination.id).where(func.lower(PrintDestination.name) == name.lower())
    ).first():
        errors.append("Destination name already exists.")

    delivery_config: dict = {}
    if not errors:
        try:
            delivery_config = _delivery_config_from_form(
                incoming,
                delivery_type,
                document_type=document_type,
            )
        except (ValueError, json.JSONDecodeError):
            errors.append("Delivery config JSON is invalid.")
    if not errors:
        _validate_print_destination(
            db,
            template=template,
            document_type=document_type,
            delivery_type=delivery_type,
            delivery_config=delivery_config,
            errors=errors,
        )

    if errors:
        form_data = _destination_to_form()
        form_data.update(incoming)
        form_data["is_default"] = is_default
        form_data["is_active"] = is_active
        _sync_pull_printer_form_fields(form_data)
        return _destination_form_response(
            request,
            mode="new",
            destination=None,
            form_data=form_data,
            error=" ".join(errors),
            status_code=400,
            db=db,
        )

    make_default = bool(is_default and is_active)
    if make_default:
        _set_unique_default_destination(
            db,
            document_type=document_type,
            destination_id=None,
        )

    destination = PrintDestination(
        name=name,
        description=description,
        document_type=document_type,
        template_id=int(template.id),
        delivery_type=delivery_type,
        delivery_config=delivery_config,
        is_default=make_default,
        is_active=is_active,
    )
    db.add(destination)
    db.flush()
    audit_log(
        db,
        request,
        action="CREATE",
        entity_type="print_destination",
        entity_id=destination.id,
        summary=f"Created print destination {destination.name}",
        details=_print_destination_snapshot(destination, template=template),
    )
    db.commit()
    return RedirectResponse(
        url=_printing_redirect_url(request, base_path="/admin/printing/destinations"),
        status_code=303,
    )


@router.get("/admin/printing/destinations/{destination_id:int}/edit", response_class=HTMLResponse)
def admin_print_destinations_edit(
    destination_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    destination = db.get(PrintDestination, destination_id)
    if destination is None:
        return HTMLResponse("Destination not found.", status_code=404)
    return _destination_form_response(
        request,
        mode="edit",
        destination=destination,
        form_data=_destination_to_form(destination),
        error=request.query_params.get("error", ""),
        db=db,
    )


@router.post("/admin/printing/destinations/{destination_id:int}/edit", response_class=HTMLResponse)
async def admin_print_destinations_update(
    destination_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    destination = db.get(PrintDestination, destination_id)
    if destination is None:
        return HTMLResponse("Destination not found.", status_code=404)

    form = await request.form()
    incoming = {key: str(value) for key, value in form.multi_items()}

    name = str(incoming.get("name", "")).strip()
    description = str(incoming.get("description", "")).strip() or None
    document_type = _normalize_document_type(incoming.get("document_type", ""))
    template_id = _parse_optional_int(incoming.get("template_id"))
    delivery_type = _normalize_delivery_type(incoming.get("delivery_type", ""))
    is_default = _is_truthy(incoming.get("is_default"))
    is_active = _is_truthy(incoming.get("is_active", "1"))

    errors: list[str] = []
    if not name:
        errors.append("Destination name is required.")
    elif len(name) > CODE_MAX:
        errors.append(f"Destination name must be {CODE_MAX} characters or fewer.")
    if description and len(description) > DESC_MAX:
        errors.append(f"Description must be {DESC_MAX} characters or fewer.")

    template = db.get(PrintTemplate, template_id) if template_id else None
    if template is None:
        errors.append("Template is required.")
    elif str(template.document_type or "").strip().upper() != document_type:
        errors.append("Template document type must match destination document type.")

    duplicate = db.execute(
        select(PrintDestination.id).where(
            func.lower(PrintDestination.name) == name.lower(),
            PrintDestination.id != destination.id,
        )
    ).first()
    if duplicate:
        errors.append("Destination name already exists.")

    delivery_config: dict = {}
    if not errors:
        try:
            delivery_config = _delivery_config_from_form(
                incoming,
                delivery_type,
                document_type=document_type,
            )
        except (ValueError, json.JSONDecodeError):
            errors.append("Delivery config JSON is invalid.")
    if not errors:
        _validate_print_destination(
            db,
            template=template,
            document_type=document_type,
            delivery_type=delivery_type,
            delivery_config=delivery_config,
            errors=errors,
        )

    if errors:
        form_data = _destination_to_form(destination)
        form_data.update(incoming)
        form_data["is_default"] = is_default
        form_data["is_active"] = is_active
        _sync_pull_printer_form_fields(form_data)
        return _destination_form_response(
            request,
            mode="edit",
            destination=destination,
            form_data=form_data,
            error=" ".join(errors),
            status_code=400,
            db=db,
        )

    make_default = bool(is_default and is_active)
    if make_default:
        _set_unique_default_destination(
            db,
            document_type=document_type,
            destination_id=destination.id,
        )

    before_audit = _print_destination_snapshot(
        destination,
        template=db.get(PrintTemplate, destination.template_id) if destination.template_id else None,
    )
    destination.name = name
    destination.description = description
    destination.document_type = document_type
    destination.template_id = int(template.id)
    destination.delivery_type = delivery_type
    destination.delivery_config = delivery_config
    destination.is_active = is_active
    destination.is_default = make_default
    after_audit = _print_destination_snapshot(destination, template=template)
    change_details = audit_diff(
        before_audit,
        after_audit,
        [
            "name",
            "description",
            "document_type",
            "template_id",
            "template_code",
            "delivery_type",
            "auto_print_on_complete",
            "is_default",
            "is_active",
        ],
    )
    if change_details["changed"]:
        audit_log(
            db,
            request,
            action="UPDATE",
            entity_type="print_destination",
            entity_id=destination.id,
            summary=f"Updated print destination {destination.name}",
            details=change_details,
        )
    db.commit()
    return RedirectResponse(
        url=_printing_redirect_url(
            request,
            base_path=f"/admin/printing/destinations/{destination.id}/edit",
        ),
        status_code=303,
    )


@router.post("/admin/printing/destinations/{destination_id:int}/deactivate")
def admin_print_destinations_deactivate(
    destination_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    destination = db.get(PrintDestination, destination_id)
    if destination is None:
        return HTMLResponse("Destination not found.", status_code=404)
    destination.is_active = False
    destination.is_default = False
    audit_log(
        db,
        request,
        action="DEACTIVATE",
        entity_type="print_destination",
        entity_id=destination.id,
        summary=f"Deactivated print destination {destination.name}",
        details=_print_destination_snapshot(
            destination,
            template=db.get(PrintTemplate, destination.template_id) if destination.template_id else None,
        ),
    )
    db.commit()
    return RedirectResponse(
        url=_printing_redirect_url(request, base_path="/admin/printing/destinations"),
        status_code=303,
    )


@router.post("/admin/printing/destinations/{destination_id:int}/reactivate")
def admin_print_destinations_reactivate(
    destination_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    destination = db.get(PrintDestination, destination_id)
    if destination is None:
        return HTMLResponse("Destination not found.", status_code=404)
    destination.is_active = True
    has_other_default = db.execute(
        select(PrintDestination.id)
        .where(
            PrintDestination.document_type == destination.document_type,
            PrintDestination.is_default.is_(True),
            PrintDestination.is_active.is_(True),
            PrintDestination.id != destination.id,
        )
        .limit(1)
    ).first()
    if not has_other_default:
        destination.is_default = True
    audit_log(
        db,
        request,
        action="REACTIVATE",
        entity_type="print_destination",
        entity_id=destination.id,
        summary=f"Reactivated print destination {destination.name}",
        details=_print_destination_snapshot(
            destination,
            template=db.get(PrintTemplate, destination.template_id) if destination.template_id else None,
        ),
    )
    db.commit()
    return RedirectResponse(
        url=_printing_redirect_url(request, base_path="/admin/printing/destinations"),
        status_code=303,
    )


@router.post("/admin/printing/destinations/{destination_id:int}/delete")
def admin_print_destinations_delete(
    destination_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    destination = db.get(PrintDestination, destination_id)
    if destination is None:
        return HTMLResponse("Destination not found.", status_code=404)

    block_error = _destination_delete_block_error(
        destination,
        has_jobs=_destination_has_jobs(db, destination_id),
    )
    if block_error:
        return RedirectResponse(
            url=_printing_redirect_url(
                request,
                base_path="/admin/printing/destinations",
                include_saved=False,
                extra={"error": block_error},
            ),
            status_code=303,
        )

    audit_log(
        db,
        request,
        action="DELETE",
        entity_type="print_destination",
        entity_id=destination.id,
        summary=f"Deleted print destination {destination.name}",
        details=_print_destination_snapshot(
            destination,
            template=db.get(PrintTemplate, destination.template_id) if destination.template_id else None,
        ),
    )
    db.delete(destination)
    db.commit()
    return RedirectResponse(
        url=_printing_redirect_url(request, base_path="/admin/printing/destinations"),
        status_code=303,
    )


@router.get("/admin/printing/templates", response_class=HTMLResponse)
def admin_print_templates_list(
    request: Request,
    q: str | None = Query(None),
    document_type: str | None = Query(None),
    hide_inactive: int | None = Query(None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    resolved_hide = _resolve_hide_inactive(hide_inactive)
    normalized_document_type = _normalize_document_type(document_type) if document_type else ""

    filters = []
    if q:
        search = f"%{q.strip()}%"
        filters.append(
            or_(
                PrintTemplate.code.ilike(search),
                PrintTemplate.description.ilike(search),
            )
        )
    if normalized_document_type:
        filters.append(PrintTemplate.document_type == normalized_document_type)
    if resolved_hide:
        filters.append(PrintTemplate.is_active.is_(True))

    query = select(PrintTemplate).where(and_(*filters)) if filters else select(PrintTemplate)
    items = sorted(list(db.execute(query).scalars()), key=_template_list_order_key)

    return templates.TemplateResponse(
        request,
        "admin/printing_templates_list.html",
        {
            "request": request,
            "active_tab": _active_printing_tab(str(request.url.path)),
            "items": items,
            "q": q or "",
            "document_type": normalized_document_type,
            "document_type_options": DOCUMENT_TYPE_OPTIONS,
            "hide_inactive": bool(resolved_hide),
            "error": request.query_params.get("error", ""),
            "saved": request.query_params.get("saved") == "1",
        },
    )


def _template_form_response(
    request: Request,
    *,
    mode: str,
    template: PrintTemplate | None,
    form_data: dict[str, str | bool],
    error: str,
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
            "document_type_options": DOCUMENT_TYPE_OPTIONS,
            "format_options": TEMPLATE_FORMAT_OPTIONS,
            "saved": request.query_params.get("saved") == "1",
        },
        status_code=status_code,
    )


@router.get("/admin/printing/templates/new", response_class=HTMLResponse)
def admin_print_templates_new(
    request: Request,
    document_type: str | None = Query(None),
) -> HTMLResponse:
    form_data = _template_to_form()
    if document_type:
        form_data["document_type"] = _normalize_document_type(document_type)
    return _template_form_response(
        request,
        mode="new",
        template=None,
        form_data=form_data,
        error="",
    )


@router.post("/admin/printing/templates/new", response_class=HTMLResponse)
async def admin_print_templates_create(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    form = await request.form()
    incoming = {key: str(value) for key, value in form.multi_items()}
    code = str(incoming.get("code", "")).strip() or None
    description = str(incoming.get("description", "")).strip() or None
    document_type = _normalize_document_type(incoming.get("document_type", ""))
    template_format = _normalize_format(incoming.get("format", ""))
    content = str(incoming.get("content", "")).strip()
    is_active = _is_truthy(incoming.get("is_active", "1"))

    errors: list[str] = []
    if code and len(code) > CODE_MAX:
        errors.append(f"Code must be {CODE_MAX} characters or fewer.")
    if description and len(description) > DESC_MAX:
        errors.append(f"Description must be {DESC_MAX} characters or fewer.")
    if not content:
        errors.append("Template content is required.")
    if code and db.execute(
        select(PrintTemplate.id).where(func.lower(PrintTemplate.code) == code.lower())
    ).first():
        errors.append("Template code already exists.")

    if errors:
        form_data = _template_to_form()
        form_data.update(incoming)
        form_data["is_active"] = is_active
        return _template_form_response(
            request,
            mode="new",
            template=None,
            form_data=form_data,
            error=" ".join(errors),
            status_code=400,
        )

    template = PrintTemplate(
        code=code,
        description=description,
        document_type=document_type,
        format=template_format,
        content=content,
        is_system=False,
        is_active=is_active,
    )
    db.add(template)
    db.flush()
    audit_log(
        db,
        request,
        action="CREATE",
        entity_type="print_template",
        entity_id=template.id,
        summary=(
            f"Created print template {template.code}"
            if template.code
            else f"Created print template #{template.id}"
        ),
        details=_print_template_snapshot(template),
    )
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
        return HTMLResponse("Template not found.", status_code=404)
    return _template_form_response(
        request,
        mode="edit",
        template=template,
        form_data=_template_to_form(template),
        error=request.query_params.get("error", ""),
    )


@router.post("/admin/printing/templates/{template_id:int}/edit", response_class=HTMLResponse)
async def admin_print_templates_update(
    template_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    template = db.get(PrintTemplate, template_id)
    if template is None:
        return HTMLResponse("Template not found.", status_code=404)
    if bool(template.is_system):
        return _template_form_response(
            request,
            mode="edit",
            template=template,
            form_data=_template_to_form(template),
            error=SYSTEM_TEMPLATE_LOCK_ERROR,
            status_code=403,
        )

    form = await request.form()
    incoming = {key: str(value) for key, value in form.multi_items()}
    code = str(incoming.get("code", "")).strip() or None
    description = str(incoming.get("description", "")).strip() or None
    document_type = _normalize_document_type(incoming.get("document_type", ""))
    template_format = _normalize_format(incoming.get("format", ""))
    content = str(incoming.get("content", "")).strip()
    is_active = _is_truthy(incoming.get("is_active", "1"))

    errors: list[str] = []
    if code and len(code) > CODE_MAX:
        errors.append(f"Code must be {CODE_MAX} characters or fewer.")
    if description and len(description) > DESC_MAX:
        errors.append(f"Description must be {DESC_MAX} characters or fewer.")
    if not content:
        errors.append("Template content is required.")
    if code and db.execute(
        select(PrintTemplate.id).where(
            func.lower(PrintTemplate.code) == code.lower(),
            PrintTemplate.id != template.id,
        )
    ).first():
        errors.append("Template code already exists.")

    if errors:
        form_data = _template_to_form(template)
        form_data.update(incoming)
        form_data["is_active"] = is_active
        return _template_form_response(
            request,
            mode="edit",
            template=template,
            form_data=form_data,
            error=" ".join(errors),
            status_code=400,
        )

    before_audit = _print_template_snapshot(template)
    template.code = code
    template.description = description
    template.document_type = document_type
    template.format = template_format
    template.content = content
    template.is_active = is_active
    after_audit = _print_template_snapshot(template)
    change_details = audit_diff(
        before_audit,
        after_audit,
        ["code", "description", "document_type", "format", "is_active"],
    )
    if change_details["changed"]:
        audit_log(
            db,
            request,
            action="UPDATE",
            entity_type="print_template",
            entity_id=template.id,
            summary=(
                f"Updated print template {template.code}"
                if template.code
                else f"Updated print template #{template.id}"
            ),
            details=change_details,
        )
    db.commit()
    return RedirectResponse(
        url=_printing_redirect_url(
            request,
            base_path=f"/admin/printing/templates/{template.id}/edit",
        ),
        status_code=303,
    )


@router.post("/admin/printing/templates/{template_id:int}/duplicate")
def admin_print_templates_duplicate(
    template_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    template = db.get(PrintTemplate, template_id)
    if template is None:
        return HTMLResponse("Template not found.", status_code=404)

    source_name = str(
        template.description or template.code or f"Template {template.id}"
    ).strip()
    copy_description = f"Copy of {source_name}" if source_name else "Copy of template"
    if len(copy_description) > DESC_MAX:
        copy_description = copy_description[:DESC_MAX].rstrip()

    duplicate = PrintTemplate(
        code=None,
        description=copy_description or None,
        document_type=str(template.document_type or DOCUMENT_TYPE_TICKET),
        format=str(template.format or PRINT_CONTENT_TYPE_TEXT),
        content=str(template.content or ""),
        is_system=False,
        is_active=bool(template.is_active),
    )
    db.add(duplicate)
    db.commit()
    db.refresh(duplicate)
    audit_log(
        db,
        request,
        action="CREATE",
        entity_type="print_template",
        entity_id=duplicate.id,
        summary=f"Duplicated print template from {source_name}",
        details=_print_template_snapshot(duplicate),
    )
    db.commit()
    return RedirectResponse(
        url=_printing_redirect_url(
            request,
            base_path=f"/admin/printing/templates/{duplicate.id}/edit",
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
        return HTMLResponse("Template not found.", status_code=404)
    if bool(template.is_system):
        return _template_form_response(
            request,
            mode="edit",
            template=template,
            form_data=_template_to_form(template),
            error=SYSTEM_TEMPLATE_LOCK_ERROR,
            status_code=403,
        )
    template.is_active = False
    audit_log(
        db,
        request,
        action="DEACTIVATE",
        entity_type="print_template",
        entity_id=template.id,
        summary=(
            f"Deactivated print template {template.code}"
            if template.code
            else f"Deactivated print template #{template.id}"
        ),
        details=_print_template_snapshot(template),
    )
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
        return HTMLResponse("Template not found.", status_code=404)
    template.is_active = True
    audit_log(
        db,
        request,
        action="REACTIVATE",
        entity_type="print_template",
        entity_id=template.id,
        summary=(
            f"Reactivated print template {template.code}"
            if template.code
            else f"Reactivated print template #{template.id}"
        ),
        details=_print_template_snapshot(template),
    )
    db.commit()
    return RedirectResponse(
        url=_printing_redirect_url(request, base_path="/admin/printing/templates"),
        status_code=303,
    )


@router.post("/admin/printing/templates/{template_id:int}/delete")
def admin_print_templates_delete(
    template_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    template = db.get(PrintTemplate, template_id)
    if template is None:
        return HTMLResponse("Template not found.", status_code=404)
    if bool(template.is_system):
        return _template_form_response(
            request,
            mode="edit",
            template=template,
            form_data=_template_to_form(template),
            error=SYSTEM_TEMPLATE_LOCK_ERROR,
            status_code=403,
        )
    in_use = db.execute(
        select(PrintDestination.id).where(PrintDestination.template_id == template.id).limit(1)
    ).first()
    if in_use:
        return RedirectResponse(
            url=_printing_redirect_url(
                request,
                base_path="/admin/printing/templates",
                include_saved=False,
                extra={"error": "Template is in use by one or more destinations."},
            ),
            status_code=303,
        )
    audit_log(
        db,
        request,
        action="DELETE",
        entity_type="print_template",
        entity_id=template.id,
        summary=(
            f"Deleted print template {template.code}"
            if template.code
            else f"Deleted print template #{template.id}"
        ),
        details=_print_template_snapshot(template),
    )
    db.delete(template)
    db.commit()
    return RedirectResponse(
        url=_printing_redirect_url(request, base_path="/admin/printing/templates"),
        status_code=303,
    )


@router.get("/admin/printing/templates/{template_id:int}/preview", response_class=HTMLResponse)
def admin_print_templates_preview(
    template_id: int,
    request: Request,
    ticket_id: int | None = Query(None),
    invoice_id: int | None = Query(None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    template = db.get(PrintTemplate, template_id)
    if template is None:
        return HTMLResponse("Template not found.", status_code=404)

    document_type = _normalize_document_type(str(template.document_type or ""))
    preview_subject = "sample payload"
    preview_label = "Built-in sample data"
    rendered = ""
    error = ""
    try:
        if document_type == DOCUMENT_TYPE_INVOICE:
            preview_invoice = db.get(Invoice, invoice_id) if invoice_id else None
            if preview_invoice is None:
                preview_invoice = (
                    db.execute(select(Invoice).order_by(Invoice.id.desc()).limit(1))
                    .scalars()
                    .first()
                )
            if preview_invoice is not None:
                try:
                    payload = build_print_payload(
                        db,
                        DOCUMENT_TYPE_INVOICE,
                        source_id=int(preview_invoice.id),
                    )
                except (LookupError, ValueError):
                    payload = build_print_payload(db, DOCUMENT_TYPE_INVOICE)
                preview_subject = "invoice"
                preview_label = str(preview_invoice.invoice_no or f"#{preview_invoice.id}")
            else:
                payload = build_print_payload(db, DOCUMENT_TYPE_INVOICE)
                preview_subject = "sample invoice payload"
                preview_label = "No invoice found"
            rendered = render_from_content(payload, template.content, db=db)
        else:
            preview_ticket = db.get(Ticket, ticket_id) if ticket_id else None
            if preview_ticket is None:
                preview_ticket = (
                    db.execute(select(Ticket).order_by(Ticket.id.desc()).limit(1)).scalars().first()
                )
            source_id = int(preview_ticket.id) if preview_ticket is not None else None
            if source_id is not None:
                try:
                    payload = build_print_payload(
                        db,
                        document_type,
                        source_id=source_id,
                    )
                except (LookupError, ValueError):
                    payload = build_print_payload(db, document_type)
                preview_subject = "ticket"
                preview_label = str(preview_ticket.ticket_no or f"#{preview_ticket.id}")
            else:
                payload = build_print_payload(db, document_type)
                if document_type == DOCUMENT_TYPE_WTN:
                    preview_subject = "sample WTN payload"
                else:
                    preview_subject = "sample ticket payload"
                preview_label = "No ticket found"
            rendered = render_from_content(payload, template.content, db=db)
    except Exception as exc:
        error = str(exc) or "Template render failed."

    if not error and _normalize_format(template.format) == PRINT_CONTENT_TYPE_HTML:
        return HTMLResponse(rendered)

    return templates.TemplateResponse(
        request,
        "admin/printing_template_preview_text.html",
        {
            "request": request,
            "template": template,
            "rendered": rendered,
            "error": error,
            "preview_subject": preview_subject,
            "preview_label": preview_label,
        },
    )

@router.get("/admin/printing/jobs", response_class=HTMLResponse)
def admin_print_jobs_list(
    request: Request,
    status: str | None = Query(None),
    document_type: str | None = Query(None),
    destination_id: int | None = Query(None),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    filters = []
    if status:
        filters.append(PrintJob.status == status.strip().upper())
    if document_type:
        filters.append(PrintJob.document_type == _normalize_document_type(document_type))
    if destination_id:
        filters.append(PrintJob.destination_id == destination_id)
    if date_from:
        filters.append(PrintJob.created_at >= datetime.combine(date_from, time.min))
    if date_to:
        filters.append(PrintJob.created_at < datetime.combine(date_to + timedelta(days=1), time.min))

    query = select(PrintJob).where(and_(*filters)) if filters else select(PrintJob)
    items = list(
        db.execute(query.order_by(PrintJob.created_at.desc(), PrintJob.id.desc()).limit(300)).scalars()
    )
    destinations = list(db.execute(select(PrintDestination).order_by(PrintDestination.name.asc())).scalars())
    destinations_by_id = {row.id: row for row in destinations}

    template_ids = [int(row.template_id) for row in items if row.template_id]
    templates_by_id: dict[int, PrintTemplate] = {}
    if template_ids:
        templates_by_id = {
            row.id: row
            for row in db.execute(select(PrintTemplate).where(PrintTemplate.id.in_(template_ids))).scalars()
        }
    ticket_ids = [int(row.ticket_id) for row in items if row.ticket_id]
    tickets_by_id: dict[int, Ticket] = {}
    if ticket_ids:
        tickets_by_id = {
            row.id: row
            for row in db.execute(select(Ticket).where(Ticket.id.in_(ticket_ids))).scalars()
        }
    invoice_ids = [int(row.invoice_id) for row in items if row.invoice_id]
    invoices_by_id: dict[int, Invoice] = {}
    if invoice_ids:
        invoices_by_id = {
            row.id: row
            for row in db.execute(select(Invoice).where(Invoice.id.in_(invoice_ids))).scalars()
        }

    job_document_refs: dict[int, str] = {}
    for row in items:
        if row.invoice_id and row.invoice_id in invoices_by_id:
            invoice = invoices_by_id[row.invoice_id]
            job_document_refs[row.id] = f"Invoice {invoice.invoice_no}"
        elif row.ticket_id and row.ticket_id in tickets_by_id:
            ticket = tickets_by_id[row.ticket_id]
            job_document_refs[row.id] = f"Ticket {ticket.ticket_no}"
        else:
            job_document_refs[row.id] = "-"

    job_rows: list[dict[str, object]] = []
    for item in items:
        destination = destinations_by_id.get(item.destination_id) if item.destination_id else None
        template = templates_by_id.get(item.template_id) if item.template_id else None
        job_rows.append(
            {
                "job": item,
                "destination": destination,
                "template": template,
                "document_ref": job_document_refs.get(item.id, "-"),
                "trigger_source_label": _print_job_trigger_source_label(item.trigger_source),
                "printer_or_target": _print_job_printer_or_target(item),
                "can_retry": _print_job_can_retry(item),
                "error_summary": str(item.last_error or "").strip() or "-",
            }
        )

    return templates.TemplateResponse(
        request,
        "admin/printing_jobs_list.html",
        {
            "request": request,
            "active_tab": _active_printing_tab(str(request.url.path)),
            "items": items,
            "job_rows": job_rows,
            "destinations": destinations,
            "destinations_by_id": destinations_by_id,
            "templates_by_id": templates_by_id,
            "job_document_refs": job_document_refs,
            "status_options": JOB_STATUS_FILTER_OPTIONS,
            "document_type_options": DOCUMENT_TYPE_OPTIONS,
            "filters": {
                "status": status or "",
                "document_type": _normalize_document_type(document_type) if document_type else "",
                "destination_id": str(destination_id or ""),
                "date_from": date_from.isoformat() if date_from else "",
                "date_to": date_to.isoformat() if date_to else "",
            },
            "saved": request.query_params.get("saved") == "1",
            "retry_success_message": request.query_params.get("retry_success_message", ""),
            "retry_error_message": request.query_params.get("retry_error_message", ""),
        },
    )


@router.get("/admin/printing/template-variables")
def admin_print_template_variables_redirect() -> RedirectResponse:
    return RedirectResponse(url="/admin/help/template-variables", status_code=303)


@router.get("/admin/printing/jobs/{job_id:int}", response_class=HTMLResponse)
def admin_print_job_detail(
    job_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    job = db.get(PrintJob, job_id)
    if job is None:
        return HTMLResponse("Print job not found.", status_code=404)
    destination = db.get(PrintDestination, job.destination_id) if job.destination_id else None
    template = db.get(PrintTemplate, job.template_id) if job.template_id else None
    ticket = db.get(Ticket, job.ticket_id) if job.ticket_id else None
    invoice = db.get(Invoice, job.invoice_id) if job.invoice_id else None
    requested_by_user = db.get(User, job.created_by_user_id) if job.created_by_user_id else None
    if requested_by_user is not None:
        requested_by_label = user_display_name(requested_by_user)
    elif job.created_by_user_id:
        requested_by_label = f"User #{job.created_by_user_id}"
    else:
        requested_by_label = "-"
    provider_response_pretty = "-"
    if job.provider_response_json is not None:
        try:
            provider_response_pretty = json.dumps(
                job.provider_response_json,
                indent=2,
                sort_keys=True,
                default=str,
            )
        except (TypeError, ValueError):
            provider_response_pretty = str(job.provider_response_json)
    if invoice is not None:
        document_reference = f"Invoice {invoice.invoice_no}"
    elif ticket is not None:
        document_reference = f"Ticket {ticket.ticket_no}"
    else:
        document_reference = "-"
    return templates.TemplateResponse(
        request,
        "admin/printing_job_detail.html",
        {
            "request": request,
            "active_tab": _active_printing_tab(str(request.url.path)),
            "job": job,
            "destination": destination,
            "template": template,
            "ticket": ticket,
            "invoice": invoice,
            "requested_by_user": requested_by_user,
            "requested_by_label": requested_by_label,
            "provider_response_pretty": provider_response_pretty,
            "document_reference": document_reference,
            "printer_or_target": _print_job_printer_or_target(job),
            "trigger_source_label": _print_job_trigger_source_label(job.trigger_source),
            "can_retry": _print_job_can_retry(job),
            "retried_before": int(job.attempt_count or 0) > 1,
            "saved": request.query_params.get("saved") == "1",
            "retry_success_message": request.query_params.get(
                "retry_success_message",
                "",
            ),
            "retry_error": request.query_params.get("retry_error_message", ""),
        },
    )


@router.post("/admin/printing/jobs/{job_id:int}/retry")
def admin_print_job_retry(
    job_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    return_to = str(request.query_params.get("return_to", "")).strip()
    job = db.get(PrintJob, job_id)
    if job is None:
        return RedirectResponse(url="/admin/printing/jobs", status_code=303)
    try:
        retry_print_job(db, job)
    except Exception as exc:
        return RedirectResponse(
            url=_print_job_retry_redirect_url(
                job_id=job.id,
                return_to=return_to,
                error_message=str(exc),
            ),
            status_code=303,
        )
    audit_log(
        db,
        request,
        action="RETRY",
        entity_type="print_job",
        entity_id=job.id,
        summary=f"Retried print job #{job.id}",
        details={
            "document_type": str(job.document_type or "").strip() or None,
            "destination_id": job.destination_id,
            "template_id": job.template_id,
            "status": str(job.status or "").strip() or None,
        },
    )
    db.commit()
    return RedirectResponse(
        url=_print_job_retry_redirect_url(
            job_id=job.id,
            return_to=return_to,
            success_message=_print_job_retry_success_message(job),
        ),
        status_code=303,
    )
