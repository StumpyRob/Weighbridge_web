from __future__ import annotations

import argparse
import csv
import os
import re
import sys

from sqlalchemy import select

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db import SessionLocal
from app.models import EwcCode
from app.models.base import utcnow


TRUE_VALUES = {"1", "true", "yes", "y", "t"}
FALSE_VALUES = {"0", "false", "no", "n", "f", ""}


def parse_bool(value: str | None) -> bool:
    normalized = str(value or "").strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise ValueError(f"Invalid boolean value: {value}")


def normalize_code(code_in: str) -> tuple[str, str] | None:
    digits = re.sub(r"\D", "", str(code_in))
    if not digits:
        return None
    if len(digits) > 6:
        raise ValueError(f"Invalid EWC code length: {code_in}")
    code_6 = digits.zfill(6)
    code_display = f"{code_6[0:2]} {code_6[2:4]} {code_6[4:6]}"
    return code_6, code_display


def import_codes(csv_path: str) -> tuple[int, int, int]:
    inserted = 0
    updated = 0
    retired = 0
    source_file = os.path.basename(csv_path)
    now = utcnow()

    with open(csv_path, "r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("CSV file has no headers.")
        field_map = {
            str(name).strip().lower(): name for name in reader.fieldnames
        }
        code_key = (
            field_map.get("code")
            or field_map.get("ewc_code")
            or field_map.get("ewc code")
        )
        desc_key = field_map.get("description") or field_map.get("desc")
        hazardous_key = field_map.get("hazardous")
        if not code_key or not desc_key or not hazardous_key:
            raise ValueError(
                "CSV headers must include code/ewc_code, description, hazardous."
            )

        seen_codes: set[str] = set()

        with SessionLocal() as session:
            existing_rows = session.execute(select(EwcCode)).scalars().all()
            existing = {row.code_6: row for row in existing_rows}

            for row in reader:
                code_in = row.get(code_key, "")
                desc = str(row.get(desc_key, "")).strip()
                hazardous_in = row.get(hazardous_key, "")
                normalized = normalize_code(code_in)
                if normalized is None:
                    continue
                code_6, code_display = normalized
                hazardous = parse_bool(hazardous_in)

                seen_codes.add(code_6)

                current = existing.get(code_6)
                if current:
                    current.description = desc
                    current.hazardous = hazardous
                    current.active = True
                    current.code_display = code_display
                    current.source_file = source_file
                    current.imported_at = now
                    updated += 1
                else:
                    session.add(
                        EwcCode(
                            code_6=code_6,
                            code_display=code_display,
                            description=desc,
                            hazardous=hazardous,
                            active=True,
                            source_file=source_file,
                            imported_at=now,
                        )
                    )
                    inserted += 1

            for row in existing_rows:
                if row.code_6 not in seen_codes and row.active:
                    row.active = False
                    row.source_file = source_file
                    row.imported_at = now
                    retired += 1

            session.commit()

    return inserted, updated, retired


def main() -> None:
    parser = argparse.ArgumentParser(description="Import EWC codes from CSV.")
    parser.add_argument(
        "csv_path",
        nargs="?",
        default=os.path.join(".", "data", "ewc_codes.csv"),
        help="Path to the EWC CSV file.",
    )
    args = parser.parse_args()

    csv_path = os.path.abspath(args.csv_path)
    if not os.path.exists(csv_path):
        raise SystemExit(f"CSV file not found: {csv_path}")

    inserted, updated, retired = import_codes(csv_path)
    print(f"Inserted: {inserted} | Updated: {updated} | Retired: {retired}")


if __name__ == "__main__":
    main()
