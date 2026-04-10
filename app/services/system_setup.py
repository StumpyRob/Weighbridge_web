from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from sqlalchemy import select
from sqlalchemy import func
from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..models import (
    CompanySetting,
    Container,
    Destination,
    Driver,
    Haulier,
    PaymentMethod,
    PrintDestination,
    PrintTemplate,
    TaxRate,
    Unit,
    VehicleType,
    VoidReason,
    Yard,
)
from ..models.base import utcnow
from ..seed import (
    seed_invoice_void_reasons,
    seed_payment_methods,
    seed_tax_rates,
    seed_units,
    seed_vehicle_types,
    seed_void_reasons,
)
from .uploads import company_logo_storage_layout, uploads_root

DEFAULT_YARD_NAME = "Main Yard"
DEFAULT_YARD_CODE_PREFIX = "Y"
VOID_REASON_TYPE_TICKET = "TICKET"
VOID_REASON_TYPE_INVOICE = "INVOICE"
PRINT_DOCUMENT_TYPES = ("TICKET", "INVOICE", "WTN")
REQUIRED_LOOKUP_TABLES: tuple[tuple[str, str, Any], ...] = (
    ("Hauliers", "hauliers", Haulier),
    ("Drivers", "drivers", Driver),
    ("Containers", "containers", Container),
    ("Destinations", "destinations", Destination),
    ("Units", "units", Unit),
    ("Tax Rates", "tax_rates", TaxRate),
    ("Vehicle Types", "vehicle_types", VehicleType),
    ("Payment Methods", "payment_methods", PaymentMethod),
    ("Void Reasons", "void_reasons", VoidReason),
)


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


def seed_required_reference_data(db: Session) -> dict[str, int]:
    return {
        "units": int(seed_units(db) or 0),
        "tax_rates": int(seed_tax_rates(db) or 0),
        "vehicle_types": int(seed_vehicle_types(db) or 0),
        "payment_methods": int(seed_payment_methods(db) or 0),
        "ticket_void_reasons": int(seed_void_reasons(db) or 0),
        "invoice_void_reasons": int(seed_invoice_void_reasons(db) or 0),
    }


def required_lookup_counts(db: Session) -> dict[str, int]:
    def _count(stmt) -> int:
        try:
            return int(db.execute(stmt).scalar_one_or_none() or 0)
        except SQLAlchemyError:
            return 0

    return {
        "units": _count(select(func.count(Unit.id))),
        "tax_rates": _count(select(func.count(TaxRate.id))),
        "vehicle_types": _count(select(func.count(VehicleType.id))),
        "payment_methods": _count(select(func.count(PaymentMethod.id))),
        "ticket_void_reasons": _count(
            select(func.count(VoidReason.id)).where(
                func.upper(VoidReason.reason_type) == VOID_REASON_TYPE_TICKET
            )
        ),
        "invoice_void_reasons": _count(
            select(func.count(VoidReason.id)).where(
                func.upper(VoidReason.reason_type) == VOID_REASON_TYPE_INVOICE
            )
        ),
    }


def required_lookup_table_status(db: Session) -> dict[str, object]:
    bind = db.get_bind()
    inspector = inspect(bind)
    available_tables = set(inspector.get_table_names())
    rows: list[dict[str, object]] = []
    migrations_complete = True

    for label, table_name, model in REQUIRED_LOOKUP_TABLES:
        exists = table_name in available_tables
        row_count: int | None = None
        if exists:
            try:
                row_count = int(
                    db.execute(select(func.count()).select_from(model)).scalar_one_or_none() or 0
                )
            except Exception:
                migrations_complete = False
        else:
            migrations_complete = False

        rows.append(
            {
                "label": label,
                "table_name": table_name,
                "exists": exists,
                "row_count": row_count,
            }
        )

    return {
        "migrations_complete": migrations_complete,
        "rows": rows,
    }


def missing_required_lookup_messages(db: Session) -> list[str]:
    counts = required_lookup_counts(db)
    messages: list[str] = []
    if counts["units"] <= 0:
        messages.append("System not initialized: missing required lookups (units).")
    if counts["tax_rates"] <= 0:
        messages.append("System not initialized: missing required lookups (tax rates).")
    if counts["vehicle_types"] <= 0:
        messages.append(
            "System not initialized: missing required lookups (vehicle types)."
        )
    if counts["payment_methods"] <= 0:
        messages.append(
            "System not initialized: missing required lookups (payment methods)."
        )
    if counts["ticket_void_reasons"] <= 0:
        messages.append(
            "System not initialized: missing required lookups (ticket void reasons)."
        )
    if counts["invoice_void_reasons"] <= 0:
        messages.append(
            "System not initialized: missing required lookups (invoice void reasons)."
        )
    return messages


def print_defaults_exist(db: Session) -> bool:
    for document_type in PRINT_DOCUMENT_TYPES:
        destination = (
            db.execute(
                select(PrintDestination).where(
                    PrintDestination.document_type == document_type,
                    PrintDestination.is_default.is_(True),
                    PrintDestination.is_active.is_(True),
                )
            )
            .scalars()
            .first()
        )
        if destination is None:
            return False
        template = db.get(PrintTemplate, destination.template_id)
        if template is None or not bool(template.is_active):
            return False
    return True


def uploads_path_status() -> dict[str, object]:
    upload_dir = uploads_root()
    exists = upload_dir.is_dir()
    writable = _is_dir_writable(upload_dir)
    return {
        "path": str(upload_dir),
        "layout": company_logo_storage_layout(upload_dir),
        "exists": exists,
        "writable": writable,
    }


def _is_dir_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    try:
        with NamedTemporaryFile(dir=path, delete=True):
            return True
    except OSError:
        return False
