from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ..constants import CODE_MAX, DESC_MAX
from ..db import get_db
from ..models import Invoice, PrintDestination, PrintJob, PrintTemplate, Ticket
from ..services.print_payload import (
    build_print_payload,
)
from ..services.print_render import render_from_content
from ..services.printing import (
    DELIVERY_TYPE_EMAIL_PDF,
    DELIVERY_TYPE_PRINT_LOCAL_BROWSER,
    DELIVERY_TYPE_PRINT_NETWORK_RAW_9100,
    DELIVERY_TYPE_PRINT_NODE_HTTP,
    DOCUMENT_TYPE_INVOICE,
    DOCUMENT_TYPE_TICKET,
    DOCUMENT_TYPE_WTN,
    PRINT_CONTENT_TYPE_HTML,
    PRINT_CONTENT_TYPE_TEXT,
    PRINT_JOB_STATUS_FAILED,
    PRINT_JOB_STATUS_QUEUED,
    PRINT_JOB_STATUS_SENT,
    retry_print_job,
)
from ..templating import templates

router = APIRouter()

DOCUMENT_TYPE_OPTIONS = (
    (DOCUMENT_TYPE_TICKET, "Ticket"),
    (DOCUMENT_TYPE_INVOICE, "Invoice"),
    (DOCUMENT_TYPE_WTN, "WTN"),
)
DOCUMENT_TYPE_VALUES = {value for value, _ in DOCUMENT_TYPE_OPTIONS}
DELIVERY_TYPE_OPTIONS = (
    (DELIVERY_TYPE_PRINT_LOCAL_BROWSER, "Print: Local Browser"),
    (DELIVERY_TYPE_PRINT_NETWORK_RAW_9100, "Print: Network RAW 9100"),
    (DELIVERY_TYPE_PRINT_NODE_HTTP, "Print: Node HTTP"),
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
JOB_STATUS_FILTER_OPTIONS = (
    (PRINT_JOB_STATUS_SENT, "Sent"),
    (PRINT_JOB_STATUS_FAILED, "Failed"),
    (PRINT_JOB_STATUS_QUEUED, "Queued"),
)


def _active_printing_tab(path: str) -> str:
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
        "email_to": str(config.get("to", "")),
        "email_cc": str(config.get("cc", "")),
        "email_bcc": str(config.get("bcc", "")),
        "email_subject_template": str(config.get("email_subject_template", "")),
        "email_body_template": str(config.get("email_body_template", "")),
        "attach_pdf": bool(config.get("attach_pdf", True)),
        "is_default": bool(destination.is_default) if destination else False,
        "is_active": bool(destination.is_active) if destination else True,
    }


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


def _delivery_config_from_form(form: dict[str, str], delivery_type: str) -> dict:
    normalized = _normalize_delivery_type(delivery_type)
    config: dict[str, object] = {}
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
    elif normalized == DELIVERY_TYPE_EMAIL_PDF:
        for key in (
            "email_to",
            "email_cc",
            "email_bcc",
            "email_subject_template",
            "email_body_template",
        ):
            value = str(form.get(key, "")).strip()
            if value:
                config[key.replace("email_", "")] = value
        config["attach_pdf"] = _is_truthy(form.get("attach_pdf"))

    raw_json = str(form.get("delivery_config", "")).strip()
    if raw_json:
        parsed = json.loads(raw_json)
        if isinstance(parsed, dict):
            config.update(parsed)
    return config


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
    template_options = sorted(
        list(db.execute(select(PrintTemplate)).scalars()),
        key=_template_order_key,
    )
    can_delete_destination = False
    destination_delete_error = ""
    if destination is not None:
        has_jobs = _destination_has_jobs(db, int(destination.id))
        destination_delete_error = (
            _destination_delete_block_error(destination, has_jobs=has_jobs) or ""
        )
        can_delete_destination = not bool(destination_delete_error)
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
            "template_options": template_options,
            "saved": request.query_params.get("saved") == "1",
            "can_delete_destination": can_delete_destination,
            "destination_delete_error": destination_delete_error,
        },
        status_code=status_code,
    )


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

    if delivery_type == DELIVERY_TYPE_EMAIL_PDF and document_type != DOCUMENT_TYPE_INVOICE:
        errors.append("EMAIL_PDF destinations are only supported for Invoice.")

    if name and db.execute(
        select(PrintDestination.id).where(func.lower(PrintDestination.name) == name.lower())
    ).first():
        errors.append("Destination name already exists.")

    delivery_config: dict = {}
    if not errors:
        try:
            delivery_config = _delivery_config_from_form(incoming, delivery_type)
        except (ValueError, json.JSONDecodeError):
            errors.append("Delivery config JSON is invalid.")

    if errors:
        form_data = _destination_to_form()
        form_data.update(incoming)
        form_data["is_default"] = is_default
        form_data["is_active"] = is_active
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

    if delivery_type == DELIVERY_TYPE_EMAIL_PDF and document_type != DOCUMENT_TYPE_INVOICE:
        errors.append("EMAIL_PDF destinations are only supported for Invoice.")

    delivery_config: dict = {}
    if not errors:
        try:
            delivery_config = _delivery_config_from_form(incoming, delivery_type)
        except (ValueError, json.JSONDecodeError):
            errors.append("Delivery config JSON is invalid.")

    if errors:
        form_data = _destination_to_form(destination)
        form_data.update(incoming)
        form_data["is_default"] = is_default
        form_data["is_active"] = is_active
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

    destination.name = name
    destination.description = description
    destination.document_type = document_type
    destination.template_id = int(template.id)
    destination.delivery_type = delivery_type
    destination.delivery_config = delivery_config
    destination.is_active = is_active
    destination.is_default = make_default
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

    template.code = code
    template.description = description
    template.document_type = document_type
    template.format = template_format
    template.content = content
    template.is_active = is_active
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
            else:
                payload = build_print_payload(db, DOCUMENT_TYPE_INVOICE)
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
            else:
                payload = build_print_payload(db, document_type)
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

    return templates.TemplateResponse(
        request,
        "admin/printing_jobs_list.html",
        {
            "request": request,
            "active_tab": _active_printing_tab(str(request.url.path)),
            "items": items,
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
    except Exception as exc:
        return RedirectResponse(
            url=_printing_redirect_url(
                request,
                base_path=f"/admin/printing/jobs/{job.id}",
                extra={"retry_error": str(exc)},
            ),
            status_code=303,
        )
    return RedirectResponse(
        url=_printing_redirect_url(request, base_path=f"/admin/printing/jobs/{job.id}"),
        status_code=303,
    )
