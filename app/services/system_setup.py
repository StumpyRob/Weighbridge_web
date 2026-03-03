from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CompanySetting, Yard
from ..models.base import utcnow

DEFAULT_YARD_NAME = "Main Yard"
DEFAULT_YARD_CODE_PREFIX = "Y"


def get_company_setting(db: Session) -> CompanySetting | None:
    return (
        db.execute(select(CompanySetting).order_by(CompanySetting.id.asc()).limit(1))
        .scalars()
        .first()
    )


def _next_yard_code(db: Session) -> str:
    existing = {
        str(code or "").strip().upper()
        for code in db.execute(select(Yard.code)).scalars().all()
    }
    index = 1
    while True:
        candidate = f"{DEFAULT_YARD_CODE_PREFIX}{index}"
        if candidate not in existing:
            return candidate
        index += 1


def upsert_default_yard(db: Session, *, yard_name: str) -> Yard:
    normalized_name = str(yard_name or "").strip() or DEFAULT_YARD_NAME
    yard = db.execute(select(Yard).order_by(Yard.id.asc()).limit(1)).scalars().first()
    if yard is None:
        yard = Yard(
            code=_next_yard_code(db),
            description=normalized_name,
            is_active=True,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        db.add(yard)
        db.flush()
        return yard

    updated = False
    if str(yard.description or "").strip() != normalized_name:
        yard.description = normalized_name
        updated = True
    if not bool(yard.is_active):
        yard.is_active = True
        updated = True
    if updated:
        yard.updated_at = utcnow()
    return yard


def ensure_company_settings_row_exists(db: Session) -> CompanySetting:
    company = get_company_setting(db)
    if company is not None:
        return company
    company = CompanySetting(is_initialized=False, created_at=utcnow(), updated_at=utcnow())
    db.add(company)
    db.commit()
    db.refresh(company)
    return company
