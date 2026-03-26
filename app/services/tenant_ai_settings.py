from __future__ import annotations

from dataclasses import dataclass

from .platform_ai_settings import (
    DEFAULT_AI_MODEL,
    PlatformAISettingsState,
    SUPPORTED_ASSISTANT_MODELS,
)


@dataclass(frozen=True)
class TenantAISettingsResolution:
    ai_assistant_enabled: bool
    ai_model_override: str | None
    effective_ai_model: str
    dashboard_insights_override: bool | None
    dashboard_insights_enabled: bool


def resolve_assistant_model_override(
    candidate: str | None,
    default_model: str | None = None,
) -> str:
    fallback = str(default_model or "").strip()
    if fallback not in SUPPORTED_ASSISTANT_MODELS:
        fallback = DEFAULT_AI_MODEL
    normalized = str(candidate or "").strip()
    if normalized in SUPPORTED_ASSISTANT_MODELS:
        return normalized
    return fallback


def resolve_dashboard_insights_enabled(
    ai_assistant_enabled: bool,
    override: bool | None,
    default_enabled: bool,
) -> bool:
    if override is not None:
        return bool(override)
    return bool(ai_assistant_enabled) and bool(default_enabled)


def parse_dashboard_insights_override(value: object) -> bool | None:
    normalized = str(value or "").strip().lower()
    if not normalized or normalized in {"default", "platform_default"}:
        return None
    if normalized in {"enabled", "1", "true", "on", "yes"}:
        return True
    if normalized in {"disabled", "0", "false", "off", "no"}:
        return False
    raise ValueError("Select a valid dashboard insights override.")


def resolve_tenant_ai_settings(
    *,
    ai_assistant_enabled: bool,
    ai_model_override: str | None,
    dashboard_insights_override: bool | None,
    platform_settings: PlatformAISettingsState,
) -> TenantAISettingsResolution:
    return TenantAISettingsResolution(
        ai_assistant_enabled=bool(ai_assistant_enabled),
        ai_model_override=str(ai_model_override or "").strip() or None,
        effective_ai_model=resolve_assistant_model_override(
            ai_model_override,
            default_model=platform_settings.default_ai_model,
        ),
        dashboard_insights_override=dashboard_insights_override,
        dashboard_insights_enabled=resolve_dashboard_insights_enabled(
            bool(ai_assistant_enabled),
            dashboard_insights_override,
            platform_settings.ai_dashboard_insights_enabled,
        ),
    )


__all__ = [
    "TenantAISettingsResolution",
    "parse_dashboard_insights_override",
    "resolve_assistant_model_override",
    "resolve_dashboard_insights_enabled",
    "resolve_tenant_ai_settings",
]
