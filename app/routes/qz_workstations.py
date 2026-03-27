from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..permissions import PERM_ACCESS_WORKSPACE, require_permission
from ..services.printing import DELIVERY_TYPE_PRINT_LOCAL_BROWSER
from ..services.qz_printing import (
    ensure_workstation_profile_rows,
    normalize_qz_document_type,
    normalize_workstation_key,
    normalize_workstation_label,
    resolve_qz_printer_for_workstation,
    set_workstation_label,
)
from ..tenancy import request_platform_mode

router = APIRouter()


def _require_tenant_workspace_user(request: Request) -> None:
    if request_platform_mode(request):
        raise HTTPException(status_code=404, detail="Not Found")
    require_permission(request, PERM_ACCESS_WORKSPACE)


class WorkstationRegistrationRequest(BaseModel):
    workstation_key: str
    workstation_label: str | None = None


class WorkstationResolveRequest(BaseModel):
    workstation_key: str
    document_type: str


class WorkstationLabelRequest(BaseModel):
    workstation_key: str
    workstation_label: str


@router.post("/printing/qz/workstation/register")
def register_qz_workstation(
    payload: WorkstationRegistrationRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    _require_tenant_workspace_user(request)
    workstation_key = normalize_workstation_key(payload.workstation_key)
    if not workstation_key:
        raise HTTPException(status_code=400, detail="Workstation key is required.")

    rows, changed = ensure_workstation_profile_rows(
        db,
        workstation_key=workstation_key,
        workstation_label=payload.workstation_label,
    )
    if changed:
        db.commit()

    workstation_label = normalize_workstation_label(payload.workstation_label)
    if not workstation_label:
        for row in rows:
            workstation_label = normalize_workstation_label(row.workstation_label)
            if workstation_label:
                break

    return {
        "workstation": {
            "key": workstation_key,
            "label": workstation_label,
            "named": bool(workstation_label),
        },
        "needs_workstation_name": not bool(workstation_label),
    }


@router.post("/printing/qz/workstation/label")
def update_qz_workstation_label(
    payload: WorkstationLabelRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    _require_tenant_workspace_user(request)
    workstation_key = normalize_workstation_key(payload.workstation_key)
    workstation_label = normalize_workstation_label(payload.workstation_label)
    if not workstation_key:
        raise HTTPException(status_code=400, detail="Workstation key is required.")
    if not workstation_label:
        raise HTTPException(status_code=400, detail="Workstation name is required.")

    _rows, changed = set_workstation_label(
        db,
        workstation_key=workstation_key,
        workstation_label=workstation_label,
    )
    if changed:
        db.commit()

    return {
        "workstation": {
            "key": workstation_key,
            "label": workstation_label,
            "named": True,
        },
        "needs_workstation_name": False,
    }


@router.post("/printing/qz/resolve")
def resolve_qz_workstation_printer(
    payload: WorkstationResolveRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    _require_tenant_workspace_user(request)
    workstation_key = normalize_workstation_key(payload.workstation_key)
    document_type = normalize_qz_document_type(payload.document_type)
    if not workstation_key:
        raise HTTPException(status_code=400, detail="Workstation key is required.")
    if not document_type:
        raise HTTPException(status_code=400, detail="Document type is required.")

    resolution = resolve_qz_printer_for_workstation(
        db,
        workstation_key=workstation_key,
        document_type=document_type,
        local_browser_delivery_type=DELIVERY_TYPE_PRINT_LOCAL_BROWSER,
    )
    db.commit()

    return {
        "workstation": {
            "key": resolution.workstation_key,
            "label": resolution.workstation_label,
            "named": resolution.workstation_named,
        },
        "printer": {
            "name": resolution.printer_name,
            "display_name": resolution.printer_display_name,
            "source": resolution.printer_source,
        },
        "needs_workstation_name": not resolution.workstation_named,
        "hint": f"Printing via QZ -> {resolution.printer_display_name}",
    }
