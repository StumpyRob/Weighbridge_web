from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import logging

from .models.base import utcnow

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import (
    PaymentMethod,
    Product,
    TaxRate,
    Unit,
    VehicleType,
    VoidReason,
)
from .services.unit_rules import (
    canonical_weight_unit,
    is_allowed_weight_unit,
    normalize_unit_name,
)

logger = logging.getLogger(__name__)

VOID_REASON_TYPE_TICKET = "TICKET"
VOID_REASON_TYPE_INVOICE = "INVOICE"


SEED_UNITS = [
    {"name": "kg", "unit_type": "WEIGHT"},
    {"name": "tonnes", "unit_type": "WEIGHT"},
    {"name": "Each", "unit_type": "COUNT"},
    {"name": "Load", "unit_type": "COUNT"},
]

SEED_TAX_RATES = [
    {
        "code": "Standard (20%) \u2013 UK VAT",
        "legacy_codes": ["Standard (20%)"],
        "description": "UK VAT standard rate",
        "rate_percent": Decimal("0.20"),
    },
    {
        "code": "Zero (0%)",
        "legacy_codes": ["Zero (0%) \u2013 UK VAT"],
        "description": "VAT zero rate",
        "rate_percent": Decimal("0.00"),
    },
]

SEED_TICKET_VOID_REASONS = [
    {"code": "Entered in error", "description": "Entered in error"},
    {"code": "Duplicate ticket", "description": "Duplicate ticket"},
    {"code": "Customer cancelled", "description": "Customer cancelled"},
    {"code": "Incorrect weights", "description": "Incorrect weights"},
    {"code": "Wrong customer", "description": "Wrong customer"},
    {"code": "Other", "description": "Other"},
    {"code": "System/test", "description": "System/test"},
]

SEED_INVOICE_VOID_REASONS = [
    {"code": "Entered in error", "description": "Entered in error"},
    {"code": "Customer cancelled", "description": "Customer cancelled"},
    {"code": "Duplicate invoice", "description": "Duplicate invoice"},
]

SEED_PAYMENT_METHODS = [
    {"code": "BACS", "description": "BACS"},
    {"code": "Card", "description": "Card"},
    {"code": "Cash", "description": "Cash"},
    {"code": "Cheque", "description": "Cheque"},
]

SEED_VEHICLE_TYPES = [
    {"code": "Car", "description": "Car"},
    {"code": "Van", "description": "Van"},
    {"code": "7.5T Rigid", "description": "7.5T Rigid"},
    {"code": "4 Wheeler", "description": "4 Wheeler"},
    {"code": "6 Wheeler", "description": "6 Wheeler"},
    {"code": "8 Wheeler", "description": "8 Wheeler"},
    {"code": "Artic", "description": "Artic"},
    {"code": "Tractor & Trailer", "description": "Tractor & Trailer"},
    {"code": "Plant", "description": "Plant"},
    {"code": "Other", "description": "Other"},
]


def seed_units() -> int:
    now = utcnow()
    created = 0
    with SessionLocal() as session:
        for entry in SEED_UNITS:
            entry_name = entry["name"]
            if entry.get("unit_type") == "WEIGHT":
                if not is_allowed_weight_unit(entry_name):
                    raise ValueError(
                        f"Invalid seeded WEIGHT unit: {entry_name}. Only kg/tonnes are allowed."
                    )
                canonical = canonical_weight_unit(entry_name)
                if canonical:
                    entry_name = canonical
            normalized = normalize_unit_name(entry_name)
            exists = session.execute(
                select(Unit).where(func.lower(Unit.name) == normalized)
            ).scalar_one_or_none()
            if exists:
                updated = False
                if entry.get("unit_type") and exists.unit_type != entry["unit_type"]:
                    exists.unit_type = entry["unit_type"]
                    updated = True
                if entry.get("unit_type") == "WEIGHT" and exists.name != entry_name:
                    exists.name = entry_name
                    updated = True
                if updated:
                    exists.updated_at = now
                continue
            session.add(
                Unit(
                    name=entry_name,
                    unit_type=entry.get("unit_type", "COUNT"),
                    is_active=True,
                    created_at=now,
                    updated_at=now,
                )
            )
            created += 1
        if created:
            session.commit()
    return created


def seed_tax_rates() -> int:
    now = utcnow()
    created = 0
    dirty = False

    with SessionLocal() as session:
        for entry in SEED_TAX_RATES:
            canonical_code = entry["code"]
            legacy_codes = list(entry.get("legacy_codes") or [])
            match_codes = [canonical_code] + legacy_codes
            matches = list(
                session.execute(
                    select(TaxRate).where(
                        func.lower(TaxRate.code).in_([code.lower() for code in match_codes])
                    )
                ).scalars()
            )
            exists = next(
                (
                    row
                    for row in matches
                    if row.code.strip().lower() == canonical_code.strip().lower()
                ),
                None,
            ) or (matches[0] if matches else None)

            if exists:
                for other in matches:
                    if other.id == exists.id:
                        continue
                    impacted_products = list(
                        session.execute(
                            select(Product).where(Product.tax_rate_id == other.id)
                        ).scalars()
                    )
                    for product in impacted_products:
                        product.tax_rate_id = exists.id
                        product.updated_at = now
                    if impacted_products:
                        dirty = True
                    session.delete(other)
                    dirty = True

                updated = False
                existing_rate = (
                    Decimal(str(exists.rate_percent))
                    if exists.rate_percent is not None
                    else None
                )
                if exists.code != canonical_code:
                    exists.code = canonical_code
                    updated = True
                if existing_rate != entry["rate_percent"]:
                    exists.rate_percent = entry["rate_percent"]
                    updated = True
                if exists.description != entry.get("description"):
                    exists.description = entry.get("description")
                    updated = True
                if not exists.is_active:
                    exists.is_active = True
                    updated = True
                if updated:
                    exists.updated_at = now
                    dirty = True
                continue

            session.add(
                TaxRate(
                    code=canonical_code,
                    description=entry.get("description"),
                    rate_percent=entry["rate_percent"],
                    is_active=True,
                    created_at=now,
                    updated_at=now,
                )
            )
            created += 1
            dirty = True

        if dirty:
            session.commit()

    return created


def _seed_code_lookup_rows(
    session: Session,
    model: type[VoidReason] | type[PaymentMethod] | type[VehicleType],
    seed_rows: list[dict[str, str]],
    now: datetime,
) -> tuple[int, bool]:
    created = 0
    dirty = False
    for entry in seed_rows:
        code = entry["code"].strip()
        description = (entry.get("description") or "").strip() or None
        exists = session.execute(
            select(model).where(func.lower(model.code) == code.lower())
        ).scalar_one_or_none()
        if exists:
            updated = False
            if exists.code != code:
                exists.code = code
                updated = True
            if exists.description != description:
                exists.description = description
                updated = True
            if not exists.is_active:
                exists.is_active = True
                updated = True
            if updated:
                exists.updated_at = now
                dirty = True
            continue

        session.add(
            model(
                code=code,
                description=description,
                is_active=True,
                created_at=now,
                updated_at=now,
            )
        )
        created += 1
        dirty = True
    return created, dirty


def _seed_void_reasons_for_type(
    session: Session,
    seed_rows: list[dict[str, str]],
    reason_type: str,
    now: datetime,
) -> tuple[int, bool]:
    created = 0
    dirty = False
    for entry in seed_rows:
        code = entry["code"].strip()
        description = (entry.get("description") or "").strip() or None
        exists = session.execute(
            select(VoidReason).where(
                func.lower(VoidReason.code) == code.lower(),
                func.upper(VoidReason.reason_type) == reason_type,
            )
        ).scalar_one_or_none()
        if exists:
            updated = False
            if exists.code != code:
                exists.code = code
                updated = True
            if exists.description != description:
                exists.description = description
                updated = True
            if (exists.reason_type or "").strip().upper() != reason_type:
                exists.reason_type = reason_type
                updated = True
            if not exists.is_active:
                exists.is_active = True
                updated = True
            if updated:
                exists.updated_at = now
                dirty = True
            continue

        session.add(
            VoidReason(
                code=code,
                reason_type=reason_type,
                description=description,
                is_active=True,
                created_at=now,
                updated_at=now,
            )
        )
        created += 1
        dirty = True
    return created, dirty


def seed_void_reasons(session: Session | None = None) -> int:
    now = utcnow()
    if session is None:
        with SessionLocal() as local_session:
            created, dirty = _seed_void_reasons_for_type(
                local_session,
                SEED_TICKET_VOID_REASONS,
                VOID_REASON_TYPE_TICKET,
                now,
            )
            if dirty:
                local_session.commit()
            return created

    created, dirty = _seed_void_reasons_for_type(
        session,
        SEED_TICKET_VOID_REASONS,
        VOID_REASON_TYPE_TICKET,
        now,
    )
    if dirty:
        session.commit()
    return created


def seed_invoice_void_reasons(session: Session | None = None) -> int:
    now = utcnow()
    if session is None:
        with SessionLocal() as local_session:
            created, dirty = _seed_void_reasons_for_type(
                local_session,
                SEED_INVOICE_VOID_REASONS,
                VOID_REASON_TYPE_INVOICE,
                now,
            )
            if dirty:
                local_session.commit()
            return created

    created, dirty = _seed_void_reasons_for_type(
        session,
        SEED_INVOICE_VOID_REASONS,
        VOID_REASON_TYPE_INVOICE,
        now,
    )
    if dirty:
        session.commit()
    return created


def seed_payment_methods(session: Session | None = None) -> int:
    now = utcnow()
    if session is None:
        with SessionLocal() as local_session:
            created, dirty = _seed_code_lookup_rows(
                local_session, PaymentMethod, SEED_PAYMENT_METHODS, now
            )
            if dirty:
                local_session.commit()
            return created

    created, dirty = _seed_code_lookup_rows(
        session, PaymentMethod, SEED_PAYMENT_METHODS, now
    )
    if dirty:
        session.commit()
    return created


def seed_vehicle_types(session: Session | None = None) -> int:
    now = utcnow()

    def seed_if_empty(target_session: Session) -> tuple[int, bool]:
        row_count = target_session.execute(select(func.count(VehicleType.id))).scalar_one()
        if row_count > 0:
            return 0, False
        return _seed_code_lookup_rows(
            target_session, VehicleType, SEED_VEHICLE_TYPES, now
        )

    if session is None:
        with SessionLocal() as local_session:
            created, dirty = seed_if_empty(local_session)
            if dirty:
                local_session.commit()
            return created

    created, dirty = seed_if_empty(session)
    if dirty:
        session.commit()
    return created


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    created_units = seed_units()
    created_tax_rates = seed_tax_rates()
    created_ticket_void_reasons = seed_void_reasons()
    created_invoice_void_reasons = seed_invoice_void_reasons()
    created_payment_methods = seed_payment_methods()
    created_vehicle_types = seed_vehicle_types()
    logger.info("Seeded units: %s", created_units)
    logger.info("Seeded tax rates: %s", created_tax_rates)
    logger.info("Seeded ticket void reasons: %s", created_ticket_void_reasons)
    logger.info("Seeded invoice void reasons: %s", created_invoice_void_reasons)
    logger.info("Seeded payment methods: %s", created_payment_methods)
    logger.info("Seeded vehicle types: %s", created_vehicle_types)


if __name__ == "__main__":
    main()
