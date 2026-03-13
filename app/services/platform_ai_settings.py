from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import PlatformSetting

DEFAULT_AI_MODEL = "gpt-5-mini"
SUPPORTED_ASSISTANT_MODELS = (DEFAULT_AI_MODEL, "gpt-5")
SUPPORTED_ASSISTANT_RESPONSE_STYLES = ("concise", "balanced", "detailed")
SUPPORTED_ASSISTANT_FOCUS_AREAS = ("operations", "accounts", "mixed")
DEFAULT_AI_TEMPERATURE = 0.2
DEFAULT_AI_MAX_OUTPUT_TOKENS = 320
DEFAULT_AI_DASHBOARD_INSIGHTS_ENABLED = True
DEFAULT_AI_DASHBOARD_CACHE_TTL_SECONDS = 600
DEFAULT_ASSISTANT_REQUESTS_PER_USER_PER_HOUR = 30
DEFAULT_ASSISTANT_REQUESTS_PER_TENANT_PER_HOUR = 300
DEFAULT_DASHBOARD_INSIGHTS_MIN_REFRESH_SECONDS = 300
DEFAULT_DASHBOARD_INSIGHTS_MAX_PER_TENANT_PER_HOUR = 12
DEFAULT_AI_RESPONSE_STYLE = "concise"
DEFAULT_AI_FOCUS = "operations"
DEFAULT_AI_EXTRA_GLOBAL_INSTRUCTIONS = ""
AI_TEMPERATURE_MIN = 0.0
AI_TEMPERATURE_MAX = 1.0
AI_MAX_OUTPUT_TOKENS_MIN = 100
AI_MAX_OUTPUT_TOKENS_MAX = 800
AI_DASHBOARD_CACHE_TTL_MIN = 60
AI_DASHBOARD_CACHE_TTL_MAX = 3600
ASSISTANT_REQUESTS_PER_USER_PER_HOUR_MIN = 1
ASSISTANT_REQUESTS_PER_USER_PER_HOUR_MAX = 200
ASSISTANT_REQUESTS_PER_TENANT_PER_HOUR_MIN = 1
ASSISTANT_REQUESTS_PER_TENANT_PER_HOUR_MAX = 2000
DASHBOARD_INSIGHTS_MIN_REFRESH_SECONDS_MIN = 60
DASHBOARD_INSIGHTS_MIN_REFRESH_SECONDS_MAX = 3600
DASHBOARD_INSIGHTS_MAX_PER_TENANT_PER_HOUR_MIN = 1
DASHBOARD_INSIGHTS_MAX_PER_TENANT_PER_HOUR_MAX = 120
AI_EXTRA_GLOBAL_INSTRUCTIONS_MAX_CHARS = 500
AI_TUNING_AUDIT_FIELDS = (
    "default_ai_model",
    "ai_temperature",
    "ai_max_output_tokens",
    "ai_dashboard_insights_enabled",
    "ai_dashboard_cache_ttl_seconds",
    "assistant_requests_per_user_per_hour",
    "assistant_requests_per_tenant_per_hour",
    "dashboard_insights_min_refresh_seconds",
    "dashboard_insights_max_per_tenant_per_hour",
    "ai_default_response_style",
    "ai_default_focus",
    "ai_extra_global_instructions",
)


@dataclass(frozen=True)
class PlatformAISettingsState:
    default_ai_model: str = DEFAULT_AI_MODEL
    ai_temperature: float = DEFAULT_AI_TEMPERATURE
    ai_max_output_tokens: int = DEFAULT_AI_MAX_OUTPUT_TOKENS
    ai_dashboard_insights_enabled: bool = DEFAULT_AI_DASHBOARD_INSIGHTS_ENABLED
    ai_dashboard_cache_ttl_seconds: int = DEFAULT_AI_DASHBOARD_CACHE_TTL_SECONDS
    assistant_requests_per_user_per_hour: int = DEFAULT_ASSISTANT_REQUESTS_PER_USER_PER_HOUR
    assistant_requests_per_tenant_per_hour: int = DEFAULT_ASSISTANT_REQUESTS_PER_TENANT_PER_HOUR
    dashboard_insights_min_refresh_seconds: int = DEFAULT_DASHBOARD_INSIGHTS_MIN_REFRESH_SECONDS
    dashboard_insights_max_per_tenant_per_hour: int = DEFAULT_DASHBOARD_INSIGHTS_MAX_PER_TENANT_PER_HOUR
    ai_default_response_style: str = DEFAULT_AI_RESPONSE_STYLE
    ai_default_focus: str = DEFAULT_AI_FOCUS
    ai_extra_global_instructions: str = DEFAULT_AI_EXTRA_GLOBAL_INSTRUCTIONS

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def platform_ai_settings_defaults() -> PlatformAISettingsState:
    return PlatformAISettingsState()


def platform_ai_settings_snapshot(settings: PlatformAISettingsState) -> dict[str, object]:
    return settings.to_dict()


def _singleton_row(db: Session) -> PlatformSetting | None:
    return db.execute(
        select(PlatformSetting).order_by(PlatformSetting.id.asc()).limit(1)
    ).scalars().first()


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _coerce_choice(
    value: object,
    *,
    allowed: tuple[str, ...],
    default: str,
    label: str,
) -> str:
    normalized = _clean_text(value).lower()
    if not normalized:
        return default
    if normalized not in allowed:
        raise ValueError(f"Select a valid {label}.")
    return normalized


def _coerce_float(
    value: object,
    *,
    default: float,
    minimum: float,
    maximum: float,
    label: str,
) -> float:
    text = _clean_text(value)
    if not text:
        return default
    try:
        number = round(float(text), 2)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number.") from exc
    if number < minimum or number > maximum:
        raise ValueError(f"{label} must be between {minimum:.1f} and {maximum:.1f}.")
    return number


def _coerce_int(
    value: object,
    *,
    default: int,
    minimum: int,
    maximum: int,
    label: str,
) -> int:
    text = _clean_text(value)
    if not text:
        return default
    try:
        number = int(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a whole number.") from exc
    if number < minimum or number > maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}.")
    return number


def _coerce_bool(value: object, *, default: bool) -> bool:
    text = _clean_text(value).lower()
    if not text:
        return default
    return text in {"1", "true", "on", "yes"}


def _coerce_extra_global_instructions(value: object) -> str:
    normalized = " ".join(_clean_text(value).split())
    if not normalized:
        return DEFAULT_AI_EXTRA_GLOBAL_INSTRUCTIONS
    if len(normalized) > AI_EXTRA_GLOBAL_INSTRUCTIONS_MAX_CHARS:
        raise ValueError(
            f"Extra global instructions must be {AI_EXTRA_GLOBAL_INSTRUCTIONS_MAX_CHARS} characters or fewer."
        )
    return normalized


def _coerce_settings(values: dict[str, Any] | None = None) -> PlatformAISettingsState:
    source = values or {}
    defaults = PlatformAISettingsState()
    return PlatformAISettingsState(
        default_ai_model=_coerce_choice(
            source.get("default_ai_model"),
            allowed=SUPPORTED_ASSISTANT_MODELS,
            default=defaults.default_ai_model,
            label="default AI model",
        ),
        ai_temperature=_coerce_float(
            source.get("ai_temperature"),
            default=defaults.ai_temperature,
            minimum=AI_TEMPERATURE_MIN,
            maximum=AI_TEMPERATURE_MAX,
            label="Temperature",
        ),
        ai_max_output_tokens=_coerce_int(
            source.get("ai_max_output_tokens"),
            default=defaults.ai_max_output_tokens,
            minimum=AI_MAX_OUTPUT_TOKENS_MIN,
            maximum=AI_MAX_OUTPUT_TOKENS_MAX,
            label="Max output tokens",
        ),
        ai_dashboard_insights_enabled=_coerce_bool(
            source.get("ai_dashboard_insights_enabled"),
            default=defaults.ai_dashboard_insights_enabled,
        ),
        ai_dashboard_cache_ttl_seconds=_coerce_int(
            source.get("ai_dashboard_cache_ttl_seconds"),
            default=defaults.ai_dashboard_cache_ttl_seconds,
            minimum=AI_DASHBOARD_CACHE_TTL_MIN,
            maximum=AI_DASHBOARD_CACHE_TTL_MAX,
            label="Dashboard insights cache TTL",
        ),
        assistant_requests_per_user_per_hour=_coerce_int(
            source.get("assistant_requests_per_user_per_hour"),
            default=defaults.assistant_requests_per_user_per_hour,
            minimum=ASSISTANT_REQUESTS_PER_USER_PER_HOUR_MIN,
            maximum=ASSISTANT_REQUESTS_PER_USER_PER_HOUR_MAX,
            label="Assistant requests per user per hour",
        ),
        assistant_requests_per_tenant_per_hour=_coerce_int(
            source.get("assistant_requests_per_tenant_per_hour"),
            default=defaults.assistant_requests_per_tenant_per_hour,
            minimum=ASSISTANT_REQUESTS_PER_TENANT_PER_HOUR_MIN,
            maximum=ASSISTANT_REQUESTS_PER_TENANT_PER_HOUR_MAX,
            label="Assistant requests per tenant per hour",
        ),
        dashboard_insights_min_refresh_seconds=_coerce_int(
            source.get("dashboard_insights_min_refresh_seconds"),
            default=defaults.dashboard_insights_min_refresh_seconds,
            minimum=DASHBOARD_INSIGHTS_MIN_REFRESH_SECONDS_MIN,
            maximum=DASHBOARD_INSIGHTS_MIN_REFRESH_SECONDS_MAX,
            label="Dashboard insights minimum refresh",
        ),
        dashboard_insights_max_per_tenant_per_hour=_coerce_int(
            source.get("dashboard_insights_max_per_tenant_per_hour"),
            default=defaults.dashboard_insights_max_per_tenant_per_hour,
            minimum=DASHBOARD_INSIGHTS_MAX_PER_TENANT_PER_HOUR_MIN,
            maximum=DASHBOARD_INSIGHTS_MAX_PER_TENANT_PER_HOUR_MAX,
            label="Dashboard insights per tenant per hour",
        ),
        ai_default_response_style=_coerce_choice(
            source.get("ai_default_response_style"),
            allowed=SUPPORTED_ASSISTANT_RESPONSE_STYLES,
            default=defaults.ai_default_response_style,
            label="default response style",
        ),
        ai_default_focus=_coerce_choice(
            source.get("ai_default_focus"),
            allowed=SUPPORTED_ASSISTANT_FOCUS_AREAS,
            default=defaults.ai_default_focus,
            label="default assistant focus",
        ),
        ai_extra_global_instructions=_coerce_extra_global_instructions(
            source.get("ai_extra_global_instructions")
        ),
    )


def validate_platform_ai_settings(**values: object) -> PlatformAISettingsState:
    return _coerce_settings(values)


def get_platform_ai_settings(db: Session) -> PlatformAISettingsState:
    row = _singleton_row(db)
    values = {} if row is None else {field: getattr(row, field, None) for field in AI_TUNING_AUDIT_FIELDS}
    return _coerce_settings(values)


def save_platform_ai_settings(
    db: Session,
    settings: PlatformAISettingsState,
) -> PlatformAISettingsState:
    row = _singleton_row(db)
    if row is None:
        row = PlatformSetting()
        db.add(row)
    for field_name, value in settings.to_dict().items():
        setattr(row, field_name, value)
    db.flush()
    return get_platform_ai_settings(db)


def reset_platform_ai_settings(db: Session) -> PlatformAISettingsState:
    return save_platform_ai_settings(db, PlatformAISettingsState())
