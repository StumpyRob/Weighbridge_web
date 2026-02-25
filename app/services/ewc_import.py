from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from sqlalchemy import select
from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import EwcCode, EwcImportLog
from ..models.base import utcnow

TRUE_VALUES = {"1", "true", "yes", "y", "t", "on"}
FALSE_VALUES = {"0", "false", "no", "n", "f", "off", ""}

CODE_COLUMN_ALIASES = (
    "code",
    "ewc_code",
    "ewc code",
    "code_6",
    "list of waste code",
    "list of wastes code",
    "low code",
)
DESCRIPTION_COLUMN_ALIASES = (
    "description",
    "desc",
    "waste description",
    "entry description",
    "entry",
)
HAZARDOUS_COLUMN_ALIASES = (
    "hazardous",
    "hazardous entry",
    "hazardous_flag",
    "is_hazardous",
)


@dataclass(slots=True)
class ImportErrorDetail:
    row: int
    reason: str


@dataclass(slots=True)
class ImportResult:
    source_file: str
    replace_mode: bool
    total_rows: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    deactivated: int = 0
    errors: list[ImportErrorDetail] = field(default_factory=list)
    fatal_error: str | None = None

    @property
    def error_count(self) -> int:
        return len(self.errors)

    @property
    def ok(self) -> bool:
        return self.fatal_error is None


def _normalize_code(code_in: str) -> tuple[str, str] | None:
    raw = str(code_in or "").strip()
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    if len(digits) > 6:
        raise ValueError(f"Invalid EWC code length: {code_in}")
    code_6 = digits.zfill(6)
    code_display = f"{code_6[0:2]} {code_6[2:4]} {code_6[4:6]}"
    return code_6, code_display


def _parse_bool(value: str | None, *, default: bool = False) -> bool:
    normalized = str(value or "").strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    return default


def _resolve_column(
    field_map: dict[str, str],
    aliases: tuple[str, ...],
) -> str | None:
    for alias in aliases:
        if alias in field_map:
            return field_map[alias]
    return None


def _read_csv_text(csv_file: IO[str] | Path) -> tuple[str, str]:
    if isinstance(csv_file, Path):
        return csv_file.read_text(encoding="utf-8-sig"), csv_file.name

    source_name = str(getattr(csv_file, "name", "upload.csv") or "upload.csv")
    raw = csv_file.read()
    if isinstance(raw, bytes):
        return raw.decode("utf-8-sig"), Path(source_name).name
    return str(raw), Path(source_name).name


def _log_result(
    db: Session,
    *,
    result: ImportResult,
    imported_by: str | None,
) -> None:
    try:
        inspector = inspect(db.get_bind())
        if "ewc_import_logs" not in set(inspector.get_table_names()):
            return
    except SQLAlchemyError:
        return

    error_payload = [
        {"row": int(item.row), "reason": item.reason}
        for item in result.errors[:100]
    ]
    try:
        db.add(
            EwcImportLog(
                source_file=result.source_file,
                replace_mode=bool(result.replace_mode),
                total_rows=int(result.total_rows),
                inserted_count=int(result.inserted),
                updated_count=int(result.updated),
                unchanged_count=int(result.unchanged),
                skipped_count=int(result.skipped),
                deactivated_count=int(result.deactivated),
                error_count=int(result.error_count),
                errors_json=json.dumps(error_payload),
                imported_by=imported_by,
                imported_at=utcnow(),
            )
        )
    except SQLAlchemyError:
        return


def import_ewc_codes(
    csv_file: IO[str] | Path,
    *,
    replace: bool = False,
    db: Session | None = None,
    imported_by: str | None = None,
    source_name: str | None = None,
) -> ImportResult:
    text, detected_source_name = _read_csv_text(csv_file)
    resolved_source_name = str(source_name or detected_source_name or "upload.csv")
    result = ImportResult(source_file=resolved_source_name, replace_mode=bool(replace))

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        result.fatal_error = "CSV file has no headers."
        return result

    field_map = {
        str(name).strip().lower(): str(name)
        for name in reader.fieldnames
        if str(name).strip()
    }
    code_key = _resolve_column(field_map, CODE_COLUMN_ALIASES)
    description_key = _resolve_column(field_map, DESCRIPTION_COLUMN_ALIASES)
    hazardous_key = _resolve_column(field_map, HAZARDOUS_COLUMN_ALIASES)
    if not code_key or not description_key:
        result.fatal_error = (
            "CSV headers must include a code column and a description column."
        )
        return result

    own_session = False
    if db is None:
        db = SessionLocal()
        own_session = True

    try:
        existing_rows = db.execute(select(EwcCode)).scalars().all()
        existing_by_code = {str(row.code_6): row for row in existing_rows}
        seen_codes: set[str] = set()

        for row_index, row in enumerate(reader, start=2):
            result.total_rows += 1
            raw_code = str((row or {}).get(code_key, "") or "").strip()
            raw_description = str((row or {}).get(description_key, "") or "").strip()
            if not raw_code and not raw_description:
                result.skipped += 1
                continue

            try:
                normalized = _normalize_code(raw_code)
            except ValueError as exc:
                result.errors.append(ImportErrorDetail(row=row_index, reason=str(exc)))
                continue
            if normalized is None:
                result.skipped += 1
                continue

            code_6, code_display = normalized
            if not raw_description:
                result.errors.append(
                    ImportErrorDetail(
                        row=row_index,
                        reason=f"Missing description for code {code_display}.",
                    )
                )
                continue

            default_hazardous = "*" in raw_code
            hazardous_raw = (row or {}).get(hazardous_key, "") if hazardous_key else ""
            hazardous = _parse_bool(hazardous_raw, default=default_hazardous)

            seen_codes.add(code_6)
            current = existing_by_code.get(code_6)
            if current is None:
                db.add(
                    EwcCode(
                        code_6=code_6,
                        code_display=code_display,
                        description=raw_description,
                        hazardous=hazardous,
                        active=True,
                        source_file=resolved_source_name,
                        imported_at=utcnow(),
                    )
                )
                result.inserted += 1
                continue

            changed = False
            if str(current.code_display or "") != code_display:
                current.code_display = code_display
                changed = True
            if str(current.description or "") != raw_description:
                current.description = raw_description
                changed = True
            if bool(current.hazardous) != bool(hazardous):
                current.hazardous = bool(hazardous)
                changed = True
            if not bool(current.active):
                current.active = True
                changed = True

            if changed:
                current.source_file = resolved_source_name
                current.imported_at = utcnow()
                result.updated += 1
            else:
                result.unchanged += 1

        if replace:
            for existing in existing_rows:
                if existing.code_6 in seen_codes:
                    continue
                if bool(existing.active):
                    existing.active = False
                    existing.source_file = resolved_source_name
                    existing.imported_at = utcnow()
                    result.deactivated += 1

        _log_result(db, result=result, imported_by=imported_by)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        if own_session:
            db.close()


def parse_import_errors_json(raw: str | None) -> list[ImportErrorDetail]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    errors: list[ImportErrorDetail] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        row: Any = item.get("row")
        reason: Any = item.get("reason")
        try:
            row_number = int(row)
        except (TypeError, ValueError):
            continue
        reason_text = str(reason or "").strip()
        if not reason_text:
            continue
        errors.append(ImportErrorDetail(row=row_number, reason=reason_text))
    return errors
