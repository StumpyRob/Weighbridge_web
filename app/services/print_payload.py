from __future__ import annotations

import base64
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from ..models import (
    Area,
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


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOGO_FILENAME = "logo.png"


@lru_cache(maxsize=1)
def _logo_data_uri() -> str:
    logo_path = PROJECT_ROOT / LOGO_FILENAME
    if not logo_path.is_file():
        return ""
    try:
        logo_bytes = logo_path.read_bytes()
    except OSError:
        return ""
    if not logo_bytes:
        return ""
    encoded_logo = base64.b64encode(logo_bytes).decode("ascii")
    return f"data:image/png;base64,{encoded_logo}"


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
    logo_data_uri = _logo_data_uri()

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
