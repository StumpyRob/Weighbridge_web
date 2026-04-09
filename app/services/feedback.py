from __future__ import annotations

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
FEEDBACK_STATUS_READ = "read"
FEEDBACK_STATUS_LABELS = {
    FEEDBACK_STATUS_NEW: "Unread",
    FEEDBACK_STATUS_READ: "Read",
}


def normalize_feedback_kind(value: object, *, default: str | None = None) -> str | None:
    normalized = str(value or "").strip().lower()
    if normalized in FEEDBACK_KINDS:
        return normalized
    return default


def feedback_kind_label(value: object) -> str:
    normalized = normalize_feedback_kind(value)
    if normalized is None:
        return "Feedback"
    return FEEDBACK_KIND_LABELS.get(normalized, "Feedback")


def feedback_status_label(value: object) -> str:
    normalized = str(value or "").strip().lower()
    return FEEDBACK_STATUS_LABELS.get(normalized, "Unread")


def feedback_display_title(feedback: UserFeedback) -> str:
    title = str(getattr(feedback, "title", "") or "").strip()
    if title:
        return title
    return f"{feedback_kind_label(getattr(feedback, 'kind', ''))} #{int(getattr(feedback, 'id', 0) or 0)}"


def feedback_unread_count(db: Session) -> int:
    return int(
        db.execute(
            select(func.count(UserFeedback.id)).where(UserFeedback.status == FEEDBACK_STATUS_NEW)
        ).scalar_one_or_none()
        or 0
    )
