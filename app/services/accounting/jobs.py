from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import AccountingConnection, AccountingSyncEvent, AccountingSyncJob
from ...models.base import utcnow
from .quickbooks_oauth import QUICKBOOKS_PROVIDER

logger = logging.getLogger(__name__)

_CONNECTED_STATUS = "connected"
_PENDING_JOB_STATUSES = ("pending", "running")


def get_active_accounting_connection(
    db: Session,
    tenant_id: int,
    *,
    provider: str = QUICKBOOKS_PROVIDER,
) -> AccountingConnection | None:
    resolved_tenant_id = int(tenant_id or 0)
    resolved_provider = str(provider or "").strip().lower()
    if resolved_tenant_id <= 0 or not resolved_provider:
        return None
    return (
        db.execute(
            select(AccountingConnection).where(
                AccountingConnection.tenant_id == resolved_tenant_id,
                AccountingConnection.provider == resolved_provider,
                AccountingConnection.status == _CONNECTED_STATUS,
            )
        )
        .scalars()
        .first()
    )


def log_accounting_event(
    db: Session,
    *,
    tenant_id: int,
    provider: str,
    event_type: str,
    entity_type: str | None,
    entity_id: int | None,
    direction: str,
    summary: str,
    detail_json: dict[str, Any] | None = None,
) -> AccountingSyncEvent:
    event = AccountingSyncEvent(
        tenant_id=int(tenant_id),
        provider=str(provider or "").strip().lower(),
        event_type=str(event_type or "").strip() or "job_enqueued",
        entity_type=str(entity_type or "").strip() or None,
        entity_id=int(entity_id) if entity_id is not None else None,
        direction=str(direction or "").strip().upper() or "OUTBOUND",
        summary=str(summary or "").strip() or "Accounting event",
        detail_json=detail_json,
    )
    db.add(event)
    return event


def _existing_pending_job(
    db: Session,
    *,
    tenant_id: int,
    provider: str,
    job_type: str,
    entity_type: str,
    entity_id: int,
) -> AccountingSyncJob | None:
    return (
        db.execute(
            select(AccountingSyncJob)
            .where(
                AccountingSyncJob.tenant_id == int(tenant_id),
                AccountingSyncJob.provider == str(provider or "").strip().lower(),
                AccountingSyncJob.job_type == str(job_type or "").strip(),
                AccountingSyncJob.entity_type == str(entity_type or "").strip(),
                AccountingSyncJob.entity_id == int(entity_id),
                AccountingSyncJob.status.in_(_PENDING_JOB_STATUSES),
            )
            .order_by(AccountingSyncJob.created_at.desc(), AccountingSyncJob.id.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )


def _enqueue_job(
    db: Session,
    *,
    tenant_id: int,
    provider: str,
    job_type: str,
    entity_type: str,
    entity_id: int,
    payload_json: dict[str, Any] | None,
    summary: str,
) -> AccountingSyncJob | None:
    connection = get_active_accounting_connection(
        db,
        int(tenant_id),
        provider=provider,
    )
    if connection is None:
        return None

    existing = _existing_pending_job(
        db,
        tenant_id=int(tenant_id),
        provider=provider,
        job_type=job_type,
        entity_type=entity_type,
        entity_id=int(entity_id),
    )
    if existing is not None:
        return existing

    job = AccountingSyncJob(
        tenant_id=int(tenant_id),
        provider=str(provider or "").strip().lower(),
        job_type=str(job_type or "").strip(),
        entity_type=str(entity_type or "").strip(),
        entity_id=int(entity_id),
        status="pending",
        attempts=0,
        available_at=utcnow(),
        payload_json=payload_json,
    )
    db.add(job)
    db.flush()
    log_accounting_event(
        db,
        tenant_id=int(tenant_id),
        provider=provider,
        event_type="job_enqueued",
        entity_type=entity_type,
        entity_id=int(entity_id),
        direction="OUTBOUND",
        summary=summary,
        detail_json={
            "job_id": int(job.id),
            "job_type": str(job.job_type),
            "status": str(job.status),
            "connection_id": int(connection.id),
        },
    )
    return job


def enqueue_sync_customer(
    db: Session,
    *,
    tenant_id: int,
    customer_id: int,
    provider: str = QUICKBOOKS_PROVIDER,
) -> AccountingSyncJob | None:
    return _enqueue_job(
        db,
        tenant_id=int(tenant_id),
        provider=provider,
        job_type="sync_customer",
        entity_type="customer",
        entity_id=int(customer_id),
        payload_json={"customer_id": int(customer_id)},
        summary="Queued accounting customer sync",
    )


def enqueue_sync_product(
    db: Session,
    *,
    tenant_id: int,
    product_id: int,
    provider: str = QUICKBOOKS_PROVIDER,
) -> AccountingSyncJob | None:
    return _enqueue_job(
        db,
        tenant_id=int(tenant_id),
        provider=provider,
        job_type="sync_product",
        entity_type="product",
        entity_id=int(product_id),
        payload_json={"product_id": int(product_id)},
        summary="Queued accounting product sync",
    )


def enqueue_sync_invoice(
    db: Session,
    *,
    tenant_id: int,
    invoice_id: int,
    provider: str = QUICKBOOKS_PROVIDER,
) -> AccountingSyncJob | None:
    return _enqueue_job(
        db,
        tenant_id=int(tenant_id),
        provider=provider,
        job_type="sync_invoice",
        entity_type="invoice",
        entity_id=int(invoice_id),
        payload_json={"invoice_id": int(invoice_id)},
        summary="Queued accounting invoice sync",
    )


def enqueue_mark_invoice_paid(
    db: Session,
    *,
    tenant_id: int,
    invoice_id: int,
    provider: str = QUICKBOOKS_PROVIDER,
) -> AccountingSyncJob | None:
    return _enqueue_job(
        db,
        tenant_id=int(tenant_id),
        provider=provider,
        job_type="mark_invoice_paid",
        entity_type="invoice",
        entity_id=int(invoice_id),
        payload_json={
            "invoice_id": int(invoice_id),
            "action": "mark_paid",
        },
        summary="Queued accounting invoice paid sync",
    )


def enqueue_void_invoice(
    db: Session,
    *,
    tenant_id: int,
    invoice_id: int,
    provider: str = QUICKBOOKS_PROVIDER,
) -> AccountingSyncJob | None:
    return _enqueue_job(
        db,
        tenant_id=int(tenant_id),
        provider=provider,
        job_type="void_invoice",
        entity_type="invoice",
        entity_id=int(invoice_id),
        payload_json={
            "invoice_id": int(invoice_id),
            "action": "void",
        },
        summary="Queued accounting invoice void sync",
    )


def commit_enqueued_accounting_job(
    db: Session,
    enqueue_func: Callable[..., AccountingSyncJob | None],
    /,
    **kwargs,
) -> AccountingSyncJob | None:
    try:
        job = enqueue_func(db, **kwargs)
        if job is None:
            return None
        db.commit()
        return job
    except Exception:
        db.rollback()
        logger.exception(
            "Accounting enqueue failed: %s",
            getattr(enqueue_func, "__name__", "unknown"),
        )
        return None
