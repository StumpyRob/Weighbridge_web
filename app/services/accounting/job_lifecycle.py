from __future__ import annotations

from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import AccountingSyncJob
from ...models.base import utcnow

ACCOUNTING_JOB_STATUS_PENDING: Final[str] = "pending"
ACCOUNTING_JOB_STATUS_RUNNING: Final[str] = "running"
ACCOUNTING_JOB_STATUS_FAILED: Final[str] = "failed"
ACCOUNTING_JOB_STATUS_MANUAL_REVIEW: Final[str] = "manual_review"
ACCOUNTING_JOB_STATUS_SUPERSEDED: Final[str] = "superseded"
ACCOUNTING_JOB_STATUS_SUCCEEDED: Final[str] = "succeeded"

_MANUAL_REVIEW_ERROR_FRAGMENTS: Final[tuple[str, ...]] = (
    "quickbooks invoice total does not match the local invoice gross total",
    "quickbooks invoice tax total does not match the local invoice tax total",
    "invoice gross total does not match its local invoice lines",
    "invoice tax total does not match its local invoice lines",
    "invoice net total does not match its local invoice lines",
    "duplicate document number",
    "duplicate doc number",
    "duplicate docnumber",
)

_SETUP_REQUIRED_ERROR_FRAGMENTS: Final[tuple[str, ...]] = (
    "no quickbooks tax mapping",
    "display code/label",
    "provider ref",
    "re-save this mapping",
    "no default revenue account is selected",
    "nominal code fallback",
    "income account with acctnum",
    "configured default quickbooks revenue account is invalid",
    "quickbooks connection is not active",
    "missing snapshotted tax rate data",
)


def _normalized_error_text(error_text: str | None) -> str:
    return str(error_text or "").strip().lower()


def job_requires_manual_review(error_text: str | None) -> bool:
    normalized_error = _normalized_error_text(error_text)
    return any(fragment in normalized_error for fragment in _MANUAL_REVIEW_ERROR_FRAGMENTS)


def job_requires_setup_fix(
    error_text: str | None,
    *,
    has_account_mismatch: bool = False,
) -> bool:
    if has_account_mismatch:
        return True
    normalized_error = _normalized_error_text(error_text)
    return any(fragment in normalized_error for fragment in _SETUP_REQUIRED_ERROR_FRAGMENTS)


def failed_job_status_for_error(error_text: str | None) -> str:
    if job_requires_manual_review(error_text):
        return ACCOUNTING_JOB_STATUS_MANUAL_REVIEW
    return ACCOUNTING_JOB_STATUS_FAILED


def newer_succeeded_job_exists(db: Session, job: AccountingSyncJob) -> bool:
    return (
        db.execute(
            select(AccountingSyncJob.id)
            .where(
                AccountingSyncJob.tenant_id == int(job.tenant_id),
                AccountingSyncJob.provider == str(job.provider or "").strip().lower(),
                AccountingSyncJob.job_type == str(job.job_type or "").strip(),
                AccountingSyncJob.entity_type == str(job.entity_type or "").strip(),
                AccountingSyncJob.entity_id == int(job.entity_id),
                AccountingSyncJob.status == ACCOUNTING_JOB_STATUS_SUCCEEDED,
                AccountingSyncJob.id > int(job.id),
            )
            .limit(1)
        )
        .scalar_one_or_none()
        is not None
    )


def mark_job_for_manual_review(job: AccountingSyncJob) -> bool:
    if str(job.status or "").strip().lower() == ACCOUNTING_JOB_STATUS_MANUAL_REVIEW:
        return False
    job.status = ACCOUNTING_JOB_STATUS_MANUAL_REVIEW
    job.updated_at = utcnow()
    return True


def mark_job_superseded(job: AccountingSyncJob) -> bool:
    if str(job.status or "").strip().lower() == ACCOUNTING_JOB_STATUS_SUPERSEDED:
        return False
    job.status = ACCOUNTING_JOB_STATUS_SUPERSEDED
    job.lock_token = None
    job.locked_at = None
    if job.finished_at is None:
        job.finished_at = utcnow()
    job.updated_at = utcnow()
    return True


def supersede_older_jobs_for_entity(
    db: Session,
    *,
    succeeded_job: AccountingSyncJob,
) -> list[AccountingSyncJob]:
    stale_jobs = list(
        db.execute(
            select(AccountingSyncJob)
            .where(
                AccountingSyncJob.tenant_id == int(succeeded_job.tenant_id),
                AccountingSyncJob.provider == str(succeeded_job.provider or "").strip().lower(),
                AccountingSyncJob.job_type == str(succeeded_job.job_type or "").strip(),
                AccountingSyncJob.entity_type == str(succeeded_job.entity_type or "").strip(),
                AccountingSyncJob.entity_id == int(succeeded_job.entity_id),
                AccountingSyncJob.id < int(succeeded_job.id),
                AccountingSyncJob.status.in_(
                    (
                        ACCOUNTING_JOB_STATUS_FAILED,
                        ACCOUNTING_JOB_STATUS_MANUAL_REVIEW,
                    )
                ),
            )
            .order_by(AccountingSyncJob.id.asc())
        ).scalars()
    )
    superseded: list[AccountingSyncJob] = []
    for job in stale_jobs:
        if mark_job_superseded(job):
            superseded.append(job)
    return superseded
