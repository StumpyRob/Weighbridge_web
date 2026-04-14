from __future__ import annotations

from datetime import datetime


ROW_VERSION_FIELD = "row_version"
STALE_EDIT_MESSAGE = (
    "This record was updated in another tab or session. Refresh the page and review "
    "the latest values before saving again."
)


def row_version_token(record: object | None) -> str:
    if record is None:
        return ""
    candidate = getattr(record, "updated_at", None) or getattr(record, "created_at", None)
    if not isinstance(candidate, datetime):
        return ""
    return candidate.replace(tzinfo=None).isoformat(timespec="microseconds")


def row_version_conflict(record: object | None, submitted_value: object) -> bool:
    submitted = str(submitted_value or "").strip()
    current = row_version_token(record)
    if not submitted or not current:
        return False
    return submitted != current
