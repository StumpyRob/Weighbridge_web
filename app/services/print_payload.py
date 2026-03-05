
from __future__ import annotations

import base64
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
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
    Invoice,
    InvoiceLine,
    Product,
    Ticket,
    TicketStatusEnum,
    Vehicle,
    WasteCode,
    Yard,
)
from .printing import DOCUMENT_TYPE_INVOICE, DOCUMENT_TYPE_TICKET, DOCUMENT_TYPE_WTN
from .uploads import logo_file_from_web_path, resolve_company_logo_web_path


PRINT_PAYLOAD_KEYS: tuple[str, ...] = (
    "document_type",
    "source_id",
    "source_ref",
    "reference_no",
    "ticket_id",
    "ticket_no",
    "invoice_id",
    "invoice_no",
    "wtn_no",
    "datetime",
    "datetime_iso",
    "date_time",
    "due_date",
    "status",
    "direction",
    "transaction_type",
    "is_complete",
    "is_sale",
    "is_waste",
    "customer_name",
    "customer_account_code",
    "customer_address",
    "producer",
    "producer_address",
    "vehicle_reg",
    "driver_name",
    "haulier_name",
    "carrier_name",
    "carrier_reg",
    "origin_site",
    "destination_name",
    "destination_site",
    "product_code",
    "product_description",
    "waste_description",
    "ewc_code",
    "ewc_description",
    "gross_kg",
    "tare_kg",
    "net_kg",
    "gross_kg_display",
    "tare_kg_display",
    "net_kg_display",
    "qty",
    "qty_display",
    "quantity_net_kg",
    "quantity_tonnes",
    "quantity_net_kg_display",
    "quantity_tonnes_display",
    "unit_price",
    "unit_price_display",
    "total",
    "total_display",
    "net_total",
    "vat_total",
    "gross_total",
    "net_total_display",
    "vat_total_display",
    "gross_total_display",
    "line_items",
    "notes",
    "send_blockers",
    "send_ready",
    "logo_data_uri",
)


def _normalize_document_type(value: str | None) -> str:
    normalized = str(value or "").strip().upper()
    if normalized in {DOCUMENT_TYPE_TICKET, DOCUMENT_TYPE_INVOICE, DOCUMENT_TYPE_WTN}:
        return normalized
    return DOCUMENT_TYPE_TICKET


def _enum_value(value: Any) -> str:
    if value is None:
        return ""
    return value.value if hasattr(value, "value") else str(value)


def _format_dt(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.strftime("%d/%m/%Y %H:%M")


def _format_date(value: date | None) -> str:
    if value is None:
        return ""
    return value.strftime("%d/%m/%Y")


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
    return f"GBP {decimal_value:,.2f}"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _text_or_dash(value: Any) -> str:
    cleaned = _text(value)
    return cleaned if cleaned else "-"


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
        return resolve_company_logo_web_path(current)
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
        return logo_file_from_web_path(normalized)

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


def _format_customer_address(customer: Customer | None) -> str:
    if customer is None:
        return ""
    lines = [
        _text(customer.address_line1),
        _text(customer.address_line2),
        _text(customer.city),
        _text(customer.postcode),
        _text(customer.country),
    ]
    return ", ".join(line for line in lines if line)


def _is_waste_transaction(value: Any) -> bool:
    normalized = _text(_enum_value(value)).upper()
    return normalized.startswith("WASTE")


def _wtn_reference(ticket: Ticket) -> str:
    ticket_no = _text(ticket.ticket_no)
    if ticket_no:
        return f"WTN-{ticket_no}"
    return f"WTN-{int(ticket.id)}"


def _wtn_quantity_tonnes(net_kg: Decimal | None) -> Decimal | None:
    if net_kg is None:
        return None
    return (net_kg / Decimal("1000")).quantize(
        Decimal("0.001"),
        rounding=ROUND_HALF_UP,
    )


def _empty_payload(document_type: str) -> dict[str, Any]:
    normalized_document_type = _normalize_document_type(document_type)
    payload: dict[str, Any] = {
        "document_type": normalized_document_type,
        "source_id": None,
        "source_ref": "",
        "reference_no": "",
        "ticket_id": None,
        "ticket_no": "",
        "invoice_id": None,
        "invoice_no": "",
        "wtn_no": "",
        "datetime": "",
        "datetime_iso": "",
        "date_time": "",
        "due_date": "",
        "status": "",
        "direction": "",
        "transaction_type": "",
        "is_complete": False,
        "is_sale": False,
        "is_waste": False,
        "customer_name": "",
        "customer_account_code": "",
        "customer_address": "",
        "producer": "",
        "producer_address": "",
        "vehicle_reg": "",
        "driver_name": "",
        "haulier_name": "",
        "carrier_name": "",
        "carrier_reg": "",
        "origin_site": "",
        "destination_name": "",
        "destination_site": "",
        "product_code": "",
        "product_description": "",
        "waste_description": "",
        "ewc_code": "",
        "ewc_description": "",
        "gross_kg": None,
        "tare_kg": None,
        "net_kg": None,
        "gross_kg_display": "-",
        "tare_kg_display": "-",
        "net_kg_display": "-",
        "qty": None,
        "qty_display": "-",
        "quantity_net_kg": None,
        "quantity_tonnes": None,
        "quantity_net_kg_display": "-",
        "quantity_tonnes_display": "-",
        "unit_price": None,
        "unit_price_display": "-",
        "total": None,
        "total_display": "-",
        "net_total": None,
        "vat_total": None,
        "gross_total": None,
        "net_total_display": "-",
        "vat_total_display": "-",
        "gross_total_display": "-",
        "line_items": [],
        "notes": "",
        "send_blockers": [],
        "send_ready": False,
        "logo_data_uri": "",
    }
    return payload

def _sample_ticket_payload() -> dict[str, Any]:
    payload = _empty_payload(DOCUMENT_TYPE_TICKET)
    payload.update(
        {
            "source_id": 0,
            "source_ref": "T-SAMPLE",
            "reference_no": "T-SAMPLE",
            "ticket_id": 0,
            "ticket_no": "T-SAMPLE",
            "datetime": "-",
            "date_time": "-",
            "status": "OPEN",
            "direction": "INWARD",
            "transaction_type": "SALE",
            "is_sale": True,
            "customer_name": "Sample Customer",
            "customer_account_code": "C-SAMPLE",
            "vehicle_reg": "SAMPLE-123",
            "driver_name": "Sample Driver",
            "haulier_name": "Sample Haulier",
            "destination_name": "Sample Destination",
            "product_code": "P-SAMPLE",
            "product_description": "Sample Product",
            "gross_kg": 0.0,
            "tare_kg": 0.0,
            "net_kg": 0.0,
            "gross_kg_display": "0.000 kg",
            "tare_kg_display": "0.000 kg",
            "net_kg_display": "0.000 kg",
            "qty": 0.0,
            "qty_display": "0.000",
            "unit_price": 0.0,
            "unit_price_display": "GBP 0.00",
            "total": 0.0,
            "total_display": "GBP 0.00",
            "send_ready": True,
        }
    )
    return payload


def _sample_invoice_payload() -> dict[str, Any]:
    payload = _empty_payload(DOCUMENT_TYPE_INVOICE)
    payload.update(
        {
            "source_id": 0,
            "source_ref": "INV-SAMPLE",
            "reference_no": "INV-SAMPLE",
            "invoice_id": 0,
            "invoice_no": "INV-SAMPLE",
            "datetime": _format_date(date.today()),
            "due_date": "",
            "status": "DRAFT",
            "customer_name": "Sample Customer",
            "customer_account_code": "C-SAMPLE",
            "customer_address": "1 Sample Street, Sample Town, AA11 1AA",
            "vehicle_reg": "SAMPLE-123",
            "qty": 1.0,
            "qty_display": "1.000",
            "unit_price": 100.0,
            "unit_price_display": "GBP 100.00",
            "total": 120.0,
            "total_display": "GBP 120.00",
            "net_total": 100.0,
            "vat_total": 20.0,
            "gross_total": 120.0,
            "net_total_display": "GBP 100.00",
            "vat_total_display": "GBP 20.00",
            "gross_total_display": "GBP 120.00",
            "line_items": [
                {
                    "description": "Sample line item",
                    "qty": "1",
                    "unit_price": "100.00",
                    "net": "100.00",
                    "vat": "20.00",
                    "gross": "120.00",
                }
            ],
            "send_ready": True,
        }
    )
    return payload


def _sample_wtn_payload() -> dict[str, Any]:
    payload = _empty_payload(DOCUMENT_TYPE_WTN)
    payload.update(
        {
            "source_id": 0,
            "source_ref": "WTN-T-SAMPLE",
            "reference_no": "WTN-T-SAMPLE",
            "ticket_id": 0,
            "ticket_no": "T-SAMPLE",
            "wtn_no": "WTN-T-SAMPLE",
            "datetime": "-",
            "date_time": "-",
            "status": "COMPLETE",
            "is_complete": True,
            "is_waste": True,
            "customer_name": "Sample Producer",
            "producer": "Sample Producer",
            "producer_address": "Sample Producer Address",
            "carrier_name": "Sample Carrier",
            "haulier_name": "Sample Carrier",
            "carrier_reg": "CBDU12345",
            "vehicle_reg": "SAMPLE-123",
            "driver_name": "Sample Driver",
            "waste_description": "Sample Waste",
            "ewc_code": "17 09 04",
            "ewc_description": "Mixed construction waste",
            "quantity_net_kg": 0.0,
            "quantity_tonnes": 0.0,
            "quantity_net_kg_display": "0.000",
            "quantity_tonnes_display": "0.000",
            "origin_site": "Sample Yard",
            "destination_site": "Sample Destination",
            "destination_name": "Sample Destination",
            "send_ready": True,
        }
    )
    payload["send_blockers"] = []
    return payload


def _build_ticket_payload_from_ticket(db: Session, ticket: Ticket) -> dict[str, Any]:
    customer = _lookup_or_none(db, Customer, ticket.customer_id)
    vehicle = _lookup_or_none(db, Vehicle, ticket.vehicle_id)
    product = _lookup_or_none(db, Product, ticket.product_id, fallback=ticket.product)
    haulier = _lookup_or_none(db, Haulier, ticket.haulier_id, fallback=ticket.haulier)
    driver = _lookup_or_none(db, Driver, ticket.driver_id, fallback=ticket.driver)
    destination = _lookup_or_none(
        db, Destination, ticket.destination_id, fallback=ticket.destination
    )
    _ = _lookup_or_none(db, Container, ticket.container_id, fallback=ticket.container)

    transaction_type = _enum_value(ticket.transaction_type)
    status = _enum_value(ticket.status)

    net_kg_decimal = _to_decimal(ticket.net_kg)
    tonnes_decimal = _wtn_quantity_tonnes(net_kg_decimal)

    payload = _empty_payload(DOCUMENT_TYPE_TICKET)
    payload.update(
        {
            "source_id": int(ticket.id),
            "source_ref": ticket.ticket_no or "",
            "reference_no": ticket.ticket_no or "",
            "ticket_id": int(ticket.id),
            "ticket_no": ticket.ticket_no or "",
            "datetime": _format_dt(ticket.datetime),
            "datetime_iso": ticket.datetime.isoformat() if ticket.datetime else "",
            "date_time": _format_dt(ticket.datetime),
            "status": status,
            "direction": _enum_value(ticket.direction),
            "transaction_type": transaction_type,
            "is_complete": status == TicketStatusEnum.COMPLETE.value,
            "is_sale": transaction_type == "SALE",
            "is_waste": transaction_type.startswith("WASTE"),
            "customer_name": customer.name if customer else "",
            "customer_account_code": customer.account_code if customer else "",
            "customer_address": _format_customer_address(customer),
            "vehicle_reg": (
                ticket.vehicle_reg_text or (vehicle.registration if vehicle else "") or ""
            ),
            "driver_name": driver.name if driver else "",
            "haulier_name": haulier.name if haulier else "",
            "carrier_name": haulier.name if haulier else "",
            "carrier_reg": (
                ticket.carrier_licence_number
                or (haulier.carrier_licence_number if haulier else "")
                or ""
            ),
            "destination_name": destination.name if destination else "",
            "destination_site": destination.name if destination else "",
            "product_code": product.code if product else "",
            "product_description": product.description if product else "",
            "waste_description": ticket.ewc_description or "",
            "ewc_code": ticket.ewc_code_display or ticket.ewc_code_6 or "",
            "ewc_description": ticket.ewc_description or "",
            "gross_kg": _to_float(ticket.gross_kg),
            "tare_kg": _to_float(ticket.tare_kg),
            "net_kg": _to_float(ticket.net_kg),
            "gross_kg_display": _format_kg(ticket.gross_kg),
            "tare_kg_display": _format_kg(ticket.tare_kg),
            "net_kg_display": _format_kg(ticket.net_kg),
            "qty": _to_float(ticket.qty),
            "qty_display": _format_qty(ticket.qty),
            "quantity_net_kg": _to_float(ticket.net_kg),
            "quantity_tonnes": float(tonnes_decimal) if tonnes_decimal is not None else None,
            "quantity_net_kg_display": (
                f"{net_kg_decimal:,.3f}" if net_kg_decimal is not None else "-"
            ),
            "quantity_tonnes_display": (
                f"{tonnes_decimal:,.3f}" if tonnes_decimal is not None else "-"
            ),
            "unit_price": _to_float(ticket.unit_price),
            "unit_price_display": _format_money(ticket.unit_price),
            "total": _to_float(ticket.total),
            "total_display": _format_money(ticket.total),
            "notes": "",
            "send_ready": True,
            "logo_data_uri": _company_logo_src(db),
        }
    )
    return payload

def _build_invoice_payload_from_invoice(db: Session, invoice: Invoice) -> dict[str, Any]:
    customer = db.get(Customer, invoice.customer_id) if invoice.customer_id else None
    customer_snapshot = (
        dict(invoice.customer_snapshot_json)
        if isinstance(invoice.customer_snapshot_json, dict)
        else {}
    )
    customer_name = _text(customer.name if customer else "") or _text(
        customer_snapshot.get("name")
    )
    customer_account_code = _text(customer.account_code if customer else "") or _text(
        customer_snapshot.get("account_code")
    )
    customer_address = _format_customer_address(customer)

    lines = list(
        db.execute(
            select(InvoiceLine)
            .where(InvoiceLine.invoice_id == invoice.id)
            .order_by(InvoiceLine.id.asc())
        ).scalars()
    )

    line_items: list[dict[str, str]] = []
    ticket_ids = [int(line.ticket_id) for line in lines if line.ticket_id]
    tickets_by_id: dict[int, Ticket] = {}
    if ticket_ids:
        tickets_by_id = {
            row.id: row
            for row in db.execute(select(Ticket).where(Ticket.id.in_(ticket_ids))).scalars()
        }

    vehicle_regs: list[str] = []
    qty_total = Decimal("0")
    for line in lines:
        quantity_decimal = _to_decimal(line.quantity) or Decimal("0")
        qty_total += quantity_decimal
        unit_price_decimal = _to_decimal(line.unit_price) or Decimal("0")
        net_decimal = _to_decimal(line.net) or Decimal("0")
        vat_decimal = _to_decimal(line.vat) or Decimal("0")
        gross_decimal = _to_decimal(line.gross) or Decimal("0")
        line_items.append(
            {
                "description": _text_or_dash(line.description),
                "qty": f"{quantity_decimal:,.3f}",
                "unit_price": f"{unit_price_decimal:,.2f}",
                "net": f"{net_decimal:,.2f}",
                "vat": f"{vat_decimal:,.2f}",
                "gross": f"{gross_decimal:,.2f}",
            }
        )
        ticket = tickets_by_id.get(int(line.ticket_id)) if line.ticket_id else None
        reg = _text(ticket.vehicle_reg_text if ticket else "")
        if reg and reg not in vehicle_regs:
            vehicle_regs.append(reg)

    payload = _empty_payload(DOCUMENT_TYPE_INVOICE)
    payload.update(
        {
            "source_id": int(invoice.id),
            "source_ref": invoice.invoice_no or "",
            "reference_no": invoice.invoice_no or "",
            "invoice_id": int(invoice.id),
            "invoice_no": invoice.invoice_no or "",
            "datetime": _format_date(invoice.invoice_date),
            "due_date": _format_date(invoice.due_date),
            "status": invoice.status or "",
            "customer_name": customer_name,
            "customer_account_code": customer_account_code,
            "customer_address": customer_address,
            "vehicle_reg": ", ".join(vehicle_regs),
            "qty": float(qty_total) if lines else None,
            "qty_display": _format_qty(qty_total if lines else None),
            "unit_price": _to_float(lines[0].unit_price) if lines else None,
            "unit_price_display": _format_money(lines[0].unit_price) if lines else "-",
            "total": _to_float(invoice.gross_total),
            "total_display": _format_money(invoice.gross_total),
            "net_total": _to_float(invoice.net_total),
            "vat_total": _to_float(invoice.vat_total),
            "gross_total": _to_float(invoice.gross_total),
            "net_total_display": _format_money(invoice.net_total),
            "vat_total_display": _format_money(invoice.vat_total),
            "gross_total_display": _format_money(invoice.gross_total),
            "line_items": line_items,
            "send_ready": True,
            "logo_data_uri": _company_logo_src(db),
        }
    )
    return payload


def wtn_send_missing_fields(payload: dict[str, Any]) -> list[str]:
    missing: list[str] = []

    if not _text(payload.get("ewc_code")):
        missing.append("EWC code")
    if not _text(payload.get("customer_name")):
        missing.append("Customer")
    if not _text(payload.get("waste_description")):
        missing.append("Waste description")
    if not _text(payload.get("carrier_name")):
        missing.append("Carrier/haulier")
    if not _text(payload.get("destination_site")):
        missing.append("Destination site")

    net_kg_value = _to_decimal(payload.get("quantity_net_kg"))
    tonnes_value = _to_decimal(payload.get("quantity_tonnes"))
    has_quantity = (
        (net_kg_value is not None and net_kg_value > 0)
        or (tonnes_value is not None and tonnes_value > 0)
    )
    if not has_quantity:
        missing.append("Net weight/quantity")

    return missing


def _build_wtn_payload_from_ticket(db: Session, ticket: Ticket) -> dict[str, Any]:
    if not _is_waste_transaction(ticket.transaction_type):
        raise ValueError("WTN is only available for waste tickets.")

    customer = _lookup_or_none(db, Customer, ticket.customer_id)
    vehicle = _lookup_or_none(db, Vehicle, ticket.vehicle_id)
    product = _lookup_or_none(db, Product, ticket.product_id, fallback=ticket.product)
    haulier = _lookup_or_none(db, Haulier, ticket.haulier_id, fallback=ticket.haulier)
    driver = _lookup_or_none(db, Driver, ticket.driver_id, fallback=ticket.driver)
    destination = _lookup_or_none(
        db, Destination, ticket.destination_id, fallback=ticket.destination
    )
    yard = _lookup_or_none(db, Yard, ticket.yard_id)
    _ = _lookup_or_none(db, Area, ticket.area_id)
    waste_code = _lookup_or_none(db, WasteCode, ticket.waste_code_id)

    producer_name = _text(ticket.waste_producer_name) or _text(
        customer.name if customer else ""
    )
    producer_address = _text(ticket.waste_producer_address) or _format_customer_address(
        customer
    )
    customer_name = _text(customer.name if customer else "") or producer_name
    carrier_name = _text(haulier.name if haulier else "")
    carrier_reg = _text(ticket.carrier_licence_number) or _text(
        haulier.carrier_licence_number if haulier else ""
    )
    vehicle_reg = _text(ticket.vehicle_reg_text) or _text(
        vehicle.registration if vehicle else ""
    )
    driver_name = _text(driver.name if driver else "")
    waste_description = _text(ticket.ewc_description) or _text(
        product.description if product else ""
    )

    ewc_code = (
        _text(ticket.ewc_code_display)
        or _text(ticket.ewc_code_6)
        or _text(waste_code.code if waste_code else "")
    )
    ewc_description = _text(ticket.ewc_description) or _text(
        waste_code.description if waste_code else ""
    )

    net_kg_decimal = _to_decimal(ticket.net_kg)
    tonnes_decimal = _wtn_quantity_tonnes(net_kg_decimal)
    quantity_net_kg = float(net_kg_decimal) if net_kg_decimal is not None else None
    quantity_tonnes = float(tonnes_decimal) if tonnes_decimal is not None else None

    payload = _empty_payload(DOCUMENT_TYPE_WTN)
    payload.update(
        {
            "source_id": int(ticket.id),
            "source_ref": _wtn_reference(ticket),
            "reference_no": _wtn_reference(ticket),
            "ticket_id": int(ticket.id),
            "ticket_no": _text_or_dash(ticket.ticket_no),
            "wtn_no": _wtn_reference(ticket),
            "datetime": _format_dt(ticket.datetime) or "-",
            "datetime_iso": ticket.datetime.isoformat() if ticket.datetime else "",
            "date_time": _format_dt(ticket.datetime) or "-",
            "status": _enum_value(ticket.status),
            "is_complete": _enum_value(ticket.status) == TicketStatusEnum.COMPLETE.value,
            "is_waste": True,
            "customer_name": customer_name,
            "customer_address": _format_customer_address(customer),
            "producer": producer_name or "-",
            "producer_address": producer_address or "-",
            "vehicle_reg": vehicle_reg or "-",
            "driver_name": driver_name or "-",
            "haulier_name": carrier_name,
            "carrier_name": carrier_name,
            "carrier_reg": carrier_reg or "-",
            "origin_site": _text(yard.description if yard else "")
            or _text(yard.code if yard else "")
            or "-",
            "destination_name": _text(destination.name if destination else ""),
            "destination_site": _text(destination.name if destination else ""),
            "waste_description": waste_description,
            "ewc_code": ewc_code,
            "ewc_description": ewc_description or "-",
            "net_kg": quantity_net_kg,
            "net_kg_display": f"{net_kg_decimal:,.3f} kg" if net_kg_decimal is not None else "-",
            "quantity_net_kg": quantity_net_kg,
            "quantity_tonnes": quantity_tonnes,
            "quantity_net_kg_display": (
                f"{net_kg_decimal:,.3f}" if net_kg_decimal is not None else "-"
            ),
            "quantity_tonnes_display": (
                f"{tonnes_decimal:,.3f}" if tonnes_decimal is not None else "-"
            ),
            "notes": "-",
            "logo_data_uri": _company_logo_src(db),
        }
    )
    blockers = wtn_send_missing_fields(payload)
    payload["send_blockers"] = blockers
    payload["send_ready"] = len(blockers) == 0
    return payload


def build_print_payload(
    db: Session | None,
    document_type: str,
    source_id: int | None = None,
) -> dict[str, Any]:
    normalized_document_type = _normalize_document_type(document_type)
    if source_id is None:
        payload: dict[str, Any]
        if normalized_document_type == DOCUMENT_TYPE_INVOICE:
            payload = _sample_invoice_payload()
        elif normalized_document_type == DOCUMENT_TYPE_WTN:
            payload = _sample_wtn_payload()
        else:
            payload = _sample_ticket_payload()
        if db is not None:
            payload["logo_data_uri"] = _company_logo_src(db)
        return payload

    if db is None:
        raise ValueError("Database session is required when source_id is provided.")

    if normalized_document_type == DOCUMENT_TYPE_INVOICE:
        invoice = db.get(Invoice, int(source_id))
        if invoice is None:
            raise LookupError("Invoice not found.")
        return _build_invoice_payload_from_invoice(db, invoice)

    ticket = db.get(Ticket, int(source_id))
    if ticket is None:
        raise LookupError("Ticket not found.")

    if normalized_document_type == DOCUMENT_TYPE_WTN:
        return _build_wtn_payload_from_ticket(db, ticket)
    return _build_ticket_payload_from_ticket(db, ticket)


def build_ticket_print_payload(db: Session, ticket: Ticket) -> dict[str, Any]:
    return _build_ticket_payload_from_ticket(db, ticket)


def build_wtn_payload(db: Session, ticket_id: int) -> dict[str, Any]:
    payload = build_print_payload(db, DOCUMENT_TYPE_WTN, source_id=ticket_id)
    if _normalize_document_type(payload.get("document_type")) != DOCUMENT_TYPE_WTN:
        raise ValueError("WTN payload build failed.")
    return payload

def print_payload_variable_docs() -> list[dict[str, str]]:
    ticket_sample = build_print_payload(None, DOCUMENT_TYPE_TICKET)
    invoice_sample = build_print_payload(None, DOCUMENT_TYPE_INVOICE)
    wtn_sample = build_print_payload(None, DOCUMENT_TYPE_WTN)

    descriptions = {
        "document_type": "Document type: TICKET, INVOICE, or WTN.",
        "source_id": "Internal source id for the document.",
        "source_ref": "Main document reference string.",
        "reference_no": "Reference shown on printouts.",
        "ticket_id": "Ticket id when source is a ticket.",
        "ticket_no": "Ticket number.",
        "invoice_id": "Invoice id when source is an invoice.",
        "invoice_no": "Invoice number.",
        "wtn_no": "Waste transfer note reference.",
        "datetime": "Display date/time value.",
        "datetime_iso": "ISO datetime string when available.",
        "date_time": "Alias of display date/time for templates.",
        "due_date": "Invoice due date.",
        "status": "Current document status.",
        "direction": "Ticket direction.",
        "transaction_type": "Ticket transaction type.",
        "is_complete": "True when source document is complete.",
        "is_sale": "True for sale transactions.",
        "is_waste": "True for waste transactions.",
        "customer_name": "Customer or producer name.",
        "customer_account_code": "Customer account code.",
        "customer_address": "Single-line customer address.",
        "producer": "Waste producer name.",
        "producer_address": "Waste producer address.",
        "vehicle_reg": "Vehicle registration.",
        "driver_name": "Driver name.",
        "haulier_name": "Haulier name.",
        "carrier_name": "Carrier name (WTN).",
        "carrier_reg": "Carrier licence/registration.",
        "origin_site": "Origin site/yard.",
        "destination_name": "Destination display name.",
        "destination_site": "Destination site text.",
        "product_code": "Product code.",
        "product_description": "Product or waste description.",
        "waste_description": "Waste description field.",
        "ewc_code": "EWC code.",
        "ewc_description": "EWC description.",
        "gross_kg": "Gross weight as number.",
        "tare_kg": "Tare weight as number.",
        "net_kg": "Net weight as number.",
        "gross_kg_display": "Gross weight display text.",
        "tare_kg_display": "Tare weight display text.",
        "net_kg_display": "Net weight display text.",
        "qty": "Quantity as number.",
        "qty_display": "Quantity display text.",
        "quantity_net_kg": "WTN net quantity in kg.",
        "quantity_tonnes": "WTN net quantity in tonnes.",
        "quantity_net_kg_display": "WTN net kg display text.",
        "quantity_tonnes_display": "WTN tonnes display text.",
        "unit_price": "Unit price as number.",
        "unit_price_display": "Unit price display text.",
        "total": "Line/total amount as number.",
        "total_display": "Line/total amount display text.",
        "net_total": "Invoice net total as number.",
        "vat_total": "Invoice VAT total as number.",
        "gross_total": "Invoice gross total as number.",
        "net_total_display": "Invoice net total display text.",
        "vat_total_display": "Invoice VAT total display text.",
        "gross_total_display": "Invoice gross total display text.",
        "line_items": "Invoice line list (each item has description, qty, unit_price, net, vat, gross).",
        "notes": "Free-form notes text.",
        "send_blockers": "Missing compliance fields for send actions.",
        "send_ready": "True when send checks pass.",
        "logo_data_uri": "Resolved company logo path/data URI.",
    }

    rows: list[dict[str, str]] = []
    for key in PRINT_PAYLOAD_KEYS:
        example_value = ticket_sample.get(key)
        if key in {
            "invoice_id",
            "invoice_no",
            "net_total",
            "vat_total",
            "gross_total",
            "net_total_display",
            "vat_total_display",
            "gross_total_display",
            "line_items",
        }:
            example_value = invoice_sample.get(key)
        elif key in {
            "wtn_no",
            "producer",
            "producer_address",
            "quantity_net_kg",
            "quantity_tonnes",
            "quantity_net_kg_display",
            "quantity_tonnes_display",
            "send_blockers",
        }:
            example_value = wtn_sample.get(key)
        rows.append(
            {
                "name": f"payload.{key}",
                "description": descriptions.get(key, "Template payload field."),
                "example": str(example_value),
            }
        )
    return rows
