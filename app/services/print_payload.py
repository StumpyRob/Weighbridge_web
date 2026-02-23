from __future__ import annotations

import base64
from datetime import datetime
from decimal import Decimal, InvalidOperation
import mimetypes
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    Area,
    CompanySetting,
    Container,
    Customer,
    Destination,
    Driver,
    Haulier,
    Product,
    Ticket,
    Vehicle,
    WasteCode,
    Yard,
)


def _enum_value(value: Any) -> str:
    if value is None:
        return ""
    return value.value if hasattr(value, "value") else str(value)


def _format_dt(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.strftime("%d/%m/%Y %H:%M")


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    decimal_value = _to_decimal(value)
    return float(decimal_value) if decimal_value is not None else None


def _format_kg(value: Any) -> str:
    decimal_value = _to_decimal(value)
    if decimal_value is None:
        return "-"
    return f"{decimal_value:,.3f} kg"


def _format_qty(value: Any) -> str:
    decimal_value = _to_decimal(value)
    if decimal_value is None:
        return "-"
    return f"{decimal_value:,.3f}"


def _format_money(value: Any) -> str:
    decimal_value = _to_decimal(value)
    if decimal_value is None:
        return "-"
    return f"£{decimal_value:,.2f}"


def _company_logo_src(db: Session) -> str:
    company = (
        db.execute(select(CompanySetting).order_by(CompanySetting.id.asc()).limit(1))
        .scalars()
        .first()
    )
    logo_path = _company_logo_path(company)
    if not logo_path:
        return ""
    if logo_path.startswith("data:"):
        return logo_path
    if logo_path.startswith(("http://", "https://")):
        return logo_path

    data_uri = _company_logo_data_uri(logo_path)
    if data_uri:
        return data_uri
    if logo_path.startswith("/"):
        return logo_path
    return ""


def _company_logo_path(company: CompanySetting | None) -> str:
    if company is None:
        return ""
    current = str(company.company_logo_path or "").strip()
    if current:
        return current
    legacy_url = str(company.logo_url or "").strip()
    if legacy_url:
        return legacy_url
    legacy_file = str(company.logo_file_path or "").strip().lstrip("/")
    if legacy_file:
        return f"/media/{legacy_file}"
    return ""


def _company_logo_data_uri(logo_path: str) -> str:
    source = _resolve_logo_file_path(logo_path)
    if source is None or not source.is_file():
        return ""
    try:
        payload = source.read_bytes()
    except OSError:
        return ""
    if not payload:
        return ""
    mime = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _resolve_logo_file_path(logo_path: str) -> Path | None:
    normalized = str(logo_path or "").strip()
    if not normalized:
        return None

    if normalized.startswith("/static/uploads/company/"):
        filename = Path(normalized).name
        if not filename:
            return None
        for upload_root in _logo_upload_root_candidates():
            candidate = (upload_root / filename).resolve()
            if candidate.is_file():
                return candidate
        return None

    if normalized.startswith("/media/"):
        media_root = Path(str(settings.media_root or "").strip() or "app/media")
        relative = normalized.removeprefix("/media/").strip().lstrip("/\\")
        if not relative:
            return None
        candidate = (media_root.resolve() / relative).resolve()
        return candidate if candidate.is_file() else None

    maybe_absolute = Path(normalized)
    if maybe_absolute.is_absolute() and maybe_absolute.is_file():
        return maybe_absolute.resolve()
    return None


def _logo_upload_root_candidates() -> tuple[Path, ...]:
    configured = Path(
        str(settings.company_logo_upload_dir or "").strip() or "app/static/uploads/company"
    )
    service_default = Path("app/static/uploads/company")
    package_default = Path(__file__).resolve().parents[1] / "static" / "uploads" / "company"
    docker_alt = Path("/app/static/uploads/company")
    docker_repo = Path("/app/app/static/uploads/company")

    candidates: list[Path] = []
    for root in (configured, service_default, package_default, docker_alt, docker_repo):
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved in candidates:
            continue
        candidates.append(resolved)
    return tuple(candidates)


def _lookup_or_none(
    db: Session,
    model: type,
    entity_id: int | None,
    fallback: Any = None,
) -> Any | None:
    if fallback is not None:
        return fallback
    if not entity_id:
        return None
    return db.get(model, entity_id)


def build_ticket_print_payload(db: Session, ticket: Ticket) -> dict[str, Any]:
    customer = _lookup_or_none(db, Customer, ticket.customer_id)
    vehicle = _lookup_or_none(db, Vehicle, ticket.vehicle_id)
    product = _lookup_or_none(db, Product, ticket.product_id, fallback=ticket.product)
    haulier = _lookup_or_none(db, Haulier, ticket.haulier_id, fallback=ticket.haulier)
    driver = _lookup_or_none(db, Driver, ticket.driver_id, fallback=ticket.driver)
    container = _lookup_or_none(
        db, Container, ticket.container_id, fallback=ticket.container
    )
    destination = _lookup_or_none(
        db, Destination, ticket.destination_id, fallback=ticket.destination
    )
    yard = _lookup_or_none(db, Yard, ticket.yard_id)
    area = _lookup_or_none(db, Area, ticket.area_id)
    waste_code = _lookup_or_none(db, WasteCode, ticket.waste_code_id)

    direction = _enum_value(ticket.direction)
    transaction_type = _enum_value(ticket.transaction_type)
    status = _enum_value(ticket.status)

    unit = product.unit if product else None
    logo_data_uri = _company_logo_src(db)

    return {
        "ticket_id": ticket.id,
        "ticket_no": ticket.ticket_no or "",
        "datetime_iso": ticket.datetime.isoformat() if ticket.datetime else "",
        "datetime_display": _format_dt(ticket.datetime),
        "status": status,
        "branding": {
            "logo_data_uri": logo_data_uri,
        },
        "direction": direction,
        "transaction_type": transaction_type,
        "is_sale": transaction_type == "SALE",
        "is_waste": transaction_type.startswith("WASTE"),
        "walk_in": bool(ticket.walk_in),
        "walk_in_sale": bool(ticket.walk_in_sale),
        "po_number": ticket.po_number or "",
        "dont_invoice": bool(ticket.dont_invoice),
        "paid": bool(ticket.paid),
        "customer": {
            "id": ticket.customer_id,
            "name": customer.name if customer else "",
            "account_code": customer.account_code if customer else "",
        },
        "vehicle": {
            "id": ticket.vehicle_id,
            "registration": (
                vehicle.registration if vehicle else (ticket.vehicle_reg_text or "")
            ),
        },
        "product": {
            "id": ticket.product_id,
            "code": product.code if product else "",
            "description": product.description if product else "",
            "unit_name": unit.name if unit else (ticket.pricing_unit_name or ""),
            "unit_type": unit.unit_type if unit else (ticket.pricing_unit_type or ""),
        },
        "logistics": {
            "haulier": haulier.name if haulier else "",
            "driver": driver.name if driver else "",
            "container": container.name if container else "",
            "destination": destination.name if destination else "",
            "yard": yard.code if yard else "",
            "area": area.code if area else "",
            "waste_code": waste_code.code if waste_code else "",
            "carrier_licence_number": (
                ticket.carrier_licence_number
                or (haulier.carrier_licence_number if haulier else "")
                or ""
            ),
        },
        "weights": {
            "gross_kg": _to_float(ticket.gross_kg),
            "tare_kg": _to_float(ticket.tare_kg),
            "net_kg": _to_float(ticket.net_kg),
            "qty": _to_float(ticket.qty),
            "unit_price": _to_float(ticket.unit_price),
            "total": _to_float(ticket.total),
            "gross_kg_display": _format_kg(ticket.gross_kg),
            "tare_kg_display": _format_kg(ticket.tare_kg),
            "net_kg_display": _format_kg(ticket.net_kg),
            "qty_display": _format_qty(ticket.qty),
            "unit_price_display": _format_money(ticket.unit_price),
            "total_display": _format_money(ticket.total),
        },
        "compliance": {
            "ewc_code_display": ticket.ewc_code_display or "",
            "ewc_code_6": ticket.ewc_code_6 or "",
            "ewc_description": ticket.ewc_description or "",
            "ewc_hazardous": bool(ticket.ewc_hazardous),
            "waste_producer_source": _enum_value(ticket.waste_producer_source),
            "waste_producer_name": ticket.waste_producer_name or "",
            "waste_producer_address": ticket.waste_producer_address or "",
        },
    }
