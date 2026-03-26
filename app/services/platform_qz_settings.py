from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import PlatformSetting
from ..models.base import utcnow

PLATFORM_QZ_AUDIT_FIELDS = ("qz_enabled",)
QZ_VALIDATION_STATUS_NOT_RUN = "not_run"
QZ_VALIDATION_STATUS_OK = "ok"
QZ_VALIDATION_STATUS_ERROR = "error"


@dataclass(frozen=True)
class PlatformQzSettingsState:
    qz_enabled: bool = True
    qz_last_validated_at: datetime | None = None
    qz_last_validation_status: str = QZ_VALIDATION_STATUS_NOT_RUN
    qz_last_validation_summary: str = ""

    @property
    def validation_has_run(self) -> bool:
        return self.qz_last_validated_at is not None

    @property
    def validation_ok(self) -> bool:
        return self.qz_last_validation_status == QZ_VALIDATION_STATUS_OK

    def snapshot(self) -> dict[str, object]:
        return {
            "qz_enabled": self.qz_enabled,
        }


def _platform_setting_row(db: Session) -> PlatformSetting | None:
    return db.execute(
        select(PlatformSetting).order_by(PlatformSetting.id.asc()).limit(1)
    ).scalars().first()


def _coerce_bool(value: object, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if not normalized:
        return default
    return normalized in {"1", "true", "yes", "on"}


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def get_platform_qz_settings(db: Session) -> PlatformQzSettingsState:
    row = _platform_setting_row(db)
    if row is None:
        return PlatformQzSettingsState()
    status = _clean_text(getattr(row, "qz_last_validation_status", None)).lower()
    if status not in {
        QZ_VALIDATION_STATUS_NOT_RUN,
        QZ_VALIDATION_STATUS_OK,
        QZ_VALIDATION_STATUS_ERROR,
    }:
        status = QZ_VALIDATION_STATUS_NOT_RUN
    return PlatformQzSettingsState(
        qz_enabled=_coerce_bool(getattr(row, "qz_enabled", None), default=True),
        qz_last_validated_at=getattr(row, "qz_last_validated_at", None),
        qz_last_validation_status=status or QZ_VALIDATION_STATUS_NOT_RUN,
        qz_last_validation_summary=_clean_text(
            getattr(row, "qz_last_validation_summary", None)
        ),
    )


def save_platform_qz_settings(
    db: Session,
    settings_state: PlatformQzSettingsState,
) -> PlatformQzSettingsState:
    row = _platform_setting_row(db)
    if row is None:
        row = PlatformSetting()
        db.add(row)
    row.qz_enabled = bool(settings_state.qz_enabled)
    db.flush()
    return get_platform_qz_settings(db)


def record_platform_qz_validation(
    db: Session,
    *,
    ok: bool,
    summary: str,
) -> PlatformQzSettingsState:
    row = _platform_setting_row(db)
    if row is None:
        row = PlatformSetting()
        db.add(row)
    row.qz_last_validated_at = utcnow()
    row.qz_last_validation_status = (
        QZ_VALIDATION_STATUS_OK if ok else QZ_VALIDATION_STATUS_ERROR
    )
    row.qz_last_validation_summary = _clean_text(summary) or (
        "QZ validation passed." if ok else "QZ validation failed."
    )
    db.flush()
    return get_platform_qz_settings(db)


def platform_qz_settings_snapshot(
    settings_state: PlatformQzSettingsState,
) -> dict[str, object]:
    return settings_state.snapshot()


def platform_qz_ready_for_tenants(db: Session) -> bool:
    from .qz_signing import build_qz_signing_diagnostics

    settings_state = get_platform_qz_settings(db)
    diagnostics = build_qz_signing_diagnostics(enabled=bool(settings_state.qz_enabled))
    return bool(diagnostics.ready_for_tenants)
