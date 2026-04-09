from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import UserFeedback

FEEDBACK_KIND_BUG = "bug"
FEEDBACK_KIND_WISH = "wish"
FEEDBACK_KINDS = (FEEDBACK_KIND_BUG, FEEDBACK_KIND_WISH)
FEEDBACK_KIND_LABELS = {
    FEEDBACK_KIND_BUG: "Bug report",
    FEEDBACK_KIND_WISH: "Feature request",
}

FEEDBACK_STATUS_NEW = "new"
FEEDBACK_STATUS_REVIEWED = "reviewed"
FEEDBACK_STATUS_CLOSED = "closed"
FEEDBACK_STATUSES = (
    FEEDBACK_STATUS_NEW,
    FEEDBACK_STATUS_REVIEWED,
    FEEDBACK_STATUS_CLOSED,
)
FEEDBACK_STATUS_LABELS = {
    FEEDBACK_STATUS_NEW: "New",
    FEEDBACK_STATUS_REVIEWED: "Reviewed",
    FEEDBACK_STATUS_CLOSED: "Closed",
}

FEEDBACK_EMAIL_STATUS_PENDING = "pending"
FEEDBACK_EMAIL_STATUS_SENT = "sent"
FEEDBACK_EMAIL_STATUS_FAILED = "failed"
FEEDBACK_EMAIL_STATUSES = (
    FEEDBACK_EMAIL_STATUS_PENDING,
    FEEDBACK_EMAIL_STATUS_SENT,
    FEEDBACK_EMAIL_STATUS_FAILED,
)
FEEDBACK_EMAIL_STATUS_LABELS = {
    FEEDBACK_EMAIL_STATUS_PENDING: "Pending",
    FEEDBACK_EMAIL_STATUS_SENT: "Sent",
    FEEDBACK_EMAIL_STATUS_FAILED: "Failed",
}


@dataclass(frozen=True)
class FeedbackSummaryItem:
    id: int
    title: str
    kind: str
    kind_label: str
    status: str
    status_label: str
    created_at: object


@dataclass(frozen=True)
class FeedbackSummary:
    total_count: int
    new_count: int
    reviewed_count: int
    closed_count: int
    recent_items: tuple[FeedbackSummaryItem, ...]


def normalize_feedback_kind(value: object, *, default: str | None = None) -> str | None:
    normalized = str(value or "").strip().lower()
    if normalized in FEEDBACK_KINDS:
        return normalized
    return default


def normalize_feedback_status(value: object, *, default: str | None = None) -> str | None:
    normalized = str(value or "").strip().lower()
    if normalized in FEEDBACK_STATUSES:
        return normalized
    return default


def normalize_feedback_email_status(
    value: object,
    *,
    default: str | None = None,
) -> str | None:
    normalized = str(value or "").strip().lower()
    if normalized in FEEDBACK_EMAIL_STATUSES:
        return normalized
    return default


def feedback_kind_label(value: object) -> str:
    normalized = normalize_feedback_kind(value)
    if normalized is None:
        return "Feedback"
    return FEEDBACK_KIND_LABELS.get(normalized, "Feedback")


def feedback_status_label(value: object) -> str:
    normalized = normalize_feedback_status(value)
    if normalized is None:
        return "Unknown"
    return FEEDBACK_STATUS_LABELS.get(normalized, "Unknown")


def feedback_email_status_label(value: object) -> str:
    normalized = normalize_feedback_email_status(value)
    if normalized is None:
        return "Unknown"
    return FEEDBACK_EMAIL_STATUS_LABELS.get(normalized, "Unknown")


def feedback_display_title(feedback: UserFeedback) -> str:
    title = str(getattr(feedback, "title", "") or "").strip()
    if title:
        return title
    source_title = str(getattr(feedback, "source_title", "") or "").split("|", 1)[0].strip()
    if source_title:
        return source_title
    source_path = str(getattr(feedback, "source_path", "") or "").strip()
    if source_path:
        return source_path
    return f"{feedback_kind_label(getattr(feedback, 'kind', ''))} #{int(getattr(feedback, 'id', 0) or 0)}"


def feedback_summary(db: Session, *, recent_limit: int = 3) -> FeedbackSummary:
    counts = {
        FEEDBACK_STATUS_NEW: 0,
        FEEDBACK_STATUS_REVIEWED: 0,
        FEEDBACK_STATUS_CLOSED: 0,
    }
    rows = db.execute(
        select(UserFeedback.status, func.count(UserFeedback.id))
        .group_by(UserFeedback.status)
    ).all()
    total_count = 0
    for status, count in rows:
        normalized = normalize_feedback_status(status)
        count_value = int(count or 0)
        total_count += count_value
        if normalized is not None:
            counts[normalized] = count_value

    recent_feedback = db.execute(
        select(UserFeedback)
        .order_by(UserFeedback.created_at.desc(), UserFeedback.id.desc())
        .limit(max(0, int(recent_limit)))
    ).scalars().all()
    recent_items = tuple(
        FeedbackSummaryItem(
            id=int(item.id),
            title=feedback_display_title(item),
            kind=str(item.kind or ""),
            kind_label=feedback_kind_label(item.kind),
            status=str(item.status or ""),
            status_label=feedback_status_label(item.status),
            created_at=item.created_at,
        )
        for item in recent_feedback
    )
    return FeedbackSummary(
        total_count=total_count,
        new_count=counts[FEEDBACK_STATUS_NEW],
        reviewed_count=counts[FEEDBACK_STATUS_REVIEWED],
        closed_count=counts[FEEDBACK_STATUS_CLOSED],
        recent_items=recent_items,
    )
