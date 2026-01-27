from __future__ import annotations

from .models.base import utcnow

from sqlalchemy import select

from .db import SessionLocal
from .models import Unit


SEED_UNITS = [
    {"name": "Tonne"},
    {"name": "Kilogram"},
    {"name": "Load"},
]


def seed_units() -> int:
    now = utcnow()
    created = 0
    with SessionLocal() as session:
        for entry in SEED_UNITS:
            exists = session.execute(
                select(Unit).where(Unit.name == entry["name"])
            ).scalar_one_or_none()
            if exists:
                continue
            session.add(
                Unit(
                    name=entry["name"],
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
