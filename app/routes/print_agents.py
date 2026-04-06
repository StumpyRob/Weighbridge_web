from __future__ import annotations

import base64
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import PrintAgent, PrintDestination, PrintJob
from ..models.base import utcnow
from ..permissions import PERM_MANAGE_SETTINGS, require_permission
from ..services.print_agents import (
    PRINT_AGENT_STATUS_OFFLINE,
    authenticate_print_agent,
    complete_print_agent_pairing as complete_print_agent_pairing_session,
    create_print_agent_pairing,
    exchange_print_agent_pairing as exchange_print_agent_pairing_session,
    generate_print_agent_credentials,
    mark_print_agent_online,
    normalize_print_agent_printers,
    PrintAgentPairingError,
)
from ..services.printing import (
    DELIVERY_TYPE_PRINT_AGENT_PULL,
    PRINT_CONTENT_TYPE_PDF,
    PRINT_JOB_STATUS_FAILED,
    PRINT_JOB_STATUS_IN_PROGRESS,
    PRINT_JOB_STATUS_PENDING,
    PRINT_JOB_STATUS_SENT,
    resolve_job_document_filename,
    resolve_job_payload,
)
from ..tenancy import request_tenant_id, require_tenant, tenant_request_url


router = APIRouter(prefix="/api/print", tags=["print-agents"])
INLINE_PAYLOAD_MAX_BYTES = 32 * 1024


class PrintAgentRegisterRequest(BaseModel):
    name: str | None = None


class PrintAgentPairingRequest(BaseModel):
    name: str | None = None


class PrintAgentPairingCompleteRequest(BaseModel):
    pairing_code: str
    name: str | None = None


class PrintAgentPairingExchangeRequest(BaseModel):
    pairing_id: str
    exchange_token: str


class PrintJobCompleteRequest(BaseModel):
    provider_job_ref: str | None = None
    provider_response_json: dict[str, Any] | None = None


class PrintJobFailRequest(BaseModel):
    error: str
    provider_job_ref: str | None = None
    provider_response_json: dict[str, Any] | None = None


class PrintAgentPrinterSyncEntry(BaseModel):
    name: str
    is_default: bool | None = None
    is_online: bool | None = None


class PrintAgentPrinterSyncRequest(BaseModel):
    printers: list[PrintAgentPrinterSyncEntry] = Field(default_factory=list)


def _assigned_agent_id(destination: PrintDestination | None) -> str:
    if destination is None or not isinstance(destination.delivery_config, dict):
        return ""
    return str(destination.delivery_config.get("agent_id", "")).strip()


def _job_name(job: PrintJob) -> str:
    document_type = str(job.document_type or "").strip().upper() or "PRINT"
    return f"{document_type} print job {job.id}"


def _current_user_id(request: Request) -> int | None:
    current_user = getattr(getattr(request, "state", None), "current_user", None)
    user_id = getattr(current_user, "id", None)
    try:
        return int(user_id) if user_id is not None else None
    except (TypeError, ValueError):
        return None


def _normalized_agent_name(name: str | None) -> str | None:
    return str(name or "").strip() or None


def _require_authenticated_agent(request: Request, db: Session) -> PrintAgent:
    tenant_id = request_tenant_id(request)
    raw_key = str(request.headers.get("X-Agent-Key", "")).strip()
    if not raw_key:
        raise HTTPException(status_code=401, detail="X-Agent-Key header is required.")
    agent = authenticate_print_agent(db, raw_key)
    if agent is None or int(getattr(agent, "tenant_id", 0) or 0) != tenant_id:
        raise HTTPException(status_code=401, detail="Invalid agent key.")
    mark_print_agent_online(agent)
    return agent


def _job_delivery_config(job: PrintJob, destination: PrintDestination | None) -> dict[str, object]:
    snapshot = (
        dict(job.delivery_config_json)
        if isinstance(job.delivery_config_json, dict)
        else {}
    )
    live = (
        dict(destination.delivery_config)
        if destination is not None and isinstance(destination.delivery_config, dict)
        else {}
    )
    merged = dict(live)
    merged.update(snapshot)
    return merged


def _job_printer_settings(
    job: PrintJob,
    destination: PrintDestination | None,
) -> tuple[str, str, int]:
    config = _job_delivery_config(job, destination)
    printer_name = str(config.get("printer_name", "")).strip()
    printer_role = str(config.get("printer_role", "")).strip()
    copies_raw = config.get("copies", 1)
    try:
        copies = int(copies_raw)
    except (TypeError, ValueError):
        copies = 1
    if copies < 1:
        copies = 1
    return printer_name, printer_role, copies


def _load_pull_job_for_agent(
    db: Session,
    *,
    job_id: int,
    agent: PrintAgent,
) -> tuple[PrintJob, PrintDestination]:
    job = db.get(PrintJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Print job not found.")
    if str(job.delivery_type or "").strip().upper() != DELIVERY_TYPE_PRINT_AGENT_PULL:
        raise HTTPException(status_code=404, detail="Print job not found.")
    destination = db.get(PrintDestination, job.destination_id) if job.destination_id else None
    if destination is None:
        raise HTTPException(status_code=404, detail="Print job not found.")
    if str(destination.delivery_type or "").strip().upper() != DELIVERY_TYPE_PRINT_AGENT_PULL:
        raise HTTPException(status_code=404, detail="Print job not found.")
    if _assigned_agent_id(destination) != str(agent.id):
        raise HTTPException(status_code=404, detail="Print job not found.")
    return job, destination


def _provider_job_ref_from_payload(
    explicit_value: str | None,
    provider_response_json: dict[str, Any] | None,
) -> str | None:
    resolved = str(explicit_value or "").strip()
    if resolved:
        return resolved
    if isinstance(provider_response_json, dict):
        candidate = str(provider_response_json.get("provider_job_ref", "")).strip()
        if candidate:
            return candidate
    return None


def _job_payload_url(request: Request, job_id: int) -> str:
    return tenant_request_url(request, path=f"/api/print/jobs/{job_id}/payload")


def _inline_payload_base64(payload_bytes: bytes, payload_format: str) -> str | None:
    if str(payload_format or "").strip().upper() == PRINT_CONTENT_TYPE_PDF:
        return None
    if len(payload_bytes) > INLINE_PAYLOAD_MAX_BYTES:
        return None
    return base64.b64encode(payload_bytes).decode("ascii")


def _optional_text(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _job_contract(
    request: Request,
    db: Session,
    *,
    job: PrintJob,
    destination: PrintDestination | None,
) -> dict[str, object]:
    payload_bytes, payload_format, payload_mime_type = resolve_job_payload(job)
    printer_name, printer_role, copies = _job_printer_settings(job, destination)
    contract: dict[str, object] = {
        "job_id": job.id,
        "document_type": str(job.document_type or "").strip().upper(),
        "document_filename": resolve_job_document_filename(db, job),
        "job_name": _job_name(job),
        "copies": copies,
        "payload_format": payload_format,
        "payload_mime_type": payload_mime_type,
        "payload_url": _job_payload_url(request, int(job.id)),
    }
    inline_payload = _inline_payload_base64(payload_bytes, payload_format)
    if inline_payload:
        contract["payload_base64"] = inline_payload
    if printer_name:
        contract["printer_name"] = printer_name
    if printer_role:
        contract["printer_role"] = printer_role
    return contract


@router.post("/agents/register")
def register_print_agent(
    payload: PrintAgentRegisterRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    require_permission(request, PERM_MANAGE_SETTINGS)
    require_tenant(request)
    agent_id, raw_api_key, hashed_api_key = generate_print_agent_credentials()
    name = _normalized_agent_name(payload.name)
    agent = PrintAgent(
        id=agent_id,
        name=name,
        api_key=hashed_api_key,
        status=PRINT_AGENT_STATUS_OFFLINE,
        last_seen_at=None,
    )
    db.add(agent)
    db.commit()
    return {"agent_id": agent.id, "api_key": raw_api_key}


@router.post("/agents/pairing/request")
def request_print_agent_pairing(
    payload: PrintAgentPairingRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    require_tenant(request)
    pairing, pairing_code, exchange_token = create_print_agent_pairing(
        db,
        requested_name=_normalized_agent_name(payload.name),
    )
    db.commit()
    return {
        "pairing_id": pairing.id,
        "pairing_code": pairing_code,
        "exchange_token": exchange_token,
        "expires_at": pairing.expires_at,
        "status": pairing.status,
    }


@router.post("/agents/pairing/complete")
def complete_print_agent_pairing(
    payload: PrintAgentPairingCompleteRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    require_permission(request, PERM_MANAGE_SETTINGS)
    require_tenant(request)
    try:
        pairing, agent = complete_print_agent_pairing_session(
            db,
            pairing_code=payload.pairing_code,
            paired_by_user_id=_current_user_id(request),
            agent_name=_normalized_agent_name(payload.name),
        )
    except PrintAgentPairingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    db.commit()
    return {
        "ok": True,
        "pairing_id": pairing.id,
        "agent_id": agent.id,
        "status": pairing.status,
        "name": agent.name,
    }


@router.post("/agents/pairing/exchange")
def exchange_print_agent_pairing(
    payload: PrintAgentPairingExchangeRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    require_tenant(request)
    try:
        pairing, agent, raw_api_key = exchange_print_agent_pairing_session(
            db,
            pairing_id=payload.pairing_id,
            exchange_token=payload.exchange_token,
        )
    except PrintAgentPairingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    db.commit()
    return {
        "agent_id": agent.id,
        "api_key": raw_api_key,
        "status": pairing.status,
        "name": agent.name,
    }


@router.post("/agents/heartbeat")
def print_agent_heartbeat(
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    agent = _require_authenticated_agent(request, db)
    db.commit()
    return {"ok": True, "agent_id": agent.id, "status": agent.status}


@router.post("/agents/printers/sync")
def print_agent_sync_printers(
    payload: PrintAgentPrinterSyncRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    agent = _require_authenticated_agent(request, db)
    normalized_printers = normalize_print_agent_printers(
        [
            {
                "name": printer.name,
                "is_default": printer.is_default,
                "is_online": printer.is_online,
            }
            for printer in payload.printers
        ]
    )
    agent.printers_json = normalized_printers
    agent.printers_synced_at = utcnow()
    db.commit()
    return {
        "ok": True,
        "agent_id": agent.id,
        "printer_count": len(normalized_printers),
        "printers_synced_at": agent.printers_synced_at,
    }


@router.get("/agents/jobs/next")
def print_agent_next_job(
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    agent = _require_authenticated_agent(request, db)
    next_job: PrintJob | None = None
    next_destination: PrintDestination | None = None
    for job in db.execute(
        select(PrintJob)
        .where(
            PrintJob.status == PRINT_JOB_STATUS_PENDING,
            PrintJob.delivery_type == DELIVERY_TYPE_PRINT_AGENT_PULL,
        )
        .order_by(PrintJob.created_at.asc(), PrintJob.id.asc())
    ).scalars():
        destination = db.get(PrintDestination, job.destination_id) if job.destination_id else None
        if _assigned_agent_id(destination) == str(agent.id):
            next_job = job
            next_destination = destination
            break

    db.commit()
    if next_job is None:
        return {"job": None}

    return {"job": _job_contract(request, db, job=next_job, destination=next_destination)}


@router.post("/jobs/{job_id:int}/claim")
def print_agent_claim_job(
    job_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    agent = _require_authenticated_agent(request, db)
    job, destination = _load_pull_job_for_agent(db, job_id=job_id, agent=agent)
    if str(job.status or "").strip().upper() != PRINT_JOB_STATUS_PENDING:
        raise HTTPException(status_code=409, detail="Print job is not pending.")

    row_count = db.execute(
        update(PrintJob)
        .where(
            PrintJob.id == job.id,
            PrintJob.status == PRINT_JOB_STATUS_PENDING,
        )
        .values(
            status=PRINT_JOB_STATUS_IN_PROGRESS,
            agent_id=str(agent.id),
            attempt_count=int(job.attempt_count or 0) + 1,
            last_error=None,
        )
    ).rowcount
    if not row_count:
        db.rollback()
        raise HTTPException(status_code=409, detail="Print job has already been claimed.")

    db.commit()
    refreshed = db.get(PrintJob, job.id)
    refreshed_job = refreshed or job
    printer_name, printer_role, copies = _job_printer_settings(refreshed_job, destination)
    contract = _job_contract(request, db, job=refreshed_job, destination=destination)
    return {
        "ok": True,
        "job_id": refreshed_job.id,
        "status": str(refreshed_job.status or ""),
        "job": contract,
        "printer_name": printer_name,
        "printer_role": printer_role,
        "copies": copies,
    }


@router.get("/jobs/{job_id:int}/payload")
def print_agent_job_payload(
    job_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    agent = _require_authenticated_agent(request, db)
    job, destination = _load_pull_job_for_agent(db, job_id=job_id, agent=agent)
    if str(job.status or "").strip().upper() != PRINT_JOB_STATUS_IN_PROGRESS:
        raise HTTPException(status_code=409, detail="Print job has not been claimed.")
    if str(job.agent_id or "").strip() != str(agent.id):
        raise HTTPException(status_code=404, detail="Print job not found.")

    payload_bytes, payload_format, payload_mime_type = resolve_job_payload(job)
    printer_name, printer_role, copies = _job_printer_settings(job, destination)
    document_filename = resolve_job_document_filename(db, job)
    db.commit()
    headers = {
        "Content-Disposition": f'attachment; filename="{document_filename}"',
        "Cache-Control": "no-store",
        "X-Print-Job-Id": str(job.id),
        "X-Print-Document-Type": str(job.document_type or "").strip().upper(),
        "X-Print-Document-Filename": document_filename,
        "X-Print-Payload-Format": payload_format,
        "X-Print-Payload-Mime-Type": payload_mime_type,
        "X-Print-Copies": str(copies),
    }
    if printer_name:
        headers["X-Print-Printer-Name"] = printer_name
    if printer_role:
        headers["X-Print-Printer-Role"] = printer_role
    return Response(
        content=payload_bytes,
        media_type=payload_mime_type or "application/octet-stream",
        headers=headers,
    )


@router.post("/jobs/{job_id:int}/complete")
def print_agent_complete_job(
    job_id: int,
    payload: PrintJobCompleteRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    agent = _require_authenticated_agent(request, db)
    job, _destination = _load_pull_job_for_agent(db, job_id=job_id, agent=agent)
    if str(job.status or "").strip().upper() != PRINT_JOB_STATUS_IN_PROGRESS:
        raise HTTPException(status_code=409, detail="Print job is not in progress.")
    if str(job.agent_id or "").strip() != str(agent.id):
        raise HTTPException(status_code=404, detail="Print job not found.")

    job.provider_job_ref = _provider_job_ref_from_payload(
        payload.provider_job_ref,
        payload.provider_response_json,
    )
    job.provider_response_json = payload.provider_response_json
    job.last_error = None
    job.status = PRINT_JOB_STATUS_SENT
    job.sent_at = utcnow()
    db.commit()
    return {"ok": True, "job_id": job.id, "status": job.status}


@router.post("/jobs/{job_id:int}/fail")
def print_agent_fail_job(
    job_id: int,
    payload: PrintJobFailRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    agent = _require_authenticated_agent(request, db)
    job, _destination = _load_pull_job_for_agent(db, job_id=job_id, agent=agent)
    if str(job.status or "").strip().upper() != PRINT_JOB_STATUS_IN_PROGRESS:
        raise HTTPException(status_code=409, detail="Print job is not in progress.")
    if str(job.agent_id or "").strip() != str(agent.id):
        raise HTTPException(status_code=404, detail="Print job not found.")

    job.provider_job_ref = _provider_job_ref_from_payload(
        payload.provider_job_ref,
        payload.provider_response_json,
    )
    job.provider_response_json = payload.provider_response_json
    job.last_error = str(payload.error or "").strip() or "Agent delivery failed."
    job.status = PRINT_JOB_STATUS_FAILED
    job.sent_at = None
    db.commit()
    return {"ok": True, "job_id": job.id, "status": job.status}
