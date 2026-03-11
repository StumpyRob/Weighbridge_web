from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import logging
from pathlib import Path

from .models.base import utcnow

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import (
    PaymentMethod,
    PrintDestination,
    PrintTemplate,
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
    {"name": "KG", "unit_type": "WEIGHT"},
    {"name": "Tonnes", "unit_type": "WEIGHT"},
    {"name": "m3", "unit_type": "COUNT"},
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

PRINT_DOCUMENT_TYPE_TICKET = "TICKET"
PRINT_DOCUMENT_TYPE_INVOICE = "INVOICE"
PRINT_DOCUMENT_TYPE_WTN = "WTN"
PRINT_DELIVERY_LOCAL_BROWSER = "PRINT_LOCAL_BROWSER"
PRINT_DELIVERY_NETWORK_RAW_9100 = "PRINT_NETWORK_RAW_9100"
PRINT_DELIVERY_NODE_HTTP = "PRINT_NODE_HTTP"
PRINT_DELIVERY_EMAIL_PDF = "EMAIL_PDF"


def _read_builtin_print_template(filename: str, fallback: str) -> str:
    candidate = Path(__file__).resolve().parent / "templates" / "print" / filename
    if not candidate.is_file():
        return fallback
    try:
        content = candidate.read_text(encoding="utf-8")
    except OSError:
        return fallback
    return content or fallback


SEED_PRINT_TEMPLATES = [
    {
        "code": "TICKET_THERMAL_SYSTEM",
        "legacy_codes": ("TICKET_DEFAULT",),
        "description": "Thermal Ticket (System)",
        "document_type": PRINT_DOCUMENT_TYPE_TICKET,
        "format": "TEXT",
        "is_system": True,
        "content": _read_builtin_print_template(
            "thermal_default.txt",
            "Ticket: {{ payload.ticket_no }}",
        ),
    },
    {
        "code": "TICKET_A4_SYSTEM",
        "description": "A4 Ticket (System)",
        "document_type": PRINT_DOCUMENT_TYPE_TICKET,
        "format": "HTML",
        "is_system": True,
        "content": _read_builtin_print_template(
            "a4_default.html",
            "<html><body><h1>Ticket {{ payload.ticket_no }}</h1></body></html>",
        ),
    },
    {
        "code": "INVOICE_SYSTEM",
        "legacy_codes": ("invoice_default", "invoice_a4_default", "inv_a4_standard"),
        "description": "Invoice (System)",
        "document_type": PRINT_DOCUMENT_TYPE_INVOICE,
        "format": "HTML",
        "is_system": True,
        "content": _read_builtin_print_template(
            "../invoices/pdf.html",
            "<html><body><h1>Invoice {{ payload.invoice_no }}</h1></body></html>",
        ),
    },
    {
        "code": "WTN_SYSTEM",
        "legacy_codes": ("WTN_DEFAULT",),
        "description": "Waste Transfer Note (System)",
        "document_type": PRINT_DOCUMENT_TYPE_WTN,
        "format": "HTML",
        "is_system": True,
        "content": _read_builtin_print_template(
            "wtn_default.html",
            "<html><body><h1>Waste Transfer Note</h1><p>Reference: {{ payload.wtn_no | default('-', true) }}</p></body></html>",
        ),
    },
]

SEED_PRINT_DESTINATIONS = [
    {
        "name": "Ticket Printer",
        "description": "Default ticket destination",
        "document_type": PRINT_DOCUMENT_TYPE_TICKET,
        "template_code": "TICKET_A4_SYSTEM",
        "delivery_type": PRINT_DELIVERY_LOCAL_BROWSER,
        "delivery_config": {},
        "is_default": True,
    },
    {
        "name": "Invoice Browser Print",
        "description": "Default invoice destination",
        "document_type": PRINT_DOCUMENT_TYPE_INVOICE,
        "template_code": "INVOICE_SYSTEM",
        "delivery_type": PRINT_DELIVERY_LOCAL_BROWSER,
        "delivery_config": {},
        "is_default": True,
    },
    {
        "name": "WTN Default",
        "description": "Default WTN destination",
        "document_type": PRINT_DOCUMENT_TYPE_WTN,
        "template_code": "WTN_SYSTEM",
        "delivery_type": PRINT_DELIVERY_LOCAL_BROWSER,
        "delivery_config": {},
        "is_default": True,
    },
]


def _seed_units_in_session(session: Session, now: datetime) -> tuple[int, bool]:
    created = 0
    dirty = False
    for entry in SEED_UNITS:
        entry_name = entry["name"]
        if entry.get("unit_type") == "WEIGHT":
            if not is_allowed_weight_unit(entry_name):
                raise ValueError(
                    f"Invalid seeded WEIGHT unit: {entry_name}. Only KG/Tonnes are allowed."
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
            if not bool(exists.is_active):
                exists.is_active = True
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
    return created, dirty


def seed_units(session: Session | None = None) -> int:
    now = utcnow()
    if session is None:
        with SessionLocal() as local_session:
            created, dirty = _seed_units_in_session(local_session, now)
            if dirty:
                local_session.commit()
            return created

    created, dirty = _seed_units_in_session(session, now)
    if dirty:
        session.commit()
    return created


def _seed_tax_rates_in_session(session: Session, now: datetime) -> tuple[int, bool]:
    created = 0
    dirty = False
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
    return created, dirty


def seed_tax_rates(session: Session | None = None) -> int:
    now = utcnow()
    if session is None:
        with SessionLocal() as local_session:
            created, dirty = _seed_tax_rates_in_session(local_session, now)
            if dirty:
                local_session.commit()
            return created

    created, dirty = _seed_tax_rates_in_session(session, now)
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


def seed_print_destinations(session: Session | None = None) -> int:
    now = utcnow()

    def seed_rows(target_session: Session) -> tuple[int, bool]:
        created = 0
        dirty = False
        for entry in SEED_PRINT_DESTINATIONS:
            name = entry["name"].strip()
            description = (entry.get("description") or "").strip() or None
            document_type = (entry.get("document_type") or "").strip().upper()
            template_code = (entry.get("template_code") or "").strip()
            template = None
            if template_code:
                template = target_session.execute(
                    select(PrintTemplate).where(
                        func.lower(PrintTemplate.code) == template_code.lower()
                    )
                ).scalar_one_or_none()
            if template is None:
                continue
            template_id = template.id
            delivery_type = (entry.get("delivery_type") or "").strip().upper()
            delivery_config = dict(entry.get("delivery_config") or {})
            is_default = bool(entry.get("is_default"))
            active_default = target_session.execute(
                select(PrintDestination).where(
                    PrintDestination.document_type == document_type,
                    PrintDestination.is_default.is_(True),
                    PrintDestination.is_active.is_(True),
                )
            ).scalar_one_or_none()
            exists = target_session.execute(
                select(PrintDestination).where(
                    func.lower(PrintDestination.name) == name.lower()
                )
            ).scalar_one_or_none()
            if exists:
                effective_is_default = is_default
                if (
                    is_default
                    and active_default is not None
                    and int(active_default.id) != int(exists.id)
                ):
                    # Respect existing defaults in configured environments.
                    effective_is_default = False
                updated = False
                if exists.name != name:
                    exists.name = name
                    updated = True
                if exists.description != description:
                    exists.description = description
                    updated = True
                if exists.document_type != document_type:
                    exists.document_type = document_type
                    updated = True
                if exists.template_id != template_id:
                    exists.template_id = template_id
                    updated = True
                if exists.delivery_type != delivery_type:
                    exists.delivery_type = delivery_type
                    updated = True
                if (exists.delivery_config or {}) != delivery_config:
                    exists.delivery_config = delivery_config
                    updated = True
                if bool(exists.is_default) != effective_is_default:
                    exists.is_default = effective_is_default
                    updated = True
                if not exists.is_active:
                    exists.is_active = True
                    updated = True
                if updated:
                    exists.updated_at = now
                    dirty = True
                continue

            if is_default and active_default is not None:
                # Default exists already for this document type; do not duplicate.
                continue

            target_session.add(
                PrintDestination(
                    name=name,
                    description=description,
                    document_type=document_type,
                    template_id=template_id,
                    delivery_type=delivery_type,
                    delivery_config=delivery_config,
                    is_default=is_default,
                    is_active=True,
                    created_at=now,
                    updated_at=now,
                )
            )
            created += 1
            dirty = True
        return created, dirty

    if session is None:
        with SessionLocal() as local_session:
            created, dirty = seed_rows(local_session)
            if dirty:
                local_session.commit()
            return created

    created, dirty = seed_rows(session)
    if dirty:
        session.commit()
    return created
def seed_print_templates(
    session: Session | None = None,
    *,
    force_system_content: bool = False,
) -> int:
    now = utcnow()

    def seed_rows(target_session: Session) -> tuple[int, bool]:
        created = 0
        dirty = False
        for entry in SEED_PRINT_TEMPLATES:
            code = entry["code"].strip()
            exists = target_session.execute(
                select(PrintTemplate).where(func.lower(PrintTemplate.code) == code.lower())
            ).scalar_one_or_none()
            if exists is None:
                legacy_codes = [
                    str(item).strip().lower()
                    for item in list(entry.get("legacy_codes") or [])
                    if str(item).strip()
                ]
                if legacy_codes:
                    exists = target_session.execute(
                        select(PrintTemplate).where(
                            func.lower(PrintTemplate.code).in_(legacy_codes)
                        )
                    ).scalars().first()
            if exists:
                updated = False
                if str(exists.code or "").strip() != code:
                    has_conflict = target_session.execute(
                        select(PrintTemplate.id).where(
                            func.lower(PrintTemplate.code) == code.lower(),
                            PrintTemplate.id != exists.id,
                        )
                    ).first()
                    if not has_conflict:
                        exists.code = code
                        updated = True
                for field in ("description", "document_type", "format", "is_system"):
                    incoming = entry.get(field)
                    if getattr(exists, field) != incoming:
                        setattr(exists, field, incoming)
                        updated = True
                incoming_content = entry.get("content")
                should_force_content = bool(entry.get("is_system")) and force_system_content
                if should_force_content:
                    if exists.content != incoming_content:
                        exists.content = incoming_content
                        updated = True
                elif not str(exists.content or "").strip():
                    exists.content = incoming_content
                    updated = True
                if not exists.is_active:
                    exists.is_active = True
                    updated = True
                if updated:
                    exists.updated_at = now
                    dirty = True
                continue
            target_session.add(
                PrintTemplate(
                    code=code,
                    description=entry.get("description"),
                    document_type=entry.get("document_type"),
                    format=entry.get("format"),
                    content=entry.get("content"),
                    is_system=bool(entry.get("is_system", False)),
                    is_active=True,
                    created_at=now,
                    updated_at=now,
                )
            )
            created += 1
            dirty = True
        return created, dirty

    if session is None:
        with SessionLocal() as local_session:
            created, dirty = seed_rows(local_session)
            if dirty:
                local_session.commit()
            return created

    created, dirty = seed_rows(session)
    if dirty:
        session.commit()
    return created


def force_refresh_system_print_templates(session: Session | None = None) -> int:
    """
    Upsert canonical SYSTEM templates and forcibly overwrite repo-managed content.
    """
    return seed_print_templates(session, force_system_content=True)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    created_units = seed_units()
    created_tax_rates = seed_tax_rates()
    created_ticket_void_reasons = seed_void_reasons()
    created_invoice_void_reasons = seed_invoice_void_reasons()
    created_payment_methods = seed_payment_methods()
    created_vehicle_types = seed_vehicle_types()
    created_print_templates = force_refresh_system_print_templates()
    created_print_destinations = seed_print_destinations()
    logger.info("Seeded units: %s", created_units)
    logger.info("Seeded tax rates: %s", created_tax_rates)
    logger.info("Seeded ticket void reasons: %s", created_ticket_void_reasons)
    logger.info("Seeded invoice void reasons: %s", created_invoice_void_reasons)
    logger.info("Seeded payment methods: %s", created_payment_methods)
    logger.info("Seeded vehicle types: %s", created_vehicle_types)
    logger.info("Seeded print templates: %s", created_print_templates)
    logger.info("Seeded print destinations: %s", created_print_destinations)


if __name__ == "__main__":
    main()
