from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import AIUsageLog
from ..models.base import utcnow
from .platform_ai_settings import PlatformAISettingsState

REQUEST_TYPE_ASSISTANT = "assistant"
REQUEST_TYPE_DASHBOARD_INSIGHTS = "dashboard_insights"

ERROR_TYPE_RATE_LIMIT_USER = "rate_limit_user"
ERROR_TYPE_RATE_LIMIT_TENANT = "rate_limit_tenant"
ERROR_TYPE_RATE_LIMIT_MIN_REFRESH = "rate_limit_min_refresh"
ERROR_TYPE_RATE_LIMIT_HOURLY = "rate_limit_hourly"
ERROR_TYPE_TIMEOUT = "timeout"
ERROR_TYPE_REQUEST_FAILED = "request_failed"
ERROR_TYPE_INVALID_RESPONSE = "invalid_response"
ERROR_TYPE_NOT_CONFIGURED = "not_configured"
ERROR_TYPE_PROVIDER_AUTH = "provider_auth"
ERROR_TYPE_PROVIDER_REQUEST = "provider_request"


@dataclass(frozen=True)
class AIRateLimitDecision:
    allowed: bool
    error_type: str | None = None


def log_ai_usage(
    db: Session,
    *,
    tenant_id: int,
    user_id: int | None,
    request_type: str,
    success: bool,
    error_type: str | None,
    counted_toward_limit: bool,
    occurred_at: datetime | None = None,
) -> AIUsageLog:
    record = AIUsageLog(
        tenant_id=int(tenant_id),
        user_id=int(user_id) if user_id is not None else None,
        occurred_at=occurred_at or utcnow(),
        request_type=str(request_type or "").strip().lower(),
        success=bool(success),
        error_type=str(error_type or "").strip().lower() or None,
        counted_toward_limit=bool(counted_toward_limit),
    )
    db.add(record)
    db.flush()
    return record


def _count_recent_requests(
    db: Session,
    *,
    tenant_id: int,
    request_type: str,
    since: datetime,
    user_id: int | None = None,
) -> int:
    statement = select(func.count(AIUsageLog.id)).where(
        AIUsageLog.tenant_id == int(tenant_id),
        AIUsageLog.request_type == str(request_type or "").strip().lower(),
        AIUsageLog.counted_toward_limit.is_(True),
        AIUsageLog.occurred_at >= since,
    )
    if user_id is not None:
        statement = statement.where(AIUsageLog.user_id == int(user_id))
    return int(db.execute(statement).scalar_one() or 0)


def _latest_counted_request_time(
    db: Session,
    *,
    tenant_id: int,
    request_type: str,
) -> datetime | None:
    return db.execute(
        select(func.max(AIUsageLog.occurred_at)).where(
            AIUsageLog.tenant_id == int(tenant_id),
            AIUsageLog.request_type == str(request_type or "").strip().lower(),
            AIUsageLog.counted_toward_limit.is_(True),
        )
    ).scalar_one_or_none()


def check_assistant_rate_limit(
    db: Session,
    *,
    tenant_id: int,
    user_id: int,
    platform_settings: PlatformAISettingsState,
    now: datetime | None = None,
) -> AIRateLimitDecision:
    current_time = now or utcnow()
    window_start = current_time - timedelta(hours=1)
    user_count = _count_recent_requests(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        request_type=REQUEST_TYPE_ASSISTANT,
        since=window_start,
    )
    if user_count >= platform_settings.assistant_requests_per_user_per_hour:
        return AIRateLimitDecision(False, ERROR_TYPE_RATE_LIMIT_USER)

    tenant_count = _count_recent_requests(
        db,
        tenant_id=tenant_id,
        request_type=REQUEST_TYPE_ASSISTANT,
        since=window_start,
    )
    if tenant_count >= platform_settings.assistant_requests_per_tenant_per_hour:
        return AIRateLimitDecision(False, ERROR_TYPE_RATE_LIMIT_TENANT)

    return AIRateLimitDecision(True)


def check_dashboard_rate_limit(
    db: Session,
    *,
    tenant_id: int,
    platform_settings: PlatformAISettingsState,
    now: datetime | None = None,
) -> AIRateLimitDecision:
    current_time = now or utcnow()
    last_request_time = _latest_counted_request_time(
        db,
        tenant_id=tenant_id,
        request_type=REQUEST_TYPE_DASHBOARD_INSIGHTS,
    )
    if last_request_time is not None:
        seconds_since_last_request = (current_time - last_request_time).total_seconds()
        if seconds_since_last_request < platform_settings.dashboard_insights_min_refresh_seconds:
            return AIRateLimitDecision(False, ERROR_TYPE_RATE_LIMIT_MIN_REFRESH)

    window_start = current_time - timedelta(hours=1)
    tenant_count = _count_recent_requests(
        db,
        tenant_id=tenant_id,
        request_type=REQUEST_TYPE_DASHBOARD_INSIGHTS,
        since=window_start,
    )
    if tenant_count >= platform_settings.dashboard_insights_max_per_tenant_per_hour:
        return AIRateLimitDecision(False, ERROR_TYPE_RATE_LIMIT_HOURLY)

    return AIRateLimitDecision(True)
__all__ = [
    "AIRateLimitDecision",
    "ERROR_TYPE_INVALID_RESPONSE",
    "ERROR_TYPE_NOT_CONFIGURED",
    "ERROR_TYPE_PROVIDER_AUTH",
    "ERROR_TYPE_PROVIDER_REQUEST",
    "ERROR_TYPE_RATE_LIMIT_HOURLY",
    "ERROR_TYPE_RATE_LIMIT_MIN_REFRESH",
    "ERROR_TYPE_RATE_LIMIT_TENANT",
    "ERROR_TYPE_RATE_LIMIT_USER",
    "ERROR_TYPE_REQUEST_FAILED",
    "ERROR_TYPE_TIMEOUT",
    "REQUEST_TYPE_ASSISTANT",
    "REQUEST_TYPE_DASHBOARD_INSIGHTS",
    "check_assistant_rate_limit",
    "check_dashboard_rate_limit",
    "log_ai_usage",
]
