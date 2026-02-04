from __future__ import annotations

from .models.base import utcnow

from sqlalchemy import select, func

from .db import SessionLocal
from .models import Unit
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


def main() -> None:
    created = seed_units()
    print(f"Seeded units: {created}")


if __name__ == "__main__":
    main()
