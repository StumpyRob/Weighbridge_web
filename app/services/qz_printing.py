from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..constants import DESC_MAX, NAME_MAX
from ..models import PrintDestination, WorkstationPrinterProfile

QZ_WORKSTATION_DOCUMENT_TYPES = (
    "TICKET",
    "WTN",
    "INVOICE",
)
QZ_WORKSTATION_KEY_MAX = 64


@dataclass(frozen=True)
class QzPrinterResolution:
    workstation_key: str
    workstation_label: str
    document_type: str
    printer_name: str
    printer_source: str

    @property
    def printer_display_name(self) -> str:
        return self.printer_name or "Default Printer"

    @property
    def workstation_named(self) -> bool:
        return bool(self.workstation_label)


def qz_printer_name_from_delivery_config(config: dict | None) -> str:
    if not isinstance(config, dict):
        return ""
    for key in ("qz_printer_name", "printer_name", "printer"):
        value = str(config.get(key, "") or "").strip()
        if value:
            return value[:DESC_MAX]
    return ""


def qz_direct_print_enabled_from_destination(
    *,
    delivery_type: str | None,
    delivery_config: dict | None,
    local_browser_delivery_type: str,
) -> bool:
    normalized_delivery_type = str(delivery_type or "").strip().upper()
    if normalized_delivery_type != str(local_browser_delivery_type or "").strip().upper():
        return False
    config = dict(delivery_config or {}) if isinstance(delivery_config, dict) else {}
    if "qz_direct_print_enabled" in config:
        return str(config.get("qz_direct_print_enabled", "") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    return bool(qz_printer_name_from_delivery_config(config))


def normalize_qz_document_type(value: str | None) -> str:
    normalized = str(value or "").strip().upper()
    if normalized in QZ_WORKSTATION_DOCUMENT_TYPES:
        return normalized
    return ""


def normalize_workstation_key(value: str | None) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > QZ_WORKSTATION_KEY_MAX:
        return ""
    return normalized


def normalize_workstation_label(value: str | None) -> str:
    normalized = " ".join(str(value or "").strip().split())
    if not normalized:
        return ""
    return normalized[:NAME_MAX]


def normalize_workstation_printer_name(value: str | None) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    return normalized[:DESC_MAX]


def qz_workstation_profile_rows(
    db: Session,
    *,
    workstation_key: str,
) -> list[WorkstationPrinterProfile]:
    normalized_key = normalize_workstation_key(workstation_key)
    if not normalized_key:
        return []
    return list(
        db.execute(
            select(WorkstationPrinterProfile)
            .where(WorkstationPrinterProfile.workstation_key == normalized_key)
            .order_by(
                WorkstationPrinterProfile.document_type.asc(),
                WorkstationPrinterProfile.id.asc(),
            )
        ).scalars()
    )


def ensure_workstation_profile_rows(
    db: Session,
    *,
    workstation_key: str,
    workstation_label: str | None = None,
) -> tuple[list[WorkstationPrinterProfile], bool]:
    normalized_key = normalize_workstation_key(workstation_key)
    if not normalized_key:
        raise ValueError("Workstation key is required.")

    normalized_label = normalize_workstation_label(workstation_label)
    rows = qz_workstation_profile_rows(db, workstation_key=normalized_key)
    rows_by_document_type = {
        str(row.document_type or "").strip().upper(): row
        for row in rows
    }
    changed = False

    for document_type in QZ_WORKSTATION_DOCUMENT_TYPES:
        row = rows_by_document_type.get(document_type)
        if row is None:
            row = WorkstationPrinterProfile(
                workstation_key=normalized_key,
                workstation_label=normalized_label or None,
                document_type=document_type,
                printer_name=None,
                is_active=False,
            )
            db.add(row)
            rows.append(row)
            rows_by_document_type[document_type] = row
            changed = True

    if normalized_label:
        for row in rows:
            if str(row.workstation_label or "").strip() != normalized_label:
                row.workstation_label = normalized_label
                changed = True

    if changed:
        db.flush()

    rows.sort(key=lambda row: str(row.document_type or "").strip().upper())
    return rows, changed


def set_workstation_label(
    db: Session,
    *,
    workstation_key: str,
    workstation_label: str | None,
) -> tuple[list[WorkstationPrinterProfile], bool]:
    rows, changed = ensure_workstation_profile_rows(
        db,
        workstation_key=workstation_key,
    )
    normalized_label = normalize_workstation_label(workstation_label)
    target_label = normalized_label or None
    for row in rows:
        if row.workstation_label != target_label:
            row.workstation_label = target_label
            changed = True
    if changed:
        db.flush()
    return rows, changed


def workstation_display_label(
    rows: list[WorkstationPrinterProfile] | tuple[WorkstationPrinterProfile, ...],
) -> str:
    for row in rows:
        label = normalize_workstation_label(getattr(row, "workstation_label", ""))
        if label:
            return label
    return ""


def default_qz_destination_for_document(
    db: Session,
    *,
    document_type: str,
) -> PrintDestination | None:
    normalized_document_type = normalize_qz_document_type(document_type)
    if not normalized_document_type:
        return None
    return (
        db.execute(
            select(PrintDestination)
            .where(
                PrintDestination.document_type == normalized_document_type,
                PrintDestination.is_default.is_(True),
                PrintDestination.is_active.is_(True),
            )
            .order_by(PrintDestination.id.asc())
            .limit(1)
        ).scalars().first()
    )


def resolve_qz_printer_for_workstation(
    db: Session,
    *,
    workstation_key: str,
    document_type: str,
    local_browser_delivery_type: str,
) -> QzPrinterResolution:
    normalized_document_type = normalize_qz_document_type(document_type)
    if not normalized_document_type:
        raise ValueError("Document type is required.")

    rows, _changed = ensure_workstation_profile_rows(
        db,
        workstation_key=workstation_key,
    )
    profile = next(
        (
            row
            for row in rows
            if str(row.document_type or "").strip().upper() == normalized_document_type
            and bool(row.is_active)
        ),
        None,
    )
    if profile is not None:
        printer_name = normalize_workstation_printer_name(profile.printer_name)
        return QzPrinterResolution(
            workstation_key=normalize_workstation_key(workstation_key),
            workstation_label=workstation_display_label(rows),
            document_type=normalized_document_type,
            printer_name=printer_name,
            printer_source="workstation" if printer_name else "workstation_default",
        )

    destination = default_qz_destination_for_document(
        db,
        document_type=normalized_document_type,
    )
    destination_printer_name = ""
    if destination is not None and qz_direct_print_enabled_from_destination(
        delivery_type=destination.delivery_type,
        delivery_config=(
            destination.delivery_config
            if isinstance(destination.delivery_config, dict)
            else {}
        ),
        local_browser_delivery_type=local_browser_delivery_type,
    ):
        destination_printer_name = qz_printer_name_from_delivery_config(
            destination.delivery_config
            if isinstance(destination.delivery_config, dict)
            else {}
        )

    return QzPrinterResolution(
        workstation_key=normalize_workstation_key(workstation_key),
        workstation_label=workstation_display_label(rows),
        document_type=normalized_document_type,
        printer_name=destination_printer_name,
        printer_source="destination" if destination_printer_name else "default",
    )
