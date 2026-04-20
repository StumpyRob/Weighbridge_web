from __future__ import annotations

import logging
import secrets
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, update
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
from .job_lifecycle import (
    ACCOUNTING_JOB_STATUS_FAILED,
    ACCOUNTING_JOB_STATUS_MANUAL_REVIEW,
    ACCOUNTING_JOB_STATUS_PENDING,
    ACCOUNTING_JOB_STATUS_SUCCEEDED,
    failed_job_status_for_error,
    job_requires_manual_review,
    mark_job_for_manual_review,
    mark_job_superseded,
    newer_succeeded_job_exists,
    supersede_older_jobs_for_entity,
)
from .jobs import log_accounting_event
from .product_sync import sync_product_to_quickbooks
from .quickbooks_client import QuickBooksApiError
from .quickbooks_oauth import QUICKBOOKS_PROVIDER

logger = logging.getLogger(__name__)

_CLAIMABLE_PENDING_STATUSES = (ACCOUNTING_JOB_STATUS_PENDING,)
_CLAIMABLE_RETRY_STATUSES = (ACCOUNTING_JOB_STATUS_FAILED,)
_MAX_RETRY_DELAY_MINUTES = 60
_MIN_RETRY_DELAY_MINUTES = 5
_CLAIM_SCAN_LIMIT = 50


@dataclass(frozen=True)
class AccountingJobBatchResult:
    processed: int
    succeeded: int
    failed: int
    processed_job_types: dict[str, int]


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
    attempted_job_ids: set[int] | None = None,
) -> AccountingSyncJob | None:
    claimable_statuses = (
        _CLAIMABLE_RETRY_STATUSES if retry_failed else _CLAIMABLE_PENDING_STATUSES
    )
    now = utcnow()
    attempted_ids = attempted_job_ids or set()
    candidates = (
        db.execute(
            (
                select(AccountingSyncJob).where(
                    AccountingSyncJob.tenant_id == int(tenant_id),
                    AccountingSyncJob.provider == str(provider or "").strip().lower(),
                    AccountingSyncJob.status.in_(claimable_statuses),
                )
                if retry_failed
                else select(AccountingSyncJob).where(
                    AccountingSyncJob.tenant_id == int(tenant_id),
                    AccountingSyncJob.provider == str(provider or "").strip().lower(),
                    AccountingSyncJob.status.in_(claimable_statuses),
                    AccountingSyncJob.available_at <= now,
                )
            )
            .order_by(
                AccountingSyncJob.available_at.asc(),
                AccountingSyncJob.created_at.asc(),
                AccountingSyncJob.id.asc(),
            )
            .limit(_CLAIM_SCAN_LIMIT)
        )
        .scalars()
        .all()
    )

    candidate: AccountingSyncJob | None = None
    for queued_job in candidates:
        if int(getattr(queued_job, "id", 0) or 0) in attempted_ids:
            continue
        queued_status = str(queued_job.status or "").strip().lower()
        if retry_failed and queued_status == ACCOUNTING_JOB_STATUS_FAILED:
            if newer_succeeded_job_exists(db, queued_job):
                if mark_job_superseded(queued_job):
                    log_accounting_event(
                        db,
                        tenant_id=int(queued_job.tenant_id),
                        provider=str(queued_job.provider or ""),
                        event_type="job_superseded",
                        entity_type=str(queued_job.entity_type or ""),
                        entity_id=int(queued_job.entity_id),
                        direction="INTERNAL",
                        summary=f"Accounting job {queued_job.id} was superseded by a later success",
                        detail_json={
                            "job_id": int(queued_job.id),
                            "job_type": str(queued_job.job_type or ""),
                        },
                    )
                continue
            if job_requires_manual_review(queued_job.error_text):
                if mark_job_for_manual_review(queued_job):
                    log_accounting_event(
                        db,
                        tenant_id=int(queued_job.tenant_id),
                        provider=str(queued_job.provider or ""),
                        event_type="job_manual_review_required",
                        entity_type=str(queued_job.entity_type or ""),
                        entity_id=int(queued_job.entity_id),
                        direction="INTERNAL",
                        summary=f"Accounting job {queued_job.id} requires manual review",
                        detail_json={
                            "job_id": int(queued_job.id),
                            "job_type": str(queued_job.job_type or ""),
                        },
                    )
                continue
        candidate = queued_job
        break

    if candidate is None:
        db.commit()
        return None

    lock_token = secrets.token_hex(16)
    updated = db.execute(
        update(AccountingSyncJob)
        .where(
            AccountingSyncJob.id == int(candidate.id),
            AccountingSyncJob.status == str(candidate.status or "").strip().lower(),
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
        job.status = ACCOUNTING_JOB_STATUS_SUCCEEDED
        job.finished_at = utcnow()
        job.lock_token = None
        job.locked_at = None
        job.error_text = None
        superseded_jobs = supersede_older_jobs_for_entity(db, succeeded_job=job)
        for stale_job in superseded_jobs:
            log_accounting_event(
                db,
                tenant_id=int(stale_job.tenant_id),
                provider=str(stale_job.provider or ""),
                event_type="job_superseded",
                entity_type=str(stale_job.entity_type or ""),
                entity_id=int(stale_job.entity_id),
                direction="INTERNAL",
                summary=f"Accounting job {stale_job.id} was superseded by a later success",
                detail_json={
                    "job_id": int(stale_job.id),
                    "job_type": str(stale_job.job_type or ""),
                    "superseded_by_job_id": int(job.id),
                },
            )
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
        job.status = failed_job_status_for_error(message)
        job.finished_at = utcnow()
        job.lock_token = None
        job.locked_at = None
        job.error_text = message
        if job.status == ACCOUNTING_JOB_STATUS_FAILED:
            job.available_at = _retry_available_at(int(job.attempts or 0))
        detail_json = {
            "job_id": int(job.id),
            "job_type": str(job.job_type or ""),
            "job_status": str(job.status or ""),
        }
        if isinstance(exc, QuickBooksApiError):
            detail_json["provider_error"] = exc.detail_json
        log_accounting_event(
            db,
            tenant_id=int(job.tenant_id),
            provider=str(job.provider or ""),
            event_type=(
                "job_manual_review_required"
                if job.status == ACCOUNTING_JOB_STATUS_MANUAL_REVIEW
                else "job_failed"
            ),
            entity_type=str(job.entity_type or ""),
            entity_id=int(job.entity_id),
            direction="INTERNAL",
            summary=(
                f"Accounting job {job.id} requires manual review"
                if job.status == ACCOUNTING_JOB_STATUS_MANUAL_REVIEW
                else f"Accounting job {job.id} failed"
            ),
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
    processed_job_types: Counter[str] = Counter()
    attempted_job_ids: set[int] = set()

    for _ in range(batch_limit):
        job = claim_next_accounting_job(
            db,
            tenant_id=int(tenant_id),
            provider=provider,
            retry_failed=retry_failed,
            attempted_job_ids=attempted_job_ids,
        )
        if job is None:
            break
        attempted_job_ids.add(int(job.id))
        processed += 1
        processed_job_types[str(job.job_type or "").strip()] += 1
        completed_job = run_accounting_job(db, job)
        if str(completed_job.status or "").strip().lower() == "succeeded":
            succeeded += 1
        else:
            failed += 1

    return AccountingJobBatchResult(
        processed=processed,
        succeeded=succeeded,
        failed=failed,
        processed_job_types=dict(processed_job_types),
    )
