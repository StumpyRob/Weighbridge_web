from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
UK_TIMEZONE = ZoneInfo("Europe/London")
UK_TIMEZONE_LABEL = "Europe/London"


def _as_utc_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def uk_now_from_utc(value: datetime) -> datetime:
    return _as_utc_aware(value).astimezone(UK_TIMEZONE)


def uk_local_naive_from_utc(value: datetime) -> datetime:
    return uk_now_from_utc(value).replace(tzinfo=None)


def uk_date_from_utc(value: datetime) -> date:
    return uk_now_from_utc(value).date()


def format_uk_datetime(
    value: datetime | None,
    fmt: str = "%d/%m/%Y %H:%M",
) -> str:
    if value is None:
        return ""
    return uk_now_from_utc(value).strftime(fmt)


def format_uk_date(
    value: datetime | date | None,
    fmt: str = "%d/%m/%Y",
) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return uk_now_from_utc(value).strftime(fmt)
    return value.strftime(fmt)
