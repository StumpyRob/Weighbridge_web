from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import log as audit_log
from ..db import get_db
from ..models import WorkstationPrinterProfile
from ..permissions import PERM_MANAGE_SETTINGS, require_permission
from ..services.qz_printing import (
    QZ_WORKSTATION_DOCUMENT_TYPES,
    ensure_workstation_profile_rows,
    normalize_workstation_key,
    normalize_workstation_label,
    normalize_workstation_printer_name,
)
from ..templating import templates
from ..tenancy import request_platform_mode

router = APIRouter()

DOCUMENT_LABELS = {
    "TICKET": "Ticket",
    "WTN": "WTN",
    "INVOICE": "Invoice",
}


@dataclass(frozen=True)
class _WorkstationAdminRow:
    workstation_key: str
    workstation_label: str
    profiles: dict[str, WorkstationPrinterProfile]


def _require_tenant_settings_admin(request: Request) -> None:
    if request_platform_mode(request):
        raise HTTPException(status_code=404, detail="Not Found")
    require_permission(request, PERM_MANAGE_SETTINGS)


def _workstations_url(*, message: str | None = None, error: str | None = None) -> str:
    params: dict[str, str] = {}
    if message:
        params["message"] = message
    if error:
        params["error"] = error
    if not params:
        return "/admin/printing/workstations"
    return f"/admin/printing/workstations?{urlencode(params)}"


def _group_workstation_profiles(
    rows: list[WorkstationPrinterProfile],
) -> list[_WorkstationAdminRow]:
    grouped: dict[str, dict[str, object]] = {}
    for row in rows:
        workstation_key = normalize_workstation_key(row.workstation_key)
        if not workstation_key:
            continue
        entry = grouped.setdefault(
            workstation_key,
            {
                "label": "",
                "profiles": {},
            },
        )
        label = normalize_workstation_label(row.workstation_label)
        if label:
            entry["label"] = label
        entry["profiles"][str(row.document_type or "").strip().upper()] = row

    items = [
        _WorkstationAdminRow(
            workstation_key=workstation_key,
            workstation_label=str(payload["label"] or ""),
            profiles=dict(payload["profiles"]),
        )
        for workstation_key, payload in grouped.items()
    ]
    items.sort(
        key=lambda item: (
            0 if item.workstation_label else 1,
            item.workstation_label.lower() if item.workstation_label else "",
            item.workstation_key.lower(),
        ),
    )
    return items


@router.get("/admin/printing/workstations", response_class=HTMLResponse)
def admin_printing_workstations(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    _require_tenant_settings_admin(request)
    rows = list(
        db.execute(
            select(WorkstationPrinterProfile).order_by(
                WorkstationPrinterProfile.workstation_key.asc(),
                WorkstationPrinterProfile.document_type.asc(),
            )
        ).scalars()
    )
    return templates.TemplateResponse(
        request,
        "admin/printing_workstations.html",
        {
            "request": request,
            "active_tab": "workstations",
            "items": _group_workstation_profiles(rows),
            "document_types": tuple(QZ_WORKSTATION_DOCUMENT_TYPES),
            "document_labels": DOCUMENT_LABELS,
            "message": request.query_params.get("message", ""),
            "error": request.query_params.get("error", ""),
        },
    )


@router.post("/admin/printing/workstations/{workstation_key}/update")
async def admin_printing_workstation_update(
    workstation_key: str,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    _require_tenant_settings_admin(request)
    normalized_key = normalize_workstation_key(workstation_key)
    if not normalized_key:
        return RedirectResponse(
            url=_workstations_url(error="Workstation not found."),
            status_code=303,
        )

    form = await request.form()
    workstation_label = normalize_workstation_label(form.get("workstation_label"))
    rows, _changed = ensure_workstation_profile_rows(
        db,
        workstation_key=normalized_key,
    )
    rows_by_document_type = {
        str(row.document_type or "").strip().upper(): row
        for row in rows
    }

    changed = False
    for row in rows:
        if row.workstation_label != (workstation_label or None):
            row.workstation_label = workstation_label or None
            changed = True

    for document_type in QZ_WORKSTATION_DOCUMENT_TYPES:
        row = rows_by_document_type.get(document_type)
        if row is None:
            continue
        key_prefix = document_type.lower()
        is_active = str(form.get(f"{key_prefix}_is_active", "") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        printer_name = normalize_workstation_printer_name(
            form.get(f"{key_prefix}_printer_name")
        )

        if bool(row.is_active) != is_active:
            row.is_active = is_active
            changed = True
        if (row.printer_name or None) != (printer_name or None):
            row.printer_name = printer_name or None
            changed = True

    if changed:
        audit_log(
            db,
            request,
            action="QZ_WORKSTATION_UPDATE",
            entity_type="qz_workstation",
            entity_id=normalized_key,
            summary=f"Updated workstation printer mappings for {workstation_label or normalized_key}",
            details={
                "workstation_key": normalized_key,
                "workstation_label": workstation_label or None,
                "profiles": {
                    document_type: {
                        "is_active": bool(rows_by_document_type[document_type].is_active),
                        "printer_name": rows_by_document_type[document_type].printer_name,
                    }
                    for document_type in QZ_WORKSTATION_DOCUMENT_TYPES
                    if document_type in rows_by_document_type
                },
            },
        )
        db.commit()

    return RedirectResponse(
        url=_workstations_url(message="Workstation settings saved."),
        status_code=303,
    )
