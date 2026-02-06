from __future__ import annotations

from decimal import Decimal

from .models.base import utcnow

from sqlalchemy import select, func

from .db import SessionLocal
from .models import Product, TaxRate, Unit
from .services.unit_rules import (
    canonical_weight_unit,
    is_allowed_weight_unit,
    normalize_unit_name,
)


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
        "code": "Zero (0%) \u2013 UK VAT",
        "legacy_codes": ["Zero (0%)"],
        "description": "UK VAT zero rate",
        "rate_percent": Decimal("0.00"),
    },
]


def seed_units() -> int:
    now = utcnow()
    created = 0
    dirty = False
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
                    dirty = True
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
            dirty = True
        if dirty:
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


def main() -> None:
    created_units = seed_units()
    created_tax_rates = seed_tax_rates()
    print(f"Seeded units: {created_units}")
    print(f"Seeded tax rates: {created_tax_rates}")


if __name__ == "__main__":
    main()
