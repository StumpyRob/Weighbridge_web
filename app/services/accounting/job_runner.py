from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from ...models import AccountingSyncJob
from ...models.base import utcnow
from .customer_sync import sync_customer_to_quickbooks
from .invoice_sync import (
    mark_invoice_sync_failed,
    sync_invoice_payment_to_quickbooks,
    sync_invoice_to_quickbooks,
    sync_invoice_void_to_quickbooks,
)
from .jobs import log_accounting_event
from .product_sync import sync_product_to_quickbooks
from .quickbooks_client import QuickBooksApiError
from .quickbooks_oauth import QUICKBOOKS_PROVIDER

logger = logging.getLogger(__name__)

_CLAIMABLE_PENDING_STATUSES = ("pending",)
_CLAIMABLE_RETRY_STATUSES = ("pending", "failed")
_MAX_RETRY_DELAY_MINUTES = 60
_MIN_RETRY_DELAY_MINUTES = 5


@dataclass(frozen=True)
class AccountingJobBatchResult:
    processed: int
    succeeded: int
    failed: int


def _retry_available_at(attempts: int) -> object:
    delay_minutes = min(
        _MAX_RETRY_DELAY_MINUTES,
        max(_MIN_RETRY_DELAY_MINUTES, int(attempts or 0) * _MIN_RETRY_DELAY_MINUTES),
    )
    return utcnow() + timedelta(minutes=delay_minutes)


def claim_next_accounting_job(
    db: Session,
    *,
    tenant_id: int,
    provider: str = QUICKBOOKS_PROVIDER,
    retry_failed: bool = False,
) -> AccountingSyncJob | None:
    claimable_statuses = (
        _CLAIMABLE_RETRY_STATUSES if retry_failed else _CLAIMABLE_PENDING_STATUSES
    )
    now = utcnow()
    availability_filter = AccountingSyncJob.available_at <= now
    if retry_failed:
        availability_filter = or_(
            AccountingSyncJob.status == "failed",
            AccountingSyncJob.available_at <= now,
        )
    candidate = (
        db.execute(
            select(AccountingSyncJob)
            .where(
                AccountingSyncJob.tenant_id == int(tenant_id),
                AccountingSyncJob.provider == str(provider or "").strip().lower(),
                AccountingSyncJob.status.in_(claimable_statuses),
                availability_filter,
            )
            .order_by(
                AccountingSyncJob.available_at.asc(),
                AccountingSyncJob.created_at.asc(),
                AccountingSyncJob.id.asc(),
            )
            .limit(1)
        )
        .scalars()
        .first()
    )
    if candidate is None:
        return None

    lock_token = secrets.token_hex(16)
    updated = db.execute(
        update(AccountingSyncJob)
        .where(
            AccountingSyncJob.id == int(candidate.id),
            AccountingSyncJob.status.in_(claimable_statuses),
        )
        .values(
            status="running",
            attempts=AccountingSyncJob.attempts + 1,
            started_at=now,
            locked_at=now,
            lock_token=lock_token,
            error_text=None,
        )
    )
    if int(getattr(updated, "rowcount", 0) or 0) != 1:
        db.rollback()
        return None

    job = db.get(AccountingSyncJob, int(candidate.id))
    if job is None:
        db.rollback()
        return None

    log_accounting_event(
        db,
        tenant_id=int(job.tenant_id),
        provider=str(job.provider or ""),
        event_type="job_claimed",
        entity_type=str(job.entity_type or ""),
        entity_id=int(job.entity_id),
        direction="INTERNAL",
        summary=f"Claimed accounting job {job.id}",
        detail_json={
            "job_id": int(job.id),
            "job_type": str(job.job_type or ""),
            "attempts": int(job.attempts or 0),
        },
    )
    db.commit()
    return job


def _dispatch_accounting_job(db: Session, job: AccountingSyncJob) -> dict:
    if job.job_type == "sync_customer":
        return sync_customer_to_quickbooks(
            db,
            tenant_id=int(job.tenant_id),
            customer_id=int(job.entity_id),
            provider=str(job.provider or ""),
        )
    if job.job_type == "sync_product":
        return sync_product_to_quickbooks(
            db,
            tenant_id=int(job.tenant_id),
            product_id=int(job.entity_id),
            provider=str(job.provider or ""),
        )
    if job.job_type == "sync_invoice":
        return sync_invoice_to_quickbooks(
            db,
            tenant_id=int(job.tenant_id),
            invoice_id=int(job.entity_id),
            provider=str(job.provider or ""),
        )
    if job.job_type == "mark_invoice_paid":
        return sync_invoice_payment_to_quickbooks(
            db,
            tenant_id=int(job.tenant_id),
            invoice_id=int(job.entity_id),
            provider=str(job.provider or ""),
        )
    if job.job_type == "void_invoice":
        return sync_invoice_void_to_quickbooks(
            db,
            tenant_id=int(job.tenant_id),
            invoice_id=int(job.entity_id),
            provider=str(job.provider or ""),
        )
    raise QuickBooksApiError(f"Unsupported accounting job type: {job.job_type}")


def run_accounting_job(
    db: Session,
    job: AccountingSyncJob,
) -> AccountingSyncJob:
    try:
        result = _dispatch_accounting_job(db, job)
        job.status = "succeeded"
        job.finished_at = utcnow()
        job.lock_token = None
        job.locked_at = None
        job.error_text = None
        log_accounting_event(
            db,
            tenant_id=int(job.tenant_id),
            provider=str(job.provider or ""),
            event_type="job_succeeded",
            entity_type=str(job.entity_type or ""),
            entity_id=int(job.entity_id),
            direction="INTERNAL",
            summary=f"Accounting job {job.id} succeeded",
            detail_json={
                "job_id": int(job.id),
                "job_type": str(job.job_type or ""),
                "result": result,
            },
        )
        db.commit()
        return job
    except Exception as exc:
        message = str(exc) or "Accounting sync failed."
        if str(job.entity_type or "").strip().lower() == "invoice":
            mark_invoice_sync_failed(
                db,
                tenant_id=int(job.tenant_id),
                invoice_id=int(job.entity_id),
                message=message,
                provider=str(job.provider or ""),
            )
        job.status = "failed"
        job.finished_at = utcnow()
        job.lock_token = None
        job.locked_at = None
        job.error_text = message
        job.available_at = _retry_available_at(int(job.attempts or 0))
        detail_json = {
            "job_id": int(job.id),
            "job_type": str(job.job_type or ""),
        }
        if isinstance(exc, QuickBooksApiError):
            detail_json["provider_error"] = exc.detail_json
        log_accounting_event(
            db,
            tenant_id=int(job.tenant_id),
            provider=str(job.provider or ""),
            event_type="job_failed",
            entity_type=str(job.entity_type or ""),
            entity_id=int(job.entity_id),
            direction="INTERNAL",
            summary=f"Accounting job {job.id} failed",
            detail_json=detail_json,
        )
        db.commit()
        logger.warning("Accounting job %s failed: %s", job.id, message)
        return job


def process_pending_accounting_jobs(
    db: Session,
    *,
    tenant_id: int,
    provider: str = QUICKBOOKS_PROVIDER,
    limit: int = 5,
    retry_failed: bool = False,
) -> AccountingJobBatchResult:
    processed = 0
    succeeded = 0
    failed = 0
    batch_limit = max(1, min(int(limit or 0), 20))

    for _ in range(batch_limit):
        job = claim_next_accounting_job(
            db,
            tenant_id=int(tenant_id),
            provider=provider,
            retry_failed=retry_failed,
        )
        if job is None:
            break
        processed += 1
        completed_job = run_accounting_job(db, job)
        if str(completed_job.status or "").strip().lower() == "succeeded":
            succeeded += 1
        else:
            failed += 1

    return AccountingJobBatchResult(
        processed=processed,
        succeeded=succeeded,
        failed=failed,
    )
