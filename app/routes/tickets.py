from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html import escape
import logging
import re
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from sqlalchemy import and_, case, func, or_, select, text
from sqlalchemy.orm import Session, joinedload

from ..audit import diff as audit_diff
from ..audit import log as audit_log
from ..auth import normalize_email, user_display_name, validate_email
from ..config import settings
from ..constants import (
    ADDRESS_LINE_MAX,
    CODE_MAX,
    NAME_MAX,
    NOTES_MAX,
    PO_NUMBER_MAX,
    POSTCODE_MAX,
    REG_MAX,
)
from ..db import get_db
from ..models.base import utcnow
from ..permissions import (
    PERM_COMPLETE_TICKETS,
    PERM_MANAGE_TICKETS,
    PERM_VIEW_TICKETS,
    PERM_VOID_TICKETS,
    require_permission,
)
from ..models import (
    Area,
    Container,
    Customer,
    DirectionEnum,
    Destination,
    Driver,
    EwcCode,
    Haulier,
    Invoice,
    InvoiceLine,
    PrintDestination,
    PrintJob,
    PrintTemplate,
    Product,
    Ticket,
    TicketVoid,
    TicketStatusEnum,
    TransactionTypeEnum,
    Unit,
    Vehicle,
    WasteCode,
    WasteProducerSourceEnum,
    VoidReason,
    Yard,
)
from ..security import validate_no_html, validate_no_html_fields
from ..services.pricing import resolve_unit_price_for_customer_product
from ..services.credit import (
    customer_outstanding_total,
    money_decimal,
    outstanding_display_values,
)
from ..services.email_service import (
    EmailAttachment,
    get_platform_email_settings,
    render_email_template,
    send_email,
)
from ..services.platform_qz_settings import platform_qz_ready_for_tenants
from ..services.qz_printing import (
    qz_direct_print_enabled_from_destination,
    qz_printer_name_from_delivery_config,
)
from ..services.signatures import normalize_png_data_url, png_has_visible_ink
from ..services.pdf import render_html_pdf_bytes
from ..services.print_payload import build_ticket_print_payload, build_wtn_payload
from ..services.print_render import (
    render_from_content,
)
from ..services.printing import (
    DELIVERY_TYPE_EMAIL_PDF,
    DELIVERY_TYPE_PRINT_LOCAL_BROWSER,
    DOCUMENT_TYPE_TICKET,
    DOCUMENT_TYPE_WTN,
    PRINT_CONTENT_TYPE_HTML,
    PRINT_CONTENT_TYPE_PDF,
    PRINT_CONTENT_TYPE_TEXT,
    RenderedPrint,
    execute_rendered_print,
    replay_print_job,
    render_destination_content,
)
from ..services.system_setup import get_company_setting, missing_required_lookup_messages
from ..services.wip_snapshots import ticket_wip_snapshot
from ..seed import seed_void_reasons
from ..templating import templates


def _require_tickets_view(request: Request) -> None:
    require_permission(request, PERM_VIEW_TICKETS)


def _require_tickets_manage(request: Request) -> None:
    require_permission(request, PERM_MANAGE_TICKETS)


router = APIRouter(dependencies=[Depends(_require_tickets_view)])
logger = logging.getLogger(__name__)

LOCKED_STATUSES = {TicketStatusEnum.COMPLETE.value, TicketStatusEnum.VOID.value}
NEW_TICKET_DEDUP_SECONDS = 5
WEIGHT_MAX_KG = Decimal("1000000")
WEIGHT_QUANTIZE = Decimal("1")
INACTIVE_PRODUCT_UNIT_ERROR = "Product unit is inactive. Choose a different product."
SALES_ONLY_WASTE_ERROR = (
    "This product is sales-only and cannot be used on waste tickets."
)
SALES_ONLY_WASTE_WARNING = (
    "Selected product is sales-only and cannot be used on waste tickets."
)
WASTE_ONLY_SALE_ERROR = "This product is waste-only and cannot be used on sale tickets."
WASTE_ONLY_SALE_WARNING = (
    "Selected product is waste-only and cannot be used on sale tickets."
)
FINAL_DISPOSAL_DESTINATION_ERROR = (
    "Destination site/company is required to complete tickets for final disposal products."
)
PRODUCT_TYPE_SALE = "sale"
PRODUCT_TYPE_WASTE = "waste"
VOID_REASON_TYPE_TICKET = "TICKET"
PO_NUMBER_MAX_LENGTH = PO_NUMBER_MAX
PO_REQUIRED_INVOICE_BANNER = (
    "PO required for invoicing. Add PO to release this ticket for invoicing."
)
COMPLIANCE_LOCKED_INVOICED_MESSAGE = (
    "Cannot update waste compliance because this ticket has already been invoiced."
)
WALK_IN_SALE_ONLY_ERROR = "Walk-in sale mode is only available for Sale tickets."
PO_UPDATE_ALLOWED_STATUSES = {
    TicketStatusEnum.OPEN.value,
    TicketStatusEnum.COMPLETE.value,
}
TICKET_SEARCH_MAX_LEN = 100
PRINT_REQUIRES_COMPLETE_ERROR = "Ticket must be complete to print."
WTN_SEND_REQUIRES_COMPLETE_ERROR = "Ticket must be complete before sending WTN."
TICKET_EMAIL_REQUIRES_COMPLETE_ERROR = "Ticket must be complete to email."
TICKET_EMAIL_DEFAULT_SUBJECT = "Ticket {ticket_no} from {company_name}"
TICKET_EMAIL_DEFAULT_BODY = (
    "Hello,\n\n"
    "Please find attached ticket {ticket_no}.\n\n"
    "Regards,\n"
    "{company_name}"
)
CREDIT_LIMIT_WARNING_RATIO = Decimal("0.80")
WTN_SIGNATURE_ROLE_LABELS: dict[str, str] = {
    "producer": "Producer",
    "carrier": "Carrier",
    "receiver": "Receiver",
}
WTN_SIGNATURE_ROLE_FIELDS: dict[str, tuple[str, str, str]] = {
    "producer": (
        "wtn_producer_signature_data_uri",
        "wtn_producer_signature_signed_at",
        "wtn_producer_signature_signer_name",
    ),
    "carrier": (
        "wtn_carrier_signature_data_uri",
        "wtn_carrier_signature_signed_at",
        "wtn_carrier_signature_signer_name",
    ),
    "receiver": (
        "wtn_receiver_signature_data_uri",
        "wtn_receiver_signature_signed_at",
        "wtn_receiver_signature_signer_name",
    ),
}
_TICKET_AUDIT_DIFF_KEYS = (
    "status",
    "product_id",
    "customer_id",
    "final_disposal",
    "used_on_site",
    "net_kg",
    "qty",
    "unit_price",
    "total",
)


def _voided_by_actor(request: Request) -> str:
    user = getattr(getattr(request, "state", None), "current_user", None)
    email = str(getattr(user, "email", "") or "").strip()
    if email:
        return email
    return "system"


@router.get("/tickets", response_class=HTMLResponse)
def tickets_list(
    request: Request,
    date_from: date | None = None,
    date_to: date | None = None,
    status: str | None = None,
    open_only: int | None = None,
    direction: str | None = None,
    transaction_type: str | None = None,
    walk_in_sale_only: int | None = None,
    ticket_no: str | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 20,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    search_query = _normalize_ticket_search_query(q)

    filters = []
    # Date filters are interpreted in server-local time (UTC by default).
    if date_from:
        filters.append(Ticket.datetime >= datetime.combine(date_from, time.min))
    if date_to:
        end_exclusive = datetime.combine(date_to + timedelta(days=1), time.min)
        filters.append(Ticket.datetime < end_exclusive)
    if open_only:
        filters.append(Ticket.status == TicketStatusEnum.OPEN.value)
    elif status:
        filters.append(Ticket.status == status)
    if direction:
        filters.append(Ticket.direction == direction)
    if transaction_type:
        filters.append(Ticket.transaction_type == transaction_type)
    if walk_in_sale_only:
        filters.append(Ticket.walk_in_sale.is_(True))
    if search_query:
        like = _ticket_contains_like_pattern(search_query)
        filters.append(
            or_(
                Ticket.ticket_no.ilike(like, escape="\\"),
                Vehicle.registration.ilike(like, escape="\\"),
                Customer.name.ilike(like, escape="\\"),
                Customer.account_code.ilike(like, escape="\\"),
                Product.code.ilike(like, escape="\\"),
                Product.description.ilike(like, escape="\\"),
                Ticket.ewc_code_display.ilike(like, escape="\\"),
                Ticket.ewc_code_6.ilike(like, escape="\\"),
                Ticket.ewc_description.ilike(like, escape="\\"),
                Ticket.product.has(
                    Product.ewc_code.has(
                        or_(
                            EwcCode.code_display.ilike(like, escape="\\"),
                            EwcCode.code_6.ilike(like, escape="\\"),
                            EwcCode.description.ilike(like, escape="\\"),
                        )
                    )
                ),
            )
        )
    elif ticket_no:
        ticket_like = f"%{ticket_no.lower()}%"
        filters.append(func.lower(Ticket.ticket_no).like(ticket_like))

    base_stmt = (
        select(Ticket, Vehicle, Customer, Product)
        .outerjoin(Vehicle, Ticket.vehicle_id == Vehicle.id)
        .outerjoin(Customer, Ticket.customer_id == Customer.id)
        .outerjoin(Product, Ticket.product_id == Product.id)
        .where(*filters)
    )
    count_stmt = (
        select(func.count(func.distinct(Ticket.id)))
        .select_from(Ticket)
        .outerjoin(Vehicle, Ticket.vehicle_id == Vehicle.id)
        .outerjoin(Customer, Ticket.customer_id == Customer.id)
        .outerjoin(Product, Ticket.product_id == Product.id)
        .where(*filters)
    )
    total_count = db.execute(count_stmt).scalar() or 0
    total_pages = max((total_count + page_size - 1) // page_size, 1)
    page = min(page, total_pages)

    status_priority = case(
        (Ticket.status == TicketStatusEnum.OPEN.value, 0),
        (Ticket.status == TicketStatusEnum.COMPLETE.value, 1),
        (Ticket.status == TicketStatusEnum.VOID.value, 2),
        else_=3,
    )
    rows = (
        db.execute(
            base_stmt.order_by(Ticket.datetime.desc(), status_priority.asc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
        .all()
    )

    return templates.TemplateResponse(request, 
        "tickets/list.html",
        {
            "request": request,
            "rows": rows,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "total_count": total_count,
            "reprint_sent": request.query_params.get("reprint_sent") == "1",
            "reprint_error": request.query_params.get("reprint_error", ""),
            "reprint_error_detail": request.query_params.get("reprint_error_detail", ""),
            "reprint_job_id": request.query_params.get("reprint_job_id", ""),
            "reprint_ticket_no": request.query_params.get("reprint_ticket_no", ""),
            "filters": {
                "date_from": date_from.isoformat() if date_from else "",
                "date_to": date_to.isoformat() if date_to else "",
                "status": status or "",
                "open_only": "1" if open_only else "",
                "direction": direction or "",
                "transaction_type": transaction_type or "",
                "walk_in_sale_only": "1" if walk_in_sale_only else "",
                "ticket_no": ticket_no or "",
                "q": search_query,
            },
        },
    )


@router.post("/tickets/print-last")
def tickets_print_last_again(
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    _require_tickets_manage(request)
    last_job = (
        db.execute(
            select(PrintJob)
            .where(
                PrintJob.status == "SENT",
                PrintJob.ticket_id.is_not(None),
                PrintJob.document_type == DOCUMENT_TYPE_TICKET,
            )
            .order_by(PrintJob.id.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    if last_job is None:
        return RedirectResponse(
            url="/tickets?reprint_error=No+successful+print+job+found+to+reprint.",
            status_code=303,
        )

    try:
        replay_result = replay_print_job(db, last_job)
    except (RuntimeError, ValueError, OSError, NotImplementedError) as exc:
        detail = str(exc) or "Print delivery failed."
        error_label = _friendly_print_error_label(detail)
        failed_job_id = _latest_print_job_id_for_ticket(
            db,
            ticket_id=int(last_job.ticket_id or 0),
            destination_id=last_job.destination_id,
            status="FAILED",
        )
        params = {
            "reprint_error": error_label,
            "reprint_error_detail": detail[:200],
        }
        if failed_job_id is not None:
            params["reprint_job_id"] = str(failed_job_id)
        return RedirectResponse(url=f"/tickets?{urlencode(params)}", status_code=303)

    params = {
        "reprint_sent": "1",
        "reprint_ticket_no": "",
        "reprint_job_id": str(replay_result.job.id),
    }
    if last_job.ticket_id:
        ticket = db.get(Ticket, last_job.ticket_id)
        if ticket and ticket.ticket_no:
            params["reprint_ticket_no"] = ticket.ticket_no
    return RedirectResponse(url=f"/tickets?{urlencode(params)}", status_code=303)


@router.get("/tickets/new", response_class=HTMLResponse)
def tickets_new(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    _require_tickets_manage(request)
    missing = missing_required_lookup_messages(db)
    if missing:
        return templates.TemplateResponse(
            request,
            "tickets/new_unavailable.html",
            {
                "request": request,
                "errors": missing,
            },
            status_code=503,
        )
    return tickets_quick_create(request, db)


@router.post("/tickets/new/quick", response_class=HTMLResponse)
def tickets_quick_create(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    _require_tickets_manage(request)
    now = utcnow()
    recent_cutoff = now - timedelta(seconds=NEW_TICKET_DEDUP_SECONDS)
    recent_ticket = (
        db.execute(
            select(Ticket)
            .where(
                Ticket.status == TicketStatusEnum.OPEN.value,
                Ticket.created_at >= recent_cutoff,
                Ticket.updated_at == Ticket.created_at,
                Ticket.direction == DirectionEnum.INWARD.value,
                Ticket.transaction_type == TransactionTypeEnum.WASTEIN.value,
                Ticket.customer_id.is_(None),
                Ticket.vehicle_id.is_(None),
                Ticket.product_id.is_(None),
                Ticket.haulier_id.is_(None),
                Ticket.driver_id.is_(None),
                Ticket.container_id.is_(None),
                Ticket.destination_id.is_(None),
                Ticket.gross_kg.is_(None),
                Ticket.tare_kg.is_(None),
                Ticket.net_kg.is_(None),
                Ticket.qty.is_(None),
                Ticket.unit_price.is_(None),
                Ticket.total.is_(None),
                Ticket.dont_invoice.is_(False),
                Ticket.paid.is_(False),
            )
            .order_by(Ticket.created_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    if recent_ticket:
        return RedirectResponse(url=f"/tickets/{recent_ticket.id}", status_code=303)
    ticket = Ticket(
        ticket_no=_generate_ticket_no(db, now),
        datetime=now.replace(second=0, microsecond=0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        waste_producer_source=WasteProducerSourceEnum.CUSTOMER.value,
        dont_invoice=False,
        paid=False,
    )
    db.add(ticket)
    db.flush()
    audit_log(
        db,
        request,
        action="CREATE",
        entity_type="ticket",
        entity_id=ticket.id,
        summary=f"Created ticket {ticket.ticket_no}",
        details={
            "ticket_no": ticket.ticket_no,
            "status": str(ticket.status),
            "direction": str(ticket.direction),
            "transaction_type": str(ticket.transaction_type),
        },
    )
    db.commit()
    return RedirectResponse(url=f"/tickets/{ticket.id}", status_code=303)


@router.get("/tickets/product-defaults", response_class=HTMLResponse)
def ticket_product_defaults(
    request: Request,
    product_id: str | None = Query(None),
    ticket_id: str | None = Query(None),
    customer_id: str | None = Query(None),
    qty: str | None = Query(None),
    gross_kg: str | None = Query(None),
    tare_kg: str | None = Query(None),
    net_kg: str | None = Query(None),
    readout_kg: str | None = Query(None),
    unit_price: str | None = Query(None),
    transaction_type: str | None = Query(None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    # Older ticket forms still send this field when requesting product defaults.
    _ = unit_price
    product_id_values = request.query_params.getlist("product_id")
    parsed_product_id = None
    for raw in product_id_values:
        candidate = _parse_int(str(raw).strip())
        if candidate is not None:
            parsed_product_id = candidate
            break
    if parsed_product_id is None and product_id is not None:
        parsed_product_id = _parse_int(str(product_id).strip())

    parsed_ticket_id = _parse_int(str(ticket_id).strip()) if ticket_id else None
    parsed_customer_id = _parse_int(str(customer_id).strip()) if customer_id else None
    apply_product_defaults = (
        _parse_bool_text(request.query_params.get("apply_product_defaults")) is True
    )
    requested_final_disposal = _parse_bool_text(
        request.query_params.get("ticket_final_disposal_value")
    )
    requested_used_on_site = _parse_bool_text(
        request.query_params.get("ticket_used_on_site_value")
    )

    if not parsed_product_id:
        return HTMLResponse("", status_code=204)

    product = (
        db.execute(
            select(Product)
            .options(joinedload(Product.unit))
            .where(Product.id == parsed_product_id)
        )
        .scalars()
        .first()
    )
    if not product:
        return HTMLResponse("", status_code=204)
    if not _product_has_active_unit(product):
        return templates.TemplateResponse(
            request,
            "tickets/_product_defaults_error.html",
            {"request": request, "error": INACTIVE_PRODUCT_UNIT_ERROR},
            status_code=400,
        )
    product_mismatch_error = _product_transaction_mismatch_error(
        product,
        (transaction_type or "").strip().upper(),
    )
    if product_mismatch_error:
        return templates.TemplateResponse(
            request,
            "tickets/_product_defaults_error.html",
            {"request": request, "error": product_mismatch_error},
            status_code=400,
        )

    weights_ticket = db.get(Ticket, parsed_ticket_id) if parsed_ticket_id else None
    effective_customer_id = (
        parsed_customer_id
        if parsed_customer_id is not None
        else (weights_ticket.customer_id if weights_ticket else None)
    )
    resolved_unit_price, using_price_override = resolve_unit_price_for_customer_product(
        db,
        customer_id=effective_customer_id,
        product=product,
    )
    unit_price_display = (
        f"{resolved_unit_price:.2f}" if resolved_unit_price is not None else ""
    )

    ewc = product.ewc_code if product else None
    unit_name = product.unit.name if product and product.unit else ""
    unit_type = product.unit.unit_type if product and product.unit else ""
    resolved_unit_type = (unit_type or "").upper()
    qty_value = qty.strip() if qty is not None else ""
    if resolved_unit_type == "WEIGHT":
        qty_value = ""

    weights_form = None
    weights_is_open = False
    if weights_ticket is not None:
        gross_raw = (gross_kg or "").strip()
        tare_raw = (tare_kg or "").strip()
        gross_value = _parse_float(gross_raw)
        tare_value = _parse_float(tare_raw)
        net_value = _parse_float((net_kg or "").strip())
        if net_value is None and gross_value is not None and tare_value is not None:
            net_value = gross_value - tare_value
        weights_form = _weights_form_from_values(
            ticket=weights_ticket,
            gross_raw=gross_raw,
            tare_raw=tare_raw,
            net_value=net_value,
        )
        weights_form["product_id"] = str(parsed_product_id or weights_ticket.product_id or "")
        weights_is_open = _is_open_ticket(weights_ticket)

    final_disposal_value, used_on_site_value = _resolve_ticket_operation_flag_values(
        product=product,
        ticket=weights_ticket,
        apply_product_defaults=apply_product_defaults,
        requested_final_disposal=requested_final_disposal,
        requested_used_on_site=requested_used_on_site,
    )

    return templates.TemplateResponse(request, 
        "tickets/_product_defaults.html",
        {
            "request": request,
            "ewc_code_6": ewc.code_6 if ewc else None,
            "unit_price": unit_price_display,
            "price_override_active": using_price_override,
            "price_override_unit_price": resolved_unit_price,
            "ewc_code_display": ewc.code_display if ewc else None,
            "ewc_description": ewc.description if ewc else None,
            "ewc_hazardous": bool(ewc.hazardous) if ewc else False,
            "default_destination_id": product.default_destination_id,
            "unit_type": unit_type,
            "unit_name": unit_name,
            "transaction_type": transaction_type or "",
            "resolved_unit_type": resolved_unit_type,
            "qty_value": qty_value,
            "oob_qty": True,
            "weights_ticket": weights_ticket,
            "weights_form": weights_form,
            "weights_is_open": weights_is_open,
            "weights_locked": resolved_unit_type == "COUNT",
            "ticket_operation_form": _ticket_operation_form_values(
                final_disposal=final_disposal_value,
                used_on_site=used_on_site_value,
            ),
            "ticket_operation": _ticket_operation_context(
                final_disposal=final_disposal_value,
                used_on_site=used_on_site_value,
            ),
            "oob_operation_flags": True,
            "oob_ticket_operation_hints": True,
        },
    )


@router.get("/tickets/product-options", response_class=HTMLResponse)
def tickets_product_options(
    request: Request,
    transaction_type: str | None = Query(None),
    product_id: str | None = Query(None),
    ticket_id: str | None = Query(None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    product_id_values = request.query_params.getlist("product_id")
    parsed_product_id = None
    for raw in product_id_values:
        candidate = _parse_int(str(raw).strip())
        if candidate is not None:
            parsed_product_id = candidate
            break
    if parsed_product_id is None and product_id is not None:
        parsed_product_id = _parse_int(str(product_id).strip())

    parsed_ticket_id = _parse_int(str(ticket_id).strip()) if ticket_id else None
    requested_final_disposal = _parse_bool_text(
        request.query_params.get("ticket_final_disposal_value")
    )
    requested_used_on_site = _parse_bool_text(
        request.query_params.get("ticket_used_on_site_value")
    )
    ticket_operation = _resolve_ticket_operation_context(
        db=db,
        ticket_id=parsed_ticket_id,
        requested_final_disposal=requested_final_disposal,
        requested_used_on_site=requested_used_on_site,
    )
    form_data = {"product_id": str(parsed_product_id or "")}
    options = _load_ticket_options_with_enums(
        db,
        transaction_type=transaction_type,
        selected_product_id=parsed_product_id,
    )
    product_usage_warning = _sales_only_selected_product_warning(
        db, transaction_type, parsed_product_id
    )
    return templates.TemplateResponse(
        request,
        "tickets/_product_field.html",
        {
            "request": request,
            "ticket_id": parsed_ticket_id or "",
            "form": form_data,
            "options": options,
            "product_unit_meta": _load_product_unit_meta(db),
            "product_usage_warning": product_usage_warning,
            "product_empty_option_label": _product_option_empty_label(transaction_type),
            "oob_product_warning": True,
            "ticket_operation": ticket_operation,
            "oob_ticket_operation_hints": True,
        },
    )


def _ticket_direction_options() -> list[tuple[str, str]]:
    return [
        ("INWARD", "Inward"),
        ("OUTWARD", "Outward"),
    ]


def _ticket_transaction_type_options_by_direction() -> dict[str, list[tuple[str, str]]]:
    return {
        "INWARD": [("WASTEIN", "Waste In")],
        "OUTWARD": [
            ("WASTEOUT", "Waste Out"),
            ("SALE", "Sale"),
        ],
    }


def _ticket_transaction_type_options(
    direction: str | None = None,
    selected_transaction_type: str | None = None,
) -> list[tuple[str, str]]:
    options_by_direction = _ticket_transaction_type_options_by_direction()
    all_options = [
        option
        for direction_options in options_by_direction.values()
        for option in direction_options
    ]
    direction_value = _enum_value_or_text(direction).upper() if direction else ""
    options = list(options_by_direction.get(direction_value, all_options))

    selected_value = _enum_value_or_text(selected_transaction_type).upper()
    if selected_value and all(value != selected_value for value, _label in options):
        for value, label in all_options:
            if value == selected_value:
                options.append((value, label))
                break
    return options


def _load_ticket_options_with_enums(
    db: Session,
    *,
    transaction_type: str | None = None,
    selected_product_id: int | None = None,
    direction: str | None = None,
    selected_transaction_type: str | None = None,
) -> dict:
    return {
        **_load_ticket_options(
            db,
            transaction_type=transaction_type,
            selected_product_id=selected_product_id,
        ),
        "directions": _ticket_direction_options(),
        "transaction_types": _ticket_transaction_type_options(
            direction=direction,
            selected_transaction_type=selected_transaction_type,
        ),
        "transaction_types_by_direction": _ticket_transaction_type_options_by_direction(),
    }


def _load_product_unit_meta(db: Session | None) -> dict[str, dict[str, str]]:
    if db is None:
        return {}
    products = db.execute(
        select(Product)
        .options(joinedload(Product.unit), joinedload(Product.ewc_code))
        .order_by(Product.id)
    ).scalars().all()
    meta: dict[str, dict[str, str]] = {}
    for product in products:
        unit = product.unit
        ewc = product.ewc_code
        meta[str(product.id)] = {
            "unit_type": (unit.unit_type if unit and unit.unit_type else ""),
            "unit_name": (unit.name if unit and unit.name else ""),
            "ewc_code_6": (ewc.code_6 if ewc and ewc.code_6 else ""),
            "ewc_code_display": (ewc.code_display if ewc and ewc.code_display else ""),
            "ewc_hazardous": "1" if ewc and ewc.hazardous else "0",
        }
    return meta


@router.get("/tickets/vehicle-suggest", response_class=HTMLResponse)
def tickets_vehicle_suggest(
    request: Request,
    reg: str | None = Query(None),
    ticket_id: int | None = Query(None),
    walk_in: str | None = Query(None),
    walk_in_sale: str | None = Query(None),
    direction: str | None = Query(None),
    customer_id: str | None = Query(None),
    haulier_id: str | None = Query(None),
    driver_id: str | None = Query(None),
    gross_kg: str | None = Query(None),
    tare_kg: str | None = Query(None),
    readout_kg: str | None = Query(None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    # Older UI flows still include these inputs in the suggest request payload.
    _ = (direction, gross_kg, tare_kg, readout_kg)
    if not reg or not ticket_id:
        return HTMLResponse("", status_code=204)
    if walk_in and str(walk_in).lower() in ("1", "true", "on", "yes"):
        return HTMLResponse("", status_code=204)
    if walk_in_sale and str(walk_in_sale).lower() in ("1", "true", "on", "yes"):
        return HTMLResponse("", status_code=204)

    ticket = db.get(Ticket, ticket_id)
    if not ticket or _is_ticket_locked(ticket):
        return HTMLResponse("", status_code=204)
    if ticket.walk_in:
        return HTMLResponse("", status_code=204)

    vehicle = _find_vehicle_by_reg(db, reg)
    if not vehicle:
        return HTMLResponse("", status_code=204)

    default_customer = (
        db.get(Customer, vehicle.default_customer_id)
        if vehicle.default_customer_id
        else None
    )
    default_haulier = (
        db.get(Haulier, vehicle.default_haulier_id)
        if vehicle.default_haulier_id
        else None
    )
    default_driver = (
        db.get(Driver, vehicle.default_driver_id)
        if vehicle.default_driver_id
        else None
    )
    selected_customer_id = _parse_int(str(customer_id or "").strip())
    selected_haulier_id = _parse_int(str(haulier_id or "").strip())
    selected_driver_id = _parse_int(str(driver_id or "").strip())

    suggested_tare_kg = None
    if vehicle.default_tare_kg is not None:
        tare_value = Decimal(str(vehicle.default_tare_kg))
        if tare_value > 0:
            suggested_tare_kg = tare_value

    errors: list[str] = []
    warnings: list[str] = []
    if default_customer and default_customer.on_stop:
        warnings.append(
            "Customer is ON STOP - allowed to record ticket; cannot complete/invoice."
        )
    if default_haulier and _is_haulier_on_stop(default_haulier):
        warnings.append(
            "Haulier is ON STOP - allowed to record ticket; cannot complete/invoice."
        )
    if default_haulier and not default_haulier.is_active:
        errors.append("Suggested haulier is inactive.")
    if default_driver and not default_driver.is_active:
        errors.append("Suggested driver is inactive.")

    return _render_vehicle_suggestions(
        request,
        ticket,
        vehicle,
        default_customer,
        default_haulier,
        default_driver,
        suggested_tare_kg=suggested_tare_kg,
        ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
        current_customer_id=selected_customer_id,
        current_haulier_id=selected_haulier_id,
        current_driver_id=selected_driver_id,
        oob_customer_options=None,
        oob_haulier_options=None,
        oob_driver_options=None,
        oob_weights_form=None,
        oob_tare_auto_hint=None,
        warnings=warnings,
        errors=errors,
    )


def _generate_ticket_no(db: Session, now: datetime | None = None) -> str:
    current_time = now or utcnow()
    tenant_id = db.info.get("tenant_id")
    if tenant_id is None:
        raise RuntimeError("Ticket number generation requires tenant scope.")
    tenant_id = int(tenant_id)
    year = current_time.year
    year_prefix = f"{str(year)[2:]}"
    year_start = datetime(year, 1, 1)
    next_year_start = datetime(year + 1, 1, 1)
    highest_existing_ticket_no = db.execute(
        select(Ticket.ticket_no)
        .where(
            Ticket.tenant_id == tenant_id,
            Ticket.datetime >= year_start,
            Ticket.datetime < next_year_start,
            Ticket.ticket_no.like(f"{year_prefix}-%"),
        )
        .order_by(Ticket.ticket_no.desc())
        .limit(1)
    ).scalar_one_or_none()
    starting_number = 1
    if highest_existing_ticket_no:
        match = re.fullmatch(r"(\d{2})-(\d{5})", str(highest_existing_ticket_no).strip())
        if match and match.group(1) == year_prefix:
            starting_number = int(match.group(2)) + 1

    next_number = db.execute(
        text(
            "INSERT INTO ticket_sequences (tenant_id, year, last_number, updated_at) "
            "VALUES (:tenant_id, :year, :starting_number, :updated_at) "
            "ON CONFLICT (tenant_id, year) DO UPDATE "
            "SET last_number = ticket_sequences.last_number + 1, updated_at = :updated_at "
            "RETURNING last_number"
        ),
        {
            "tenant_id": tenant_id,
            "year": year,
            "starting_number": starting_number,
            "updated_at": current_time,
        },
    ).scalar_one()

    return f"{str(year)[2:]}-{next_number:05d}"


def _load_ticket_options(
    db: Session | None,
    *,
    transaction_type: str | None = None,
    selected_product_id: int | None = None,
) -> dict[str, list[tuple[int, str]]]:
    if db is None:
        return {key: [] for key in _option_keys()}

    def as_options(rows, label_fn):
        return [(str(row.id), label_fn(row)) for row in rows]

    resolved_transaction_type = _enum_value_or_text(transaction_type).upper()
    products_stmt = (
        select(Product)
        .options(joinedload(Product.unit))
        .join(Unit, Product.unit_id == Unit.id)
        .where(Unit.is_active.is_(True))
        .order_by(Product.description)
    )
    product_filter = _product_filter_for_transaction(resolved_transaction_type)
    if product_filter is not None:
        products_stmt = products_stmt.where(product_filter)
    product_rows = db.execute(products_stmt).scalars().all()
    product_options = as_options(product_rows, lambda row: row.description)
    selected_product_id_str = str(selected_product_id or "")
    has_selected_product = any(
        option_id == selected_product_id_str for option_id, _ in product_options
    )
    if selected_product_id and not has_selected_product:
        selected_product = (
            db.execute(
                select(Product)
                .options(joinedload(Product.unit))
                .where(Product.id == selected_product_id)
            )
            .scalars()
            .first()
        )
        if selected_product and _product_has_active_unit(selected_product):
            selected_label = selected_product.description
            mismatch_warning = _product_transaction_mismatch_error(
                selected_product,
                resolved_transaction_type,
                warning=True,
            )
            if mismatch_warning == SALES_ONLY_WASTE_WARNING:
                selected_label = f"{selected_label} (sales only)"
            elif mismatch_warning == WASTE_ONLY_SALE_WARNING:
                selected_label = f"{selected_label} (waste only)"
            elif mismatch_warning:
                selected_label = (
                    f"{selected_label} (not available for this transaction type)"
                )
            product_options = [
                (
                    str(selected_product.id),
                    selected_label,
                )
            ] + product_options

    def _active_void_reasons_seeded() -> list[VoidReason]:
        reasons = (
            db.execute(
                select(VoidReason)
                .where(
                    VoidReason.is_active.is_(True),
                    func.upper(VoidReason.reason_type) == VOID_REASON_TYPE_TICKET,
                )
                .order_by(VoidReason.code)
            )
            .scalars()
            .all()
        )
        if reasons:
            return reasons
        # Keep voiding operational in clean databases and long-running dev sessions.
        seed_void_reasons(db)
        return (
            db.execute(
                select(VoidReason)
                .where(
                    VoidReason.is_active.is_(True),
                    func.upper(VoidReason.reason_type) == VOID_REASON_TYPE_TICKET,
                )
                .order_by(VoidReason.code)
            )
            .scalars()
            .all()
        )

    return {
        "customers": as_options(
            db.execute(select(Customer).order_by(Customer.name)).scalars().all(),
            lambda row: row.name,
        ),
        "vehicles": as_options(
            db.execute(select(Vehicle).order_by(Vehicle.registration)).scalars().all(),
            lambda row: row.registration,
        ),
        "products": product_options,
        "hauliers": as_options(
            db.execute(select(Haulier).order_by(Haulier.name)).scalars().all(),
            lambda row: row.name,
        ),
        "drivers": as_options(
            db.execute(select(Driver).order_by(Driver.name)).scalars().all(),
            lambda row: row.name,
        ),
        "containers": as_options(
            db.execute(select(Container).order_by(Container.name)).scalars().all(),
            lambda row: row.name,
        ),
        "destinations": as_options(
            db.execute(select(Destination).order_by(Destination.name)).scalars().all(),
            lambda row: row.name,
        ),
        "ewc_codes": [
            (row.code_display, row.description, row.code_6, bool(row.hazardous))
            for row in db.execute(
                select(EwcCode)
                .where(EwcCode.active.is_(True))
                .order_by(EwcCode.code_6)
            )
            .scalars()
            .all()
        ],
        "yards": as_options(
            db.execute(select(Yard).order_by(Yard.code)).scalars().all(),
            lambda row: row.code,
        ),
        "areas": as_options(
            db.execute(select(Area).order_by(Area.code)).scalars().all(),
            lambda row: row.code,
        ),
        "void_reasons": as_options(
            _active_void_reasons_seeded(),
            lambda row: row.description or row.code,
        ),
    }


def _active_lookup_options(ticket: Ticket, db: Session) -> dict[str, list[tuple[str, str]]]:
    def active_options(model, current_id):
        rows = (
            db.execute(
                select(model)
                .where(model.is_active.is_(True))
                .order_by(model.name)
            )
            .scalars()
            .all()
        )
        options = [(str(row.id), row.name) for row in rows]
        if current_id is None:
            return options

        if any(str(row_id) == str(current_id) for row_id, _ in options):
            return options

        current = db.get(model, current_id)
        if current is None:
            return options

        label = f"{current.name} (inactive)"
        return [(str(current.id), label)] + options

    return {
        "hauliers": active_options(Haulier, ticket.haulier_id),
        "drivers": active_options(Driver, ticket.driver_id),
        "containers": active_options(Container, ticket.container_id),
        "destinations": active_options(Destination, ticket.destination_id),
    }


def _oob_lookup_options(
    ticket: Ticket, db: Session
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    options = _load_ticket_options(db)
    active = _active_lookup_options(ticket, db)
    return options["customers"], active["hauliers"]


def _option_keys() -> list[str]:
    return [
        "customers",
        "vehicles",
        "products",
        "hauliers",
        "drivers",
        "containers",
        "destinations",
        "ewc_codes",
        "yards",
        "areas",
        "void_reasons",
    ]


def _enum_value_or_text(value: object) -> str:
    if value is None:
        return ""
    return value.value if hasattr(value, "value") else str(value)


def _sales_only_selected_product_warning(
    db: Session,
    transaction_type: object,
    selected_product_id: int | None,
) -> str | None:
    if not selected_product_id:
        return None
    selected_product = db.get(Product, selected_product_id)
    if not selected_product:
        return None
    return _product_transaction_mismatch_error(
        selected_product,
        _enum_value_or_text(transaction_type),
        warning=True,
    )


def _normalize_registration(value: str | None) -> str:
    if not value:
        return ""
    cleaned = re.sub(r"\s+", "", str(value)).upper()
    return cleaned


def _normalize_reg_text(value: str | None) -> str:
    if not value:
        return ""
    cleaned = re.sub(r"\s+", "", str(value)).upper()
    return cleaned


def _find_vehicle_by_reg(db: Session, reg: str) -> Vehicle | None:
    normalized = _normalize_registration(reg)
    if not normalized:
        return None
    return (
        db.execute(
            select(Vehicle).where(
                func.replace(func.upper(Vehicle.registration), " ", "") == normalized
            )
        )
        .scalars()
        .first()
    )


def _ticket_vehicle_reg(db: Session, ticket: Ticket) -> str:
    if ticket.vehicle_id:
        vehicle = db.get(Vehicle, ticket.vehicle_id)
        if vehicle and vehicle.registration:
            return vehicle.registration
    return ticket.vehicle_reg_text or ""


async def _resolve_vehicle_for_defaults(
    request: Request, ticket: Ticket, db: Session
) -> Vehicle | None:
    if ticket.vehicle_id:
        return db.get(Vehicle, ticket.vehicle_id)
    if ticket.walk_in:
        return None

    form = await request.form()
    if _form_value(form, "walk_in") == "on":
        return None
    reg = str(form.get("reg", "")).strip()
    if not reg:
        return None
    vehicle = _find_vehicle_by_reg(db, reg)
    if vehicle:
        ticket.vehicle_id = vehicle.id
    return vehicle


def _status_value(value) -> str:
    if value is None:
        return ""
    return value.value if hasattr(value, "value") else str(value)


def _is_ticket_locked(ticket: Ticket) -> bool:
    return _status_value(ticket.status) in LOCKED_STATUSES


def _can_void_ticket(ticket: Ticket) -> bool:
    return (
        _status_value(ticket.status) == TicketStatusEnum.COMPLETE.value
        and ticket.invoice_id is None
    )


def _ticket_locked_message(ticket: Ticket) -> str:
    if _status_value(ticket.status) == TicketStatusEnum.VOID.value:
        return "This ticket is void and cannot be edited."
    return "This ticket is complete and cannot be edited."


def _ticket_void_blocked_message(ticket: Ticket) -> str:
    if ticket.invoice_id is not None:
        return "Cannot void a ticket that has already been invoiced."
    if _status_value(ticket.status) == TicketStatusEnum.VOID.value:
        return "This ticket is void and cannot be edited."
    if _status_value(ticket.status) != TicketStatusEnum.COMPLETE.value:
        return "Only complete tickets can be voided."
    return "Cannot void this ticket."


def _ensure_ticket_void_reasons(db: Session) -> None:
    # Keep ticket voiding operational in clean databases.
    seed_void_reasons(db)


def _latest_ticket_void_with_reason(
    db: Session, ticket_id: int
) -> tuple[TicketVoid | None, VoidReason | None]:
    row = db.execute(
        select(TicketVoid, VoidReason)
        .outerjoin(VoidReason, TicketVoid.reason_id == VoidReason.id)
        .where(TicketVoid.ticket_id == ticket_id)
        .order_by(TicketVoid.voided_at.desc(), TicketVoid.id.desc())
        .limit(1)
    ).first()
    if not row:
        return None, None
    return row[0], row[1]


def _is_haulier_on_stop(haulier: Haulier | None) -> bool:
    return bool(getattr(haulier, "on_stop", False)) if haulier else False


def _apply_walk_in_sale_mode(payload: dict) -> None:
    transaction_type = payload.get("transaction_type")
    is_sale = transaction_type == TransactionTypeEnum.SALE.value
    walk_in_sale = bool(payload.get("walk_in_sale"))

    form_data = payload.get("form")
    if not isinstance(form_data, dict):
        form_data = {}
        payload["form"] = form_data

    if walk_in_sale and not is_sale:
        payload["errors"].append(WALK_IN_SALE_ONLY_ERROR)
        payload["walk_in_sale"] = False
        form_data["walk_in_sale"] = ""
        return

    if not walk_in_sale:
        return

    payload["customer_id"] = None
    form_data["customer_id"] = ""
    payload["dont_invoice"] = True
    form_data["dont_invoice"] = "on"

    for key in ("haulier_id", "driver_id", "container_id", "destination_id", "area_id"):
        payload[key] = None
        form_data[key] = ""
    payload["area_id_present"] = True

    payload["waste_code_id"] = None
    form_data["waste_code_id"] = ""
    payload["waste_producer_same_as_customer"] = True
    form_data["waste_producer_same_as_customer"] = "on"
    for key in (
        "waste_producer_name",
        "waste_producer_address_line_1",
        "waste_producer_address_line_2",
        "waste_producer_address_line_3",
        "waste_producer_postcode",
    ):
        payload[key] = ""
        form_data[key] = ""

    payload["ewc_code_raw"] = ""
    payload["ewc_code_6"] = None
    payload["ewc_manual_override"] = False
    payload["ewc_hazardous"] = False
    payload["ewc_auto_code_6"] = ""
    payload["ewc_product_default_code_6"] = ""
    payload["ewc_product_default_display"] = ""
    form_data["ewc_code"] = ""
    form_data["ewc_manual_override"] = "0"
    form_data["ewc_hazardous"] = "0"
    form_data["ewc_auto_code_6"] = ""


def _is_other_void_reason(reason: VoidReason | None) -> bool:
    if not reason:
        return False
    code = (reason.code or "").strip().lower()
    description = (reason.description or "").strip().lower()
    return code == "other" or description == "other"


def _on_stop_blockers(
    db: Session, customer_id: int | None, haulier_id: int | None
) -> list[str]:
    blockers: list[str] = []
    if customer_id:
        customer = db.get(Customer, customer_id)
        if customer and customer.on_stop:
            blockers.append("Customer")
    if haulier_id:
        haulier = db.get(Haulier, haulier_id)
        if _is_haulier_on_stop(haulier):
            blockers.append("Haulier")
    return blockers


def _on_stop_completion_error(blockers: list[str]) -> str:
    if not blockers:
        return ""
    if len(blockers) == 1:
        subject = blockers[0]
    else:
        subject = "Customer and Haulier"
    return (
        f"Cannot complete ticket: {subject} is ON STOP. "
        "You may record the ticket, but it cannot be completed/invoiced until stop is removed."
    )


def _on_stop_banner_message(blockers: list[str]) -> str:
    if not blockers:
        return ""
    if len(blockers) == 1:
        subject = blockers[0]
    else:
        subject = "Customer and Haulier"
    return (
        f"{subject} is ON STOP - allowed to record ticket; cannot complete/invoice."
    )


def _customer_invoice_warning_flags(
    db: Session,
    customer_id: int | None,
    po_number: str | None,
    *,
    ticket_status: str | None = None,
) -> tuple[bool, bool]:
    if not customer_id:
        return False, False
    customer = db.get(Customer, customer_id)
    if not customer:
        return False, False
    status_value = _status_value(ticket_status)
    if status_value == TicketStatusEnum.VOID.value:
        return bool(customer.do_not_invoice), False
    if status_value != TicketStatusEnum.COMPLETE.value:
        return bool(customer.do_not_invoice), False
    po_text = (po_number or "").strip()
    return bool(customer.do_not_invoice), bool(customer.must_have_po and not po_text)


def _expected_weigh_in_field(direction) -> str:
    direction_value = _status_value(direction)
    return "tare_kg" if direction_value == DirectionEnum.OUTWARD.value else "gross_kg"


def _validate_weighing_order(
    direction, gross_kg: float | None, tare_kg: float | None
) -> list[str]:
    if gross_kg is None and tare_kg is None:
        return []

    expected_weigh_in = _expected_weigh_in_field(direction)
    if expected_weigh_in == "gross_kg" and tare_kg is not None and gross_kg is None:
        return ["Weigh-in (gross) is required before tare."]
    if expected_weigh_in == "tare_kg" and gross_kg is not None and tare_kg is None:
        return ["Weigh-in (tare) is required before gross."]
    return []


def _is_open_ticket(ticket: Ticket) -> bool:
    return _status_value(ticket.status) == TicketStatusEnum.OPEN.value


def _ticket_dev_preview_enabled() -> bool:
    explicit_flag = templates.env.globals.get("DEV_MODE")
    if explicit_flag is not None:
        return bool(explicit_flag)
    return bool(settings.dev_mode or settings.debug)


def _is_ticket_invoiced(ticket: Ticket, db: Session | None = None) -> bool:
    if ticket.invoice_id is not None:
        return True
    if not db or ticket.id is None:
        return False
    return (
        db.execute(
            select(InvoiceLine.id)
            .where(InvoiceLine.ticket_id == ticket.id)
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


def _can_update_po(ticket: Ticket, db: Session | None = None) -> bool:
    status_value = _status_value(ticket.status)
    return (
        status_value in PO_UPDATE_ALLOWED_STATUSES
        and status_value != TicketStatusEnum.VOID.value
        and not _is_ticket_invoiced(ticket, db)
    )


def _can_edit_complete_po(ticket: Ticket, db: Session | None = None) -> bool:
    return (
        _status_value(ticket.status) == TicketStatusEnum.COMPLETE.value
        and _can_update_po(ticket, db)
    )


def _po_locked_invoiced(ticket: Ticket, db: Session | None = None) -> bool:
    return (
        _status_value(ticket.status) == TicketStatusEnum.COMPLETE.value
        and _is_ticket_invoiced(ticket, db)
    )


def _compliance_locked_invoiced(ticket: Ticket, db: Session | None = None) -> bool:
    return _is_ticket_invoiced(ticket, db)


def _is_ticket_compliance_hazardous(ticket: Ticket) -> bool:
    ewc_display = str(ticket.ewc_code_display or "").strip()
    if "*" in ewc_display:
        return True
    if bool(ticket.ewc_hazardous):
        return True
    product_ewc = ticket.product.ewc_code if ticket.product else None
    return bool(getattr(product_ewc, "hazardous", False))


def _compliance_fields_present_in_form(form) -> bool:
    compliance_keys = (
        "ewc_code",
        "ewc_manual_override",
        "ewc_auto_code_6",
        "ewc_product_default_code_6",
        "ewc_product_default_display",
        "ewc_hazardous",
        "waste_producer_same_as_customer",
        "waste_producer_same_as_customer_present",
        "waste_producer_name",
        "waste_producer_address_line_1",
        "waste_producer_address_line_2",
        "waste_producer_address_line_3",
        "waste_producer_postcode",
    )
    return any(key in form for key in compliance_keys)


def _freeze_lookup_fields(ticket: Ticket, payload: dict) -> None:
    for key in ("haulier_id", "driver_id", "container_id", "destination_id"):
        current_value = getattr(ticket, key)
        payload[key] = current_value
        form_data = payload.get("form")
        if isinstance(form_data, dict):
            form_data[key] = str(current_value or "")


def _validate_lookup_fields(
    ticket: Ticket, payload: dict, db: Session
) -> list[str]:
    if not _is_open_ticket(ticket):
        _freeze_lookup_fields(ticket, payload)
        return []

    errors: list[str] = []
    form_data = payload.get("form") if isinstance(payload.get("form"), dict) else None
    checks = (
        ("haulier_id", Haulier, "Haulier"),
        ("driver_id", Driver, "Driver"),
        ("container_id", Container, "Container"),
        ("destination_id", Destination, "Destination"),
    )
    for field, model, label in checks:
        raw_value = payload.get(field)
        if raw_value in (None, ""):
            payload[field] = None
            if form_data is not None:
                form_data[field] = ""
            continue
        if isinstance(raw_value, int):
            value = raw_value
        else:
            try:
                value = int(raw_value)
            except (TypeError, ValueError):
                errors.append(f"{label} not found.")
                payload[field] = None
                if form_data is not None:
                    form_data[field] = ""
                continue
        payload[field] = value
        if form_data is not None:
            form_data[field] = str(value)
        record = db.get(model, value)
        if not record:
            errors.append(f"{label} not found.")
            continue
        if not record.is_active and value != getattr(ticket, field):
            errors.append(f"{label} is inactive.")

    return errors


def _validate_ticket_entity_references(
    ticket: Ticket,
    payload: dict,
    db: Session,
) -> list[str]:
    errors: list[str] = []
    form_data = payload.get("form") if isinstance(payload.get("form"), dict) else {}
    checks = (
        ("customer_id", Customer, "Customer"),
        ("vehicle_id", Vehicle, "Vehicle"),
        ("yard_id", Yard, "Yard"),
        ("area_id", Area, "Area"),
        ("unit_id", Unit, "Unit"),
        ("waste_code_id", WasteCode, "Waste code"),
    )
    for field, model, label in checks:
        raw_value = payload.get(field)
        if raw_value in (None, ""):
            payload[field] = None
            form_data[field] = ""
            continue
        try:
            record_id = int(raw_value)
        except (TypeError, ValueError):
            errors.append(f"{label} not found.")
            payload[field] = None
            form_data[field] = ""
            continue

        record = db.get(model, record_id)
        if not record:
            errors.append(f"{label} not found.")
            payload[field] = None
            form_data[field] = ""
            continue

        if (
            hasattr(record, "is_active")
            and not bool(getattr(record, "is_active", False))
            and int(record_id) != int(getattr(ticket, field, 0) or 0)
        ):
            errors.append(f"{label} is inactive.")

    return errors


def _request_expects_json(request: Request) -> bool:
    content_type = request.headers.get("content-type", "").lower()
    accept = request.headers.get("accept", "").lower()
    return "application/json" in content_type or "application/json" in accept


def _ticket_destination_display_name(destination: PrintDestination) -> str:
    description = str(destination.description or "").strip()
    if description:
        return description
    name = str(destination.name or "").strip()
    return name or f"Destination {destination.id}"


def _ticket_qz_printer_name(destination: PrintDestination | None) -> str:
    if destination is None:
        return ""
    return qz_printer_name_from_delivery_config(
        destination.delivery_config if isinstance(destination.delivery_config, dict) else {}
    )


def _ticket_qz_direct_print_enabled(destination: PrintDestination | None) -> bool:
    if destination is None:
        return False
    return qz_direct_print_enabled_from_destination(
        delivery_type=destination.delivery_type,
        delivery_config=destination.delivery_config if isinstance(destination.delivery_config, dict) else {},
        local_browser_delivery_type=DELIVERY_TYPE_PRINT_LOCAL_BROWSER,
    )


def _friendly_print_error_label(raw_message: str) -> str:
    normalized = str(raw_message or "").strip().lower()
    if any(
        marker in normalized
        for marker in (
            "refused",
            "timed out",
            "timeout",
            "unreachable",
            "no route to host",
            "getaddrinfo",
            "name or service not known",
            "temporary failure in name resolution",
        )
    ):
        return "Output unavailable"
    return "Print failed"


def _latest_print_job_id_for_ticket(
    db: Session,
    *,
    ticket_id: int,
    destination_id: int | None = None,
    document_type: str | None = None,
    status: str | None = None,
) -> int | None:
    query = select(PrintJob.id).where(PrintJob.ticket_id == ticket_id)
    if destination_id is not None:
        query = query.where(PrintJob.destination_id == destination_id)
    if document_type:
        query = query.where(PrintJob.document_type == document_type)
    if status:
        query = query.where(PrintJob.status == status)
    row = db.execute(query.order_by(PrintJob.id.desc()).limit(1)).first()
    return int(row[0]) if row else None


def _load_active_ticket_print_destinations(db: Session) -> list[PrintDestination]:
    return list(
        db.execute(
            select(PrintDestination)
            .where(
                PrintDestination.is_active.is_(True),
                PrintDestination.document_type == DOCUMENT_TYPE_TICKET,
            )
            .order_by(
                PrintDestination.is_default.desc(),
                PrintDestination.name.asc(),
            )
        ).scalars()
    )


def _default_ticket_print_destination(
    destinations: list[PrintDestination],
) -> PrintDestination | None:
    return next((row for row in destinations if row.is_default), None)


def _resolve_ticket_print_destination(
    db: Session,
    *,
    destination_id: int | None = None,
    require_default: bool = False,
) -> PrintDestination | None:
    destinations = _load_active_ticket_print_destinations(db)
    if destination_id is not None:
        selected = next((row for row in destinations if row.id == destination_id), None)
        if selected is None:
            raise ValueError("Destination not found or inactive.")
        return selected
    default_destination = _default_ticket_print_destination(destinations)
    if require_default and default_destination is None:
        raise ValueError("Printing is not configured. Contact admin.")
    return default_destination


def _load_active_wtn_destinations(db: Session) -> list[PrintDestination]:
    return list(
        db.execute(
            select(PrintDestination)
            .where(
                PrintDestination.is_active.is_(True),
                PrintDestination.document_type == DOCUMENT_TYPE_WTN,
            )
            .order_by(
                PrintDestination.is_default.desc(),
                PrintDestination.name.asc(),
            )
        ).scalars()
    )


def _default_wtn_destination(
    destinations: list[PrintDestination],
) -> PrintDestination | None:
    return next((row for row in destinations if row.is_default), None)


def _resolve_wtn_destination(
    db: Session,
    *,
    require_default: bool = False,
) -> PrintDestination | None:
    destinations = _load_active_wtn_destinations(db)
    default_destination = _default_wtn_destination(destinations)
    if require_default and default_destination is None:
        raise ValueError("Sending is not set up yet. Ask an admin.")
    return default_destination


def _resolve_wtn_default_template(db: Session) -> PrintTemplate | None:
    for code in ("wtn_system",):
        template = (
            db.execute(
                select(PrintTemplate)
                .where(
                    func.lower(PrintTemplate.code) == code,
                    PrintTemplate.document_type == DOCUMENT_TYPE_WTN,
                    PrintTemplate.is_active.is_(True),
                )
                .order_by(PrintTemplate.id.asc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        if template is not None:
            return template
    return None


def _ticket_print_actions_context(
    db: Session,
    *,
    ticket: Ticket,
) -> dict[str, object]:
    destinations = _load_active_ticket_print_destinations(db)
    default_destination = _default_ticket_print_destination(destinations)
    send_enabled = default_destination is not None
    primary_name = (
        _ticket_destination_display_name(default_destination)
        if default_destination is not None
        else ""
    )
    ticket_id = int(ticket.id)
    ticket_label = str(ticket.ticket_no or f"#{ticket_id}")
    download_url = f"/tickets/{ticket_id}/pdf" if send_enabled else ""
    platform_qz_ready = platform_qz_ready_for_tenants(db)
    qz_direct_print_enabled = _ticket_qz_direct_print_enabled(default_destination)
    qz_printer_name = _ticket_qz_printer_name(default_destination)
    return {
        "has_print_destinations": bool(destinations),
        "print_primary_destination_name": primary_name,
        "print_missing_default": not send_enabled,
        "print_actions": {
            "entity_type": "ticket",
            "entity_id": int(ticket_id),
            "send_url": f"/tickets/{ticket_id}/print",
            "preview_url": f"/tickets/{ticket_id}/preview",
            "send_enabled": send_enabled,
            "send_label": "Print",
            "send_button_variant": "primary",
            "preview_label": "Preview",
            "preview_button_variant": "secondary",
            "preview_button_id": "preview_browser_print_button",
            "download_url": download_url,
            "download_label": "Download PDF",
            "download_button_variant": "secondary",
            "qz_print": {
                "enabled": bool(send_enabled and platform_qz_ready and qz_direct_print_enabled),
                "pdf_url": f"/tickets/{ticket_id}/pdf",
                "printer_name": qz_printer_name,
                "document_label": f"Ticket {ticket_label}",
                "success_base_url": f"/tickets/{ticket_id}",
                "success_kind": "ticket",
            },
            "no_default_message": "Printing is not configured. Contact admin.",
        },
    }


def _ticket_wtn_actions_context(
    db: Session,
    *,
    ticket: Ticket,
) -> dict[str, object]:
    is_waste_ticket = _is_waste_transaction(_enum_value_or_text(ticket.transaction_type))
    is_complete = _status_value(ticket.status) == TicketStatusEnum.COMPLETE.value
    show_wtn_actions = bool(is_waste_ticket and is_complete)
    if not show_wtn_actions:
        return {
            "show_wtn_actions": False,
            "wtn_missing_default": False,
            "wtn_actions": None,
            "wtn_disabled_hint": "",
        }

    destinations = _load_active_wtn_destinations(db)
    default_destination = _default_wtn_destination(destinations)
    send_enabled = default_destination is not None
    platform_qz_ready = platform_qz_ready_for_tenants(db)
    qz_direct_print_enabled = _ticket_qz_direct_print_enabled(default_destination)
    qz_printer_name = _ticket_qz_printer_name(default_destination)
    return {
        "show_wtn_actions": True,
        "wtn_missing_default": not send_enabled,
        "wtn_actions": {
            "entity_type": "ticket",
            "entity_id": int(ticket.id),
            "send_url": f"/tickets/{ticket.id}/wtn/send",
            "preview_url": f"/tickets/{ticket.id}/wtn/preview",
            "send_enabled": send_enabled,
            "send_label": "Print",
            "send_button_variant": "primary",
            "preview_label": "Preview",
            "preview_button_variant": "secondary",
            "preview_button_id": "",
            "download_url": f"/tickets/{ticket.id}/wtn/pdf",
            "download_label": "Download PDF",
            "download_button_variant": "secondary",
            "qz_print": {
                "enabled": bool(send_enabled and platform_qz_ready and qz_direct_print_enabled),
                "pdf_url": f"/tickets/{ticket.id}/wtn/pdf",
                "printer_name": qz_printer_name,
                "document_label": f"WTN {ticket.ticket_no or ticket.id}",
                "success_base_url": f"/tickets/{ticket.id}",
                "success_kind": "wtn",
            },
            "no_default_message": "Sending is not set up yet. Ask an admin.",
        },
        "wtn_disabled_hint": (
            "Sending is not set up yet. Ask an admin."
            if not send_enabled
            else ""
        ),
    }


def _rendered_document_html(rendered_content: str, content_type: str) -> str:
    if content_type == PRINT_CONTENT_TYPE_HTML:
        return rendered_content
    return f"<pre>{escape(rendered_content)}</pre>"


def _ticket_render_document(
    db: Session,
    ticket: Ticket,
) -> tuple[PrintDestination, RenderedPrint]:
    payload = build_ticket_print_payload(db, ticket)
    destination = _resolve_ticket_print_destination(
        db,
        require_default=True,
    )
    if destination is None:
        raise ValueError("Printing is not configured. Contact admin.")
    rendered = render_destination_content(
        db,
        payload=payload,
        destination=destination,
    )
    return destination, rendered


def _ticket_filename(ticket: Ticket) -> str:
    token = re.sub(r"[^A-Za-z0-9._-]+", "-", str(ticket.ticket_no or "").strip()).strip("-")
    if not token:
        token = f"ticket-{ticket.id}"
    return f"Ticket-{token}.pdf"


def _render_ticket_pdf_bytes(
    request: Request,
    db: Session,
    ticket: Ticket,
) -> tuple[bytes, PrintDestination]:
    destination, rendered = _ticket_render_document(db, ticket)
    pdf_bytes = render_html_pdf_bytes(
        _rendered_document_html(
            rendered.rendered_content,
            rendered.content_type,
        ),
        base_url=str(request.base_url),
        allow_fallback=False,
        include_fallback_warning=False,
        enforce_print_safe=rendered.content_type == PRINT_CONTENT_TYPE_HTML,
        enforce_single_page=rendered.content_type == PRINT_CONTENT_TYPE_HTML,
    )
    return pdf_bytes, destination


def _ticket_email_form_values(
    db: Session,
    ticket: Ticket,
    *,
    values: dict[str, object] | None = None,
) -> dict[str, str]:
    customer = db.get(Customer, ticket.customer_id) if ticket.customer_id else None
    company = get_company_setting(db)
    company_name = str(getattr(company, "name", "") or "").strip() or "Weighbridge Web"
    subject_template = str(getattr(company, "ticket_email_subject_template", "") or "").strip()
    body_template = str(getattr(company, "ticket_email_body_template", "") or "").strip()
    placeholders = {
        "company_name": company_name,
        "ticket_no": ticket.ticket_no or "",
        "final_disposal": "Yes" if bool(ticket.final_disposal) else "No",
        "used_on_site": "Yes" if bool(ticket.used_on_site) else "No",
    }
    defaults = {
        "to_email": normalize_email(getattr(customer, "invoice_email", None)),
        "cc_email": "",
        "subject": (
            render_email_template(
                subject_template,
                placeholders=placeholders,
            )
            if subject_template
            else TICKET_EMAIL_DEFAULT_SUBJECT.format(
                ticket_no=ticket.ticket_no or "",
                company_name=company_name,
            )
        ),
        "message": (
            render_email_template(
                body_template,
                placeholders=placeholders,
            )
            if body_template
            else TICKET_EMAIL_DEFAULT_BODY.format(
                ticket_no=ticket.ticket_no or "",
                company_name=company_name,
            )
        ),
    }
    if values is None:
        return defaults
    return {
        "to_email": normalize_email(values.get("to_email")),
        "cc_email": str(values.get("cc_email", "") or "").strip(),
        "subject": str(values.get("subject", "") or "").strip() or defaults["subject"],
        "message": str(values.get("message", "") or "").strip() or defaults["message"],
    }


def _audit_ticket_email_attempt(
    db: Session,
    request: Request,
    ticket: Ticket,
    *,
    recipient: str,
    cc: str,
    subject: str,
    error: str | None = None,
) -> None:
    sent = error is None
    audit_log(
        db,
        request,
        action="TICKET_EMAIL_SENT" if sent else "TICKET_EMAIL_FAILED",
        entity_type="ticket",
        entity_id=ticket.id,
        summary=(
            f"Sent ticket {ticket.ticket_no} by email"
            if sent
            else f"Failed to send ticket {ticket.ticket_no} by email"
        ),
        details={
            "ticket_id": ticket.id,
            "ticket_no": ticket.ticket_no,
            "recipient": recipient,
            "cc": cc,
            "subject": subject,
            "status": "sent" if sent else "failed",
            "error": error,
        },
    )


def _optional_money_decimal(value: object | None) -> Decimal | None:
    if value is None:
        return None
    try:
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return money_decimal(decimal_value)


def _pence_to_money(value: int | None) -> Decimal | None:
    if value is None:
        return None
    return money_decimal(Decimal(value) / Decimal("100"))


def _ticket_estimated_total(
    ticket: Ticket,
    *,
    form: dict | None = None,
) -> Decimal | None:
    ticket_total = _optional_money_decimal(getattr(ticket, "total", None))
    if ticket_total is not None:
        return ticket_total

    qty_value: float | None = None
    unit_price_value: Decimal | None = None
    if form is not None:
        qty_value = _parse_float(str(form.get("qty", "")))
        unit_price_value = _parse_decimal(str(form.get("unit_price", "")))
    if qty_value is None and getattr(ticket, "qty", None) is not None:
        qty_value = _parse_float(str(ticket.qty))
    if unit_price_value is None:
        unit_price_value = _optional_money_decimal(getattr(ticket, "unit_price", None))
    if qty_value is None or unit_price_value is None:
        return None
    return money_decimal(Decimal(str(qty_value)) * unit_price_value)


def _ticket_credit_limit_banner_context(
    db: Session,
    *,
    customer_id: int | None,
    ticket: Ticket,
    form: dict | None = None,
) -> dict[str, object]:
    hidden_context: dict[str, object] = {"show_credit_limit_banner": False}
    if not customer_id:
        return hidden_context
    customer = db.get(Customer, customer_id)
    if customer is None:
        return hidden_context
    credit_limit = _pence_to_money(customer.credit_limit_pence)
    if credit_limit is None:
        return hidden_context

    outstanding_raw = customer_outstanding_total(db, customer_id)
    outstanding_display, credit_balance = outstanding_display_values(outstanding_raw)
    ticket_estimated_total = _ticket_estimated_total(ticket, form=form)
    projected_raw = money_decimal(
        outstanding_raw + (ticket_estimated_total or Decimal("0.00"))
    )
    projected_display, _ = outstanding_display_values(projected_raw)
    warning_threshold = money_decimal(credit_limit * CREDIT_LIMIT_WARNING_RATIO)

    if projected_raw > credit_limit:
        level = "over"
        headline = "Over credit limit"
    elif projected_raw >= warning_threshold:
        level = "approaching"
        headline = "Approaching credit limit"
    else:
        return hidden_context

    return {
        "show_credit_limit_banner": True,
        "credit_limit_banner_level": level,
        "credit_limit_banner_headline": headline,
        "credit_limit_banner_limit": credit_limit,
        "credit_limit_banner_outstanding": outstanding_display,
        "credit_limit_banner_credit_balance": credit_balance,
        "credit_limit_banner_ticket_total": ticket_estimated_total,
        "credit_limit_banner_projected": projected_display,
    }


@router.get("/tickets/{ticket_id:int}", response_class=HTMLResponse)
def tickets_edit(
    ticket_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return templates.TemplateResponse(request, 
            "tickets/not_found.html",
            {"request": request, "ticket_id": ticket_id},
            status_code=404,
        )

    _ensure_ticket_void_reasons(db)
    is_admin = False
    invoice = db.get(Invoice, ticket.invoice_id) if ticket.invoice_id else None
    ticket_void, ticket_void_reason = _latest_ticket_void_with_reason(db, ticket.id)
    stop_blockers = _on_stop_blockers(db, ticket.customer_id, ticket.haulier_id)
    do_not_invoice_warning, po_required_warning = _customer_invoice_warning_flags(
        db, ticket.customer_id, ticket.po_number, ticket_status=ticket.status
    )
    form = _ticket_to_form(ticket)
    options = _load_ticket_options_with_enums(
        db,
        transaction_type=form.get("transaction_type"),
        selected_product_id=ticket.product_id,
        direction=form.get("direction"),
        selected_transaction_type=form.get("transaction_type"),
    )
    product_warning = _sales_only_selected_product_warning(
        db, form.get("transaction_type"), ticket.product_id
    )
    selected_customer_id = _parse_int(str(form.get("customer_id") or ""))
    (
        price_override_active,
        price_override_unit_price,
    ) = _ticket_price_override_context(
        db,
        customer_id=selected_customer_id,
        product_id=ticket.product_id,
    )
    credit_limit_banner_context = _ticket_credit_limit_banner_context(
        db,
        customer_id=selected_customer_id,
        ticket=ticket,
        form=form,
    )
    print_actions_context = _ticket_print_actions_context(
        db,
        ticket=ticket,
    )
    wtn_actions_context = _ticket_wtn_actions_context(
        db,
        ticket=ticket,
    )
    ticket_email_form = _ticket_email_form_values(db, ticket)
    return templates.TemplateResponse(request, 
        "tickets/edit.html",
        {
            "request": request,
            "errors": [],
            "warnings": [],
            "product_usage_warning": product_warning,
            "ticket_operation": _ticket_operation_context_from_form(form),
            "ticket_operation_form": _ticket_operation_form_values(
                final_disposal=_form_value_enabled(form.get("final_disposal")),
                used_on_site=_form_value_enabled(form.get("used_on_site")),
            ),
            "saved": request.query_params.get("saved") == "1",
            "completed": request.query_params.get("completed") == "1",
            "voided": request.query_params.get("voided") == "1",
            "printed": request.query_params.get("printed") == "1",
            "print_failed": request.query_params.get("print_failed") == "1",
            "print_error_label": request.query_params.get("print_error", ""),
            "print_error_detail": request.query_params.get("print_error_detail", ""),
            "printed_to": request.query_params.get("printed_to", ""),
            "print_status": request.query_params.get("print_status") == "1",
            "print_sent_at": request.query_params.get("print_sent_at", ""),
            "print_job_id": request.query_params.get("print_job_id", ""),
            "wtn_sent": request.query_params.get("wtn_sent") == "1",
            "wtn_failed": request.query_params.get("wtn_failed") == "1",
            "wtn_error_detail": request.query_params.get("wtn_error_detail", ""),
            "wtn_job_id": request.query_params.get("wtn_job_id", ""),
            "wtn_signature_saved": request.query_params.get("wtn_signature_saved")
            == "1",
            "wtn_receiver_signature_applied": request.query_params.get(
                "wtn_receiver_signature_applied"
            )
            == "1",
            "email_sent": request.query_params.get("email_sent") == "1",
            "ticket_email_failed": False,
            "ticket_email_error": "",
            "ticket": ticket,
            "ticket_void": ticket_void,
            "ticket_void_reason": ticket_void_reason,
            "invoice": invoice,
            "is_admin": is_admin,
            "allow_dev_preview": (
                _ticket_dev_preview_enabled()
                and _status_value(ticket.status) != TicketStatusEnum.COMPLETE.value
            ),
            "is_open": _is_open_ticket(ticket),
            "is_locked": _is_ticket_locked(ticket),
            "can_void": _can_void_ticket(ticket),
            "void_blocked_message": _ticket_void_blocked_message(ticket)
            if _status_value(ticket.status) != TicketStatusEnum.VOID.value
            and not _can_void_ticket(ticket)
            else "",
            "locked_message": _ticket_locked_message(ticket)
            if _is_ticket_locked(ticket)
            else "",
            "stop_blocked": bool(stop_blockers),
            "stop_banner_message": _on_stop_banner_message(stop_blockers),
            "customer_do_not_invoice_warning": do_not_invoice_warning,
            "customer_po_required_warning": po_required_warning,
            "po_required_banner_text": PO_REQUIRED_INVOICE_BANNER,
            "po_number_max_length": PO_NUMBER_MAX_LENGTH,
            "compliance_locked_invoiced": _compliance_locked_invoiced(ticket, db),
            "compliance_locked_invoiced_message": COMPLIANCE_LOCKED_INVOICED_MESSAGE,
            "ticket_compliance_hazardous": _is_ticket_compliance_hazardous(ticket),
            "weight_warning": _net_negative(ticket),
            "form": form,
            "vehicle_reg": _ticket_vehicle_reg(db, ticket),
            "can_edit_complete_po": _can_edit_complete_po(ticket, db),
            "po_locked_invoiced": _po_locked_invoiced(ticket, db),
            "price_override_active": price_override_active,
            "price_override_unit_price": price_override_unit_price,
            "options": options,
            "product_empty_option_label": _product_option_empty_label(
                form.get("transaction_type")
            ),
            "product_unit_meta": _load_product_unit_meta(db),
            "enums": _ticket_enums(),
            "receipts_wip_enabled": bool(settings.receipts_wip_enabled),
            "ticket_email_form": ticket_email_form,
            "ticket_email_form_open": False,
            **credit_limit_banner_context,
            **print_actions_context,
            **wtn_actions_context,
            **_active_lookup_options(ticket, db),
        },
    )


@router.get(
    "/tickets/{ticket_id:int}/preview",
    response_class=HTMLResponse,
)
def tickets_print_browser(
    ticket_id: int,
    request: Request,
    job_id: int | None = Query(None),
    printed_to: str | None = Query(None),
    print_destination: str | None = Query(None),
    print_sent_at: str | None = Query(None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return HTMLResponse("Ticket not found.", status_code=404)
    is_complete = _status_value(ticket.status) == TicketStatusEnum.COMPLETE.value
    allow_draft_preview = bool(not is_complete and _ticket_dev_preview_enabled())
    if not is_complete and not allow_draft_preview:
        return HTMLResponse(PRINT_REQUIRES_COMPLETE_ERROR, status_code=400)

    destination: PrintDestination | None = None
    rendered_content = ""
    rendered_content_type = PRINT_CONTENT_TYPE_TEXT
    try:
        destination, rendered = _ticket_render_document(db, ticket)
        rendered_content = rendered.rendered_content
        rendered_content_type = rendered.content_type
    except ValueError as exc:
        return HTMLResponse(str(exc) or "Printing is not configured. Contact admin.", status_code=400)
    except (RuntimeError, OSError, NotImplementedError) as exc:
        return HTMLResponse(f"Print render failed: {exc}", status_code=400)

    destination_name = (
        _ticket_destination_display_name(destination)
        if destination is not None
        else "Configured output"
    )
    resolved_printed_to = str(printed_to or destination_name).strip() or destination_name
    resolved_print_destination = (
        str(print_destination or resolved_printed_to).strip() or destination_name
    )
    resolved_sent_at = (
        str(print_sent_at or "").strip() or datetime.now().strftime("%H:%M")
    )

    back_params: dict[str, str] = {}
    if job_id is not None:
        back_params.update(
            {
                "printed": "1",
                "print_sent": "1",
                "print_status": "1",
                "printed_to": resolved_printed_to,
                "print_destination": resolved_print_destination,
                "print_sent_at": resolved_sent_at,
                "print_job_id": str(job_id),
            }
        )
    back_url = f"/tickets/{ticket.id}"
    if back_params:
        back_url = f"{back_url}?{urlencode(back_params)}"

    return templates.TemplateResponse(
        request,
        "tickets/print_browser.html",
        {
            "request": request,
            "ticket": ticket,
            "destination_display_name": destination_name,
            "job_id": job_id,
            "is_text": rendered_content_type != PRINT_CONTENT_TYPE_HTML,
            "is_draft_preview": allow_draft_preview,
            "rendered_content": rendered_content,
            "back_url": back_url,
        },
    )


@router.get("/tickets/{ticket_id:int}/pdf")
def tickets_download_pdf(
    ticket_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return Response("Ticket not found.", status_code=404)
    if _status_value(ticket.status) != TicketStatusEnum.COMPLETE.value:
        return Response(PRINT_REQUIRES_COMPLETE_ERROR, status_code=400)

    try:
        pdf_bytes, _destination = _render_ticket_pdf_bytes(request, db, ticket)
    except (ValueError, RuntimeError, OSError, NotImplementedError) as exc:
        return Response(str(exc) or "Ticket PDF render failed.", status_code=400)

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{_ticket_filename(ticket)}"',
        },
    )


def _render_ticket_receipt_html(*, ticket: Ticket, payload: dict) -> str:
    # WIP_RECEIPTS renderer path
    receipt_template = templates.env.get_template("tickets/receipt.html")
    return receipt_template.render(ticket=ticket, payload=payload)


@router.get(
    "/tickets/{ticket_id:int}/receipt",
    response_class=HTMLResponse,
)
def tickets_receipt(
    ticket_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return HTMLResponse("Ticket not found.", status_code=404)

    # WIP_RECEIPTS: Receipt printing gated behind RECEIPTS_WIP_ENABLED in operator UI.
    payload = build_ticket_print_payload(db, ticket)
    rendered_html = _render_ticket_receipt_html(ticket=ticket, payload=payload)
    try:
        result = execute_rendered_print(
            db,
            document_type=DOCUMENT_TYPE_TICKET,
            rendered_content=rendered_html,
            content_type=PRINT_CONTENT_TYPE_HTML,
            delivery_type=DELIVERY_TYPE_PRINT_LOCAL_BROWSER,
            delivery_config={},
            destination_id=None,
            template_id=None,
            ticket_id=ticket.id,
            created_by_user_id=None,
            base_url=str(request.base_url),
        )
    except (RuntimeError, ValueError, OSError, NotImplementedError) as exc:
        return HTMLResponse(f"Receipt render failed: {exc}", status_code=400)

    return HTMLResponse(result.browser_content or rendered_html)


@router.post("/tickets/{ticket_id:int}/print")
async def tickets_print_dispatch(
    ticket_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    _require_tickets_manage(request)
    ticket = db.get(Ticket, ticket_id)
    expects_json = _request_expects_json(request)
    if not ticket:
        if expects_json:
            return JSONResponse(
                {"ok": False, "error": "Ticket not found."},
                status_code=404,
            )
        return templates.TemplateResponse(
            request,
            "tickets/not_found.html",
            {"request": request, "ticket_id": ticket_id},
            status_code=404,
        )
    if _status_value(ticket.status) != TicketStatusEnum.COMPLETE.value:
        if expects_json:
            return JSONResponse(
                {"ok": False, "error": PRINT_REQUIRES_COMPLETE_ERROR},
                status_code=400,
            )
        return _render_ticket_edit(
            request,
            ticket,
            db,
            errors=[PRINT_REQUIRES_COMPLETE_ERROR],
            status_code=400,
        )

    try:
        destination = _resolve_ticket_print_destination(
            db,
            require_default=True,
        )
    except ValueError as exc:
        message = str(exc) or "Printing is not configured. Contact admin."
        if expects_json:
            return JSONResponse({"ok": False, "error": message}, status_code=400)
        return _render_ticket_edit(
            request,
            ticket,
            db,
            errors=[message],
            status_code=400,
        )

    try:
        _resolved_destination, rendered = _ticket_render_document(db, ticket)
        delivery_type = str(destination.delivery_type or "").strip().upper()
        delivery_config = (
            dict(destination.delivery_config)
            if isinstance(destination.delivery_config, dict)
            else {}
        )
    except (RuntimeError, ValueError, OSError, NotImplementedError) as exc:
        message = str(exc) or "Print failed."
        if expects_json:
            return JSONResponse({"ok": False, "error": message}, status_code=400)
        return _render_ticket_edit(
            request,
            ticket,
            db,
            errors=[f"Print failed: {message}"],
            status_code=400,
        )

    try:
        result = execute_rendered_print(
            db,
            document_type=DOCUMENT_TYPE_TICKET,
            rendered_content=rendered.rendered_content,
            content_type=rendered.content_type,
            delivery_type=delivery_type,
            delivery_config=delivery_config,
            destination_id=destination.id,
            template_id=rendered.template_id,
            ticket_id=ticket.id,
            created_by_user_id=None,
            base_url=str(request.base_url),
        )
    except (RuntimeError, ValueError, OSError, NotImplementedError) as exc:
        detail = str(exc) or "Print delivery failed."
        error_label = _friendly_print_error_label(detail)
        failed_job_id = _latest_print_job_id_for_ticket(
            db,
            ticket_id=ticket.id,
            destination_id=destination.id,
            status="FAILED",
        )
        if expects_json:
            return JSONResponse(
                {
                    "ok": False,
                    "error": detail,
                    "error_label": error_label,
                    "detail": detail,
                    "job_id": failed_job_id,
                },
                status_code=400,
            )
        query_data = {
            "print_failed": "1",
            "print_error": error_label,
            "print_error_detail": detail[:200],
            "print_destination": _ticket_destination_display_name(destination),
        }
        if failed_job_id is not None:
            query_data["print_job_id"] = str(failed_job_id)
        return RedirectResponse(
            url=f"/tickets/{ticket.id}?{urlencode(query_data)}",
            status_code=303,
        )

    destination_name = _ticket_destination_display_name(destination)
    print_sent_at = datetime.now().strftime("%H:%M")
    success_query_data = {
        "print_sent": "1",
        "printed": "1",
        "print_status": "1",
        "printed_to": destination_name,
        "print_destination": destination_name,
        "print_sent_at": print_sent_at,
        "print_job_id": str(result.job.id),
    }

    is_local_browser = delivery_type == DELIVERY_TYPE_PRINT_LOCAL_BROWSER
    if expects_json:
        payload_json: dict[str, object] = {
            "ok": True,
            "destination_id": destination.id,
            "destination_name": destination_name,
            "destination_display_name": destination_name,
            "job_id": result.job.id,
        }
        if is_local_browser:
            browser_query = urlencode(
                {
                    "job_id": str(result.job.id),
                    "printed_to": destination_name,
                    "print_destination": destination_name,
                    "print_sent_at": print_sent_at,
                }
            )
            payload_json["browser_print_url"] = (
                f"/tickets/{ticket.id}/preview?{browser_query}"
            )
        return JSONResponse(payload_json)

    if is_local_browser:
        browser_query = urlencode(
            {
                "job_id": str(result.job.id),
                "printed_to": destination_name,
                "print_destination": destination_name,
                "print_sent_at": print_sent_at,
            }
        )
        return RedirectResponse(
            url=f"/tickets/{ticket.id}/preview?{browser_query}",
            status_code=303,
        )

    query = urlencode(success_query_data)
    return RedirectResponse(
        url=f"/tickets/{ticket.id}?{query}",
        status_code=303,
    )


@router.post("/tickets/{ticket_id:int}/email")
async def tickets_email(
    ticket_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    _require_tickets_manage(request)
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return templates.TemplateResponse(
            request,
            "tickets/not_found.html",
            {"request": request, "ticket_id": ticket_id},
            status_code=404,
        )

    form = await request.form()
    form_data = _ticket_email_form_values(
        db,
        ticket,
        values={
            "to_email": form.get("to_email"),
            "cc_email": form.get("cc_email"),
            "subject": form.get("subject"),
            "message": form.get("message"),
        },
    )
    to_email = form_data["to_email"]
    cc_email = form_data["cc_email"]
    subject = form_data["subject"]
    message = form_data["message"]

    error = ""
    attachment: EmailAttachment | None = None
    if _status_value(ticket.status) != TicketStatusEnum.COMPLETE.value:
        error = TICKET_EMAIL_REQUIRES_COMPLETE_ERROR
    elif not validate_email(to_email):
        error = "To email must be a valid email address."
    else:
        platform_email = get_platform_email_settings(db)
        if not platform_email.resend_api_key:
            error = "Resend API key is not configured."
        elif not platform_email.from_email:
            error = "From email address is not configured."

    if not error:
        try:
            pdf_bytes, _destination = _render_ticket_pdf_bytes(request, db, ticket)
            attachment = EmailAttachment(
                filename=_ticket_filename(ticket),
                content_bytes=pdf_bytes,
                content_type="application/pdf",
            )
        except (ValueError, RuntimeError, OSError, NotImplementedError) as exc:
            error = str(exc) or "Ticket render failed."

    if not error and attachment is not None:
        result = send_email(
            subject=subject,
            text_body=message,
            to=to_email,
            cc=cc_email,
            attachments=[attachment],
            db=db,
        )
        if not result.ok:
            error = result.error or "Email send failed."

    if error:
        _audit_ticket_email_attempt(
            db,
            request,
            ticket,
            recipient=to_email,
            cc=cc_email,
            subject=subject,
            error=error,
        )
        db.commit()
        return _render_ticket_edit(
            request,
            ticket,
            db,
            errors=[],
            ticket_email_form=form_data,
            ticket_email_form_open=True,
            ticket_email_failed=True,
            ticket_email_error=error,
            status_code=400,
        )

    _audit_ticket_email_attempt(
        db,
        request,
        ticket,
        recipient=to_email,
        cc=cc_email,
        subject=subject,
    )
    db.commit()
    return RedirectResponse(
        url=f"/tickets/{ticket.id}?email_sent=1",
        status_code=303,
    )


def _wtn_rendered_html_for_preview(rendered_content: str, content_type: str) -> str:
    return _rendered_document_html(rendered_content, content_type)


def _wtn_filename(ticket: Ticket) -> str:
    token = re.sub(r"[^A-Za-z0-9._-]+", "-", str(ticket.ticket_no or "").strip()).strip("-")
    if not token:
        token = f"ticket-{ticket.id}"
    return f"{token}-wtn.pdf"


def _normalize_wtn_signature_role(value: str | None) -> str:
    role = str(value or "").strip().lower()
    return role if role in WTN_SIGNATURE_ROLE_FIELDS else ""


def _wtn_signature_role_columns(role: str) -> tuple[str, str, str]:
    normalized_role = _normalize_wtn_signature_role(role)
    if not normalized_role:
        raise KeyError("Invalid WTN signature role.")
    return WTN_SIGNATURE_ROLE_FIELDS[normalized_role]


@router.get("/tickets/{ticket_id:int}/wtn/preview", response_class=HTMLResponse)
def tickets_wtn_preview(
    ticket_id: int,
    request: Request,
    wtn_sent: int | None = Query(None),
    wtn_job_id: int | None = Query(None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        return templates.TemplateResponse(
            request,
            "tickets/not_found.html",
            {"request": request, "ticket_id": ticket_id},
            status_code=404,
        )

    try:
        payload = build_wtn_payload(db, ticket.id)
        destination = _resolve_wtn_destination(db, require_default=False)
        rendered_content = ""
        rendered_content_type = PRINT_CONTENT_TYPE_HTML
        if destination is not None:
            rendered = render_destination_content(
                db,
                payload=payload,
                destination=destination,
            )
            rendered_content = rendered.rendered_content
            rendered_content_type = rendered.content_type
        else:
            fallback_template = _resolve_wtn_default_template(db)
            if fallback_template is not None:
                rendered_content = render_from_content(
                    payload,
                    fallback_template.content,
                    db=db,
                )
                rendered_content_type = str(fallback_template.format or "").strip().upper()
            else:
                raise ValueError("WTN template is not configured. Contact admin.")
    except (LookupError, ValueError) as exc:
        return HTMLResponse(str(exc), status_code=400)
    except (RuntimeError, OSError, NotImplementedError) as exc:
        return HTMLResponse(f"WTN preview failed: {exc}", status_code=400)

    rendered_html = _wtn_rendered_html_for_preview(
        rendered_content,
        rendered_content_type,
    )
    blockers = list(payload.get("send_blockers") or [])
    return templates.TemplateResponse(
        request,
        "tickets/wtn_preview.html",
        {
            "request": request,
            "ticket": ticket,
            "rendered_html": rendered_html,
            "back_url": f"/tickets/{ticket.id}",
            "send_blockers": blockers,
            "send_configured": destination is not None,
            "wtn_sent": bool(wtn_sent),
            "wtn_job_id": wtn_job_id,
        },
    )


@router.get("/tickets/{ticket_id:int}/wtn/pdf")
def tickets_wtn_pdf(
    ticket_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found.")

    try:
        payload = build_wtn_payload(db, ticket.id)
        destination = _resolve_wtn_destination(db, require_default=True)
        if destination is None:
            raise ValueError("Sending is not set up yet. Ask an admin.")
        rendered = render_destination_content(
            db,
            payload=payload,
            destination=destination,
        )
        rendered_html = _wtn_rendered_html_for_preview(
            rendered.rendered_content,
            rendered.content_type,
        )
        pdf_bytes = render_html_pdf_bytes(
            rendered_html,
            base_url=str(request.base_url),
            allow_fallback=False,
            include_fallback_warning=False,
            enforce_print_safe=rendered.content_type == PRINT_CONTENT_TYPE_HTML,
            enforce_single_page=rendered.content_type == PRINT_CONTENT_TYPE_HTML,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{_wtn_filename(ticket)}"',
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


async def _tickets_wtn_signature_save_for_role(
    *,
    ticket_id: int,
    role: str,
    request: Request,
    db: Session,
) -> Response:
    _require_tickets_manage(request)
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        return templates.TemplateResponse(
            request,
            "tickets/not_found.html",
            {"request": request, "ticket_id": ticket_id},
            status_code=404,
        )

    normalized_role = _normalize_wtn_signature_role(role)
    if not normalized_role:
        return HTMLResponse("WTN signature role not found.", status_code=404)

    status_value = _status_value(ticket.status)
    transaction_type_value = _enum_value_or_text(ticket.transaction_type)
    if (
        status_value != TicketStatusEnum.COMPLETE.value
        or not _is_waste_transaction(transaction_type_value)
    ):
        return _render_ticket_edit(
            request,
            ticket,
            db,
            errors=["WTN signatures are only available for complete waste tickets."],
            status_code=400,
        )

    form = await request.form()
    signature_data_url = str(form.get("signature_data_url") or "").strip()
    signer_name_input = str(form.get("signer_name") or "").strip()

    signer_name: str | None = None
    if signer_name_input:
        signer_errors: list[str] = []
        validate_no_html(signer_name_input, "Signer name", signer_errors)
        if len(signer_name_input) > NAME_MAX:
            signer_errors.append(f"Signer name must be {NAME_MAX} characters or fewer.")
        if signer_errors:
            return _render_ticket_edit(
                request,
                ticket,
                db,
                errors=signer_errors,
                status_code=400,
            )
        signer_name = signer_name_input

    normalized_signature = normalize_png_data_url(signature_data_url)
    if normalized_signature is None:
        return _render_ticket_edit(
            request,
            ticket,
            db,
            errors=["Signature image is invalid. Please capture and save again."],
            status_code=400,
        )
    normalized_data_url, normalized_png_bytes = normalized_signature
    if not png_has_visible_ink(normalized_png_bytes):
        return _render_ticket_edit(
            request,
            ticket,
            db,
            errors=["Signature cannot be blank."],
            status_code=400,
        )

    data_field, signed_at_field, signer_field = _wtn_signature_role_columns(normalized_role)
    replacing_existing = bool(str(getattr(ticket, data_field, "") or "").strip())
    signed_at_value = utcnow()
    setattr(ticket, data_field, normalized_data_url)
    setattr(ticket, signed_at_field, signed_at_value)
    setattr(ticket, signer_field, signer_name)
    signed_at_iso = signed_at_value.isoformat()
    role_label = WTN_SIGNATURE_ROLE_LABELS.get(normalized_role, normalized_role.title())
    audit_log(
        db,
        request,
        action="TICKET_WTN_SIGNATURE_SAVED",
        entity_type="ticket",
        entity_id=ticket.id,
        summary=(
            f"Replaced WTN {role_label} signature for ticket {ticket.ticket_no}"
            if replacing_existing
            else f"Saved WTN {role_label} signature for ticket {ticket.ticket_no}"
        ),
        details={
            "ticket_id": ticket.id,
            "ticket_no": ticket.ticket_no,
            "role": normalized_role,
            "operation": "replace" if replacing_existing else "save",
            "signer_name": signer_name or None,
            "signed_at": signed_at_iso,
        },
    )
    db.add(ticket)
    db.commit()
    return RedirectResponse(
        url=f"/tickets/{ticket.id}?wtn_signature_saved=1",
        status_code=303,
    )


@router.post("/tickets/{ticket_id:int}/wtn/signature/producer")
async def tickets_wtn_signature_save_producer(
    ticket_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    return await _tickets_wtn_signature_save_for_role(
        ticket_id=ticket_id,
        role="producer",
        request=request,
        db=db,
    )


@router.post("/tickets/{ticket_id:int}/wtn/signature/carrier")
async def tickets_wtn_signature_save_carrier(
    ticket_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    return await _tickets_wtn_signature_save_for_role(
        ticket_id=ticket_id,
        role="carrier",
        request=request,
        db=db,
    )


@router.post("/tickets/{ticket_id:int}/wtn/signature/receiver")
async def tickets_wtn_signature_save_receiver(
    ticket_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    return await _tickets_wtn_signature_save_for_role(
        ticket_id=ticket_id,
        role="receiver",
        request=request,
        db=db,
    )


@router.post("/tickets/{ticket_id:int}/wtn/signature")
async def tickets_wtn_signature_save(
    ticket_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    # Legacy compatibility path from phase-1; maps to receiver signature in phase-2.
    return await _tickets_wtn_signature_save_for_role(
        ticket_id=ticket_id,
        role="receiver",
        request=request,
        db=db,
    )


@router.post("/tickets/{ticket_id:int}/wtn/signature/receiver/apply-saved")
async def tickets_wtn_receiver_signature_apply_saved(
    ticket_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    _require_tickets_manage(request)
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        return templates.TemplateResponse(
            request,
            "tickets/not_found.html",
            {"request": request, "ticket_id": ticket_id},
            status_code=404,
        )

    status_value = _status_value(ticket.status)
    transaction_type_value = _enum_value_or_text(ticket.transaction_type)
    if (
        status_value != TicketStatusEnum.COMPLETE.value
        or not _is_waste_transaction(transaction_type_value)
    ):
        return _render_ticket_edit(
            request,
            ticket,
            db,
            errors=["WTN signatures are only available for complete waste tickets."],
            status_code=400,
        )

    current_user = getattr(getattr(request, "state", None), "current_user", None)
    saved_signature_data_uri = str(
        getattr(current_user, "saved_signature_data_uri", "") or ""
    ).strip()
    if not saved_signature_data_uri:
        return _render_ticket_edit(
            request,
            ticket,
            db,
            errors=["You do not have a saved signature yet."],
            status_code=400,
        )

    applied_by_user_name = user_display_name(current_user)
    signer_name = (
        str(getattr(current_user, "saved_signature_signer_name", "") or "").strip()
        or applied_by_user_name
    )
    signer_name = signer_name[:NAME_MAX].strip()
    replacing_existing = ticket.has_wtn_receiver_signature
    signed_at_value = utcnow()
    ticket.wtn_receiver_signature_data_uri = saved_signature_data_uri
    ticket.wtn_receiver_signature_signer_name = signer_name or None
    ticket.wtn_receiver_signature_signed_at = signed_at_value
    audit_log(
        db,
        request,
        action="TICKET_WTN_RECEIVER_SIG_APPLY",
        entity_type="ticket",
        entity_id=ticket.id,
        summary=(
            f"Replaced receiver signature from saved user signature for ticket {ticket.ticket_no}"
            if replacing_existing
            else f"Applied saved user signature to receiver for ticket {ticket.ticket_no}"
        ),
        details={
            "ticket_id": ticket.id,
            "ticket_no": ticket.ticket_no,
            "applied_by_user_id": getattr(current_user, "id", None),
            "applied_by_user_name": applied_by_user_name,
            "signer_name": signer_name or None,
            "signed_at": signed_at_value.isoformat(),
            "operation": "apply_saved_signature",
        },
    )
    db.add(ticket)
    db.commit()
    return RedirectResponse(
        url=f"/tickets/{ticket.id}?wtn_receiver_signature_applied=1",
        status_code=303,
    )


@router.post("/tickets/{ticket_id:int}/wtn/send")
async def tickets_wtn_send(
    ticket_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    _require_tickets_manage(request)
    ticket = db.get(Ticket, ticket_id)
    expects_json = _request_expects_json(request)
    if ticket is None:
        if expects_json:
            return JSONResponse({"ok": False, "error": "Ticket not found."}, status_code=404)
        return templates.TemplateResponse(
            request,
            "tickets/not_found.html",
            {"request": request, "ticket_id": ticket_id},
            status_code=404,
        )

    if _status_value(ticket.status) != TicketStatusEnum.COMPLETE.value:
        if expects_json:
            return JSONResponse(
                {"ok": False, "error": WTN_SEND_REQUIRES_COMPLETE_ERROR},
                status_code=400,
            )
        return _render_ticket_edit(
            request,
            ticket,
            db,
            errors=[WTN_SEND_REQUIRES_COMPLETE_ERROR],
            status_code=400,
        )

    try:
        payload = build_wtn_payload(db, ticket.id)
    except (LookupError, ValueError) as exc:
        if expects_json:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return _render_ticket_edit(
            request,
            ticket,
            db,
            errors=[str(exc)],
            status_code=400,
        )

    blockers = list(payload.get("send_blockers") or [])
    if blockers:
        message = f"Cannot send WTN. Missing required fields: {', '.join(blockers)}."
        if expects_json:
            return JSONResponse(
                {"ok": False, "error": message, "missing_fields": blockers},
                status_code=400,
            )
        return _render_ticket_edit(
            request,
            ticket,
            db,
            errors=[message],
            status_code=400,
        )

    try:
        destination = _resolve_wtn_destination(db, require_default=True)
        if destination is None:
            raise ValueError("Sending is not set up yet. Ask an admin.")
        rendered = render_destination_content(
            db,
            payload=payload,
            destination=destination,
        )
        delivery_type = str(destination.delivery_type or "").strip().upper()
        delivery_config = (
            dict(destination.delivery_config)
            if isinstance(destination.delivery_config, dict)
            else {}
        )
        render_content_type = rendered.content_type
        payload_bytes: bytes | None = None
        if delivery_type == DELIVERY_TYPE_EMAIL_PDF:
            rendered_html = _wtn_rendered_html_for_preview(
                rendered.rendered_content,
                rendered.content_type,
            )
            payload_bytes = render_html_pdf_bytes(
                rendered_html,
                base_url=str(request.base_url),
                allow_fallback=False,
                include_fallback_warning=False,
                enforce_print_safe=rendered.content_type == PRINT_CONTENT_TYPE_HTML,
                enforce_single_page=rendered.content_type == PRINT_CONTENT_TYPE_HTML,
            )
            render_content_type = PRINT_CONTENT_TYPE_PDF
        result = execute_rendered_print(
            db,
            document_type=DOCUMENT_TYPE_WTN,
            rendered_content=rendered.rendered_content,
            content_type=render_content_type,
            delivery_type=delivery_type,
            delivery_config=delivery_config,
            destination_id=destination.id,
            template_id=rendered.template_id,
            ticket_id=ticket.id,
            created_by_user_id=None,
            payload_bytes=payload_bytes,
            base_url=str(request.base_url),
        )
    except (RuntimeError, ValueError, OSError, NotImplementedError) as exc:
        detail = str(exc) or "WTN send failed."
        failed_job_id = _latest_print_job_id_for_ticket(
            db,
            ticket_id=ticket.id,
            destination_id=None,
            document_type=DOCUMENT_TYPE_WTN,
            status="FAILED",
        )
        if expects_json:
            return JSONResponse(
                {
                    "ok": False,
                    "error": detail,
                    "job_id": failed_job_id,
                },
                status_code=400,
            )
        query_data = {
            "wtn_failed": "1",
            "wtn_error_detail": detail[:200],
        }
        if failed_job_id is not None:
            query_data["wtn_job_id"] = str(failed_job_id)
        return RedirectResponse(
            url=f"/tickets/{ticket.id}?{urlencode(query_data)}",
            status_code=303,
        )

    is_local_browser = (
        str(destination.delivery_type or "").strip().upper() == DELIVERY_TYPE_PRINT_LOCAL_BROWSER
    )
    if expects_json:
        payload_json: dict[str, object] = {
            "ok": True,
            "job_id": result.job.id,
            "destination_id": destination.id,
            "destination_name": _ticket_destination_display_name(destination),
        }
        if is_local_browser:
            payload_json["browser_preview_url"] = (
                f"/tickets/{ticket.id}/wtn/preview?wtn_sent=1&wtn_job_id={result.job.id}"
            )
        return JSONResponse(payload_json)

    if is_local_browser:
        return RedirectResponse(
            url=f"/tickets/{ticket.id}/wtn/preview?wtn_sent=1&wtn_job_id={result.job.id}",
            status_code=303,
        )

    return RedirectResponse(
        url=f"/tickets/{ticket.id}?wtn_sent=1&wtn_job_id={result.job.id}",
        status_code=303,
    )


@router.post("/tickets/{ticket_id:int}/po", response_class=HTMLResponse)
async def tickets_update_po(
    ticket_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    _require_tickets_manage(request)
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return templates.TemplateResponse(request, 
            "tickets/not_found.html",
            {"request": request, "ticket_id": ticket_id},
            status_code=404,
        )

    if _status_value(ticket.status) == TicketStatusEnum.VOID.value:
        return _render_ticket_edit(
            request,
            ticket,
            db,
            errors=["Cannot update PO on a void ticket."],
            status_code=400,
        )
    if _is_ticket_invoiced(ticket, db):
        return _render_ticket_edit(
            request,
            ticket,
            db,
            errors=["Cannot update PO because this ticket has already been invoiced."],
            status_code=400,
        )
    if not _can_update_po(ticket, db):
        return _render_ticket_edit(
            request,
            ticket,
            db,
            errors=["PO can only be updated on open or complete tickets."],
            status_code=400,
        )

    form = await request.form()
    po_number_raw = _form_value(form, "po_number")
    errors: list[str] = []
    po_number = _parse_po_number(po_number_raw, errors)
    if errors:
        form_data = _ticket_to_form(ticket)
        form_data["po_number"] = po_number_raw
        return _render_ticket_edit(
            request,
            ticket,
            db,
            errors=errors,
            form=form_data,
            status_code=400,
        )

    ticket.po_number = po_number
    ticket.updated_at = utcnow()
    db.commit()
    return RedirectResponse(url=f"/tickets/{ticket_id}?saved=1", status_code=303)


@router.post("/tickets/{ticket_id:int}", response_class=HTMLResponse)
async def tickets_update(
    ticket_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return templates.TemplateResponse(request, 
            "tickets/not_found.html",
            {"request": request, "ticket_id": ticket_id},
            status_code=404,
        )

    form = await request.form()
    action = str(form.get("action", "save"))
    if action == "complete":
        require_permission(request, PERM_COMPLETE_TICKETS)
    elif action == "void":
        require_permission(request, PERM_VOID_TICKETS)
    else:
        _require_tickets_manage(request)
    original_snapshot = _ticket_audit_snapshot(ticket) if action == "save" else None
    original_audit_values = _ticket_audit_values(ticket) if action == "save" else None
    if action == "void":
        _ensure_ticket_void_reasons(db)

    if _is_ticket_locked(ticket) and not (action == "void" and _can_void_ticket(ticket)):
        if action == "void":
            return _render_ticket_edit(
                request,
                ticket,
                db,
                errors=[_ticket_void_blocked_message(ticket)],
                status_code=400,
            )
        return _render_ticket_edit(
            request,
            ticket,
            db,
            errors=[_ticket_locked_message(ticket)],
            status_code=400,
        )

    if (
        action in {"save", "complete"}
        and _compliance_locked_invoiced(ticket, db)
        and _compliance_fields_present_in_form(form)
    ):
        return _render_ticket_edit(
            request,
            ticket,
            db,
            errors=[COMPLIANCE_LOCKED_INVOICED_MESSAGE],
            status_code=400,
        )

    payload = _parse_ticket_form(
        form,
        current_status=ticket.status.value if ticket.status else None,
        validate_ewc=action == "complete",
    )
    _apply_walk_in_sale_mode(payload)
    payload["form"]["direction"] = payload["direction"] or ""
    payload["form"]["transaction_type"] = payload["transaction_type"] or ""
    if payload["vehicle_id"] is None and ticket.vehicle_id is not None:
        payload["vehicle_id"] = ticket.vehicle_id
        payload["form"]["vehicle_id"] = str(ticket.vehicle_id)
    payload["errors"].extend(_validate_ticket_entity_references(ticket, payload, db))

    if action == "complete":
        before_complete_values = _ticket_audit_values(ticket)
        ticket.direction = payload["direction"]
        ticket.transaction_type = payload["transaction_type"]
        weight_warning = _net_negative_values(payload["gross_kg"], payload["tare_kg"])
        lookup_errors = _validate_lookup_fields(ticket, payload, db)
        payload["errors"].extend(lookup_errors)
        _apply_ticket_defaults(db, payload)
        product = _validate_product_ewc(payload, db)
        ewc_snapshot = _resolve_ticket_ewc_snapshot(payload, db, product)
        _coerce_mode_fields(payload, product)
        weight_warning = _net_negative_values(payload["gross_kg"], payload["tare_kg"])
        _apply_destination_default(ticket, payload, product)
        haulier = _validate_carrier_licence(payload, db, errors=payload["errors"])
        is_waste_tx = _is_waste_transaction(payload.get("transaction_type"))
        _resolve_waste_producer_snapshot(
            payload,
            db,
            require_customer_when_same=is_waste_tx,
            require_manual_name=False,
        )
        _validate_waste_producer_on_complete(payload)
        if is_waste_tx and haulier and not haulier.carrier_licence_number:
            payload["errors"].append(
                "Selected haulier has no Waste Carrier Licence number. "
                "Add it in Lookups → Hauliers, then complete the ticket."
            )

        effective_reg_text = payload["vehicle_reg_text"] or ticket.vehicle_reg_text or ""
        completion_allowed = (
            payload["walk_in"]
            or payload["vehicle_id"] is not None
            or bool(effective_reg_text)
        )
        if is_waste_tx and not completion_allowed:
            payload["errors"].append(
                "To complete: enter a registration or select a vehicle."
            )

        _validate_required_on_complete(payload)
        stop_blockers = _on_stop_blockers(
            db, payload.get("customer_id"), payload.get("haulier_id")
        )
        if stop_blockers:
            payload["errors"].append(_on_stop_completion_error(stop_blockers))
        product_unit_type = _product_unit_type(product)
        has_qty = payload.get("qty") is not None and payload.get("qty") > 0
        has_gross = payload.get("gross_kg") is not None
        has_tare = payload.get("tare_kg") is not None and payload.get("tare_kg") > 0
        if is_waste_tx and product_unit_type != "COUNT" and has_gross and not has_tare:
            vehicle = (
                db.get(Vehicle, payload["vehicle_id"])
                if payload.get("vehicle_id")
                else None
            )
            if vehicle and vehicle.default_tare_kg is not None:
                payload["tare_kg"] = vehicle.default_tare_kg
                payload["form"]["tare_kg"] = f"{vehicle.default_tare_kg:.0f}"
                payload["net_kg"] = payload["gross_kg"] - payload["tare_kg"]
                payload["form"]["net_kg"] = f"{payload['net_kg']:.0f}"
                has_tare = True
        has_weights = has_gross and has_tare
        if product_unit_type == "COUNT":
            count_mode_error = "COUNT product: enter Qty only (weights must be blank)."
            if not has_qty:
                if count_mode_error not in payload["errors"]:
                    payload["errors"].append(count_mode_error)
        elif product_unit_type == "WEIGHT":
            weight_mode_error = "WEIGHT product: enter weights only (Qty must be blank)."
            if has_qty or not has_weights:
                if weight_mode_error not in payload["errors"]:
                    payload["errors"].append(weight_mode_error)
        else:
            if is_waste_tx and not has_weights:
                payload["errors"].append(
                    "Weigh-in and weigh-out are required to complete a waste ticket."
                )
            if (
                not is_waste_tx
                and payload.get("transaction_type") == TransactionTypeEnum.SALE.value
            ):
                allow_sale_weights = bool(
                    has_weights and product and product.unit and product.unit.unit_type == "WEIGHT"
                )
                if not (has_qty or allow_sale_weights):
                    payload["errors"].append(
                        "Enter a quantity or weigh-in and weigh-out to complete a sale."
                    )
        if (
            product_unit_type != "COUNT"
            and
            has_weights
            and _net_negative_values(payload["gross_kg"], payload["tare_kg"])
        ):
            payload["errors"].append(
                "Net weight cannot be negative. Use Swap Weights."
            )

        _validate_waste_ewc_on_complete(payload, ewc_snapshot)

        if payload["errors"]:
            ticket.vehicle_reg_text = payload["vehicle_reg_text"]
            ticket.walk_in = payload["walk_in"]
            ticket.walk_in_sale = payload["walk_in_sale"]
            ticket.updated_at = utcnow()
            db.commit()
            return _render_ticket_edit(
                request,
                ticket,
                db,
                errors=payload["errors"],
                warnings=payload.get("warnings"),
                form=payload["form"],
                vehicle_reg=payload["vehicle_reg_text"],
                weight_warning=weight_warning,
                status_code=400,
            )

        _apply_ticket_updates(ticket, payload)
        pricing_info = _apply_ticket_pricing(ticket, payload, product)
        _apply_ticket_ewc_snapshot(ticket, ewc_snapshot)
        _apply_carrier_licence_snapshot(ticket, haulier)
        _apply_waste_producer_snapshot(ticket, payload)
        is_hazardous = bool(ticket.ewc_hazardous)
        if is_waste_tx and is_hazardous:
            if not ticket.customer_id:
                payload["errors"].append(
                    "Customer is required for hazardous waste tickets."
                )
        if payload["errors"]:
            return _render_ticket_edit(
                request,
                ticket,
                db,
                errors=payload["errors"],
                warnings=payload.get("warnings"),
                form=payload["form"],
                vehicle_reg=payload["vehicle_reg_text"],
                weight_warning=weight_warning,
                status_code=400,
            )
        unit = getattr(product, "unit", None) if product else None
        ticket.pricing_unit_name = unit.name if unit else None
        ticket.pricing_unit_type = unit.unit_type if unit else None
        ticket.pricing_unit_price = ticket.unit_price
        ticket.pricing_qty_snapshot = ticket.qty
        ticket.pricing_net_kg_snapshot = (
            pricing_info.get("net_kg") if pricing_info else None
        ) or ticket.net_kg
        ticket.pricing_billable_qty_snapshot = (
            pricing_info.get("billable_qty") if pricing_info else None
        )
        snapshot_customer = (
            db.get(Customer, ticket.customer_id) if ticket.customer_id else None
        )
        ticket.wip_snapshot_json = ticket_wip_snapshot(
            customer=snapshot_customer,
            product=product,
        )
        ticket.status = TicketStatusEnum.COMPLETE.value
        complete_change_details = audit_diff(
            before_complete_values,
            _ticket_audit_values(ticket),
            _TICKET_AUDIT_DIFF_KEYS,
        )
        audit_log(
            db,
            request,
            action="COMPLETE",
            entity_type="ticket",
            entity_id=ticket.id,
            summary=f"Completed ticket {ticket.ticket_no}",
            details=complete_change_details,
        )
        db.commit()
        return RedirectResponse(url=f"/tickets/{ticket_id}?completed=1", status_code=303)

    if action == "void":
        if not _can_void_ticket(ticket):
            return _render_ticket_edit(
                request,
                ticket,
                db,
                errors=[_ticket_void_blocked_message(ticket)],
                status_code=400,
            )
        reason_id = _parse_int(str(form.get("void_reason_id", "")).strip())
        note = str(form.get("void_note", "")).strip()
        errors = []
        validate_no_html(note, "Void note", errors)
        if note and len(note) > NOTES_MAX:
            errors.append(f"Void note must be {NOTES_MAX} characters or fewer.")
        if not reason_id:
            errors.append("Void reason is required.")
        reason = db.get(VoidReason, reason_id) if reason_id else None
        if reason_id and (
            not reason
            or not reason.is_active
            or (reason.reason_type or "").strip().upper() != VOID_REASON_TYPE_TICKET
        ):
            errors.append("Void reason is invalid.")
        if reason and _is_other_void_reason(reason) and not note:
            errors.append("Void note is required when reason is Other.")
        if errors:
            return _render_ticket_edit(
                request,
                ticket,
                db,
                errors=errors,
                status_code=400,
            )

        ticket.status = TicketStatusEnum.VOID.value
        db.add(
            TicketVoid(
                ticket_id=ticket.id,
                reason_id=reason_id,
                note=note or "No note provided.",
                voided_at=utcnow(),
                voided_by=_voided_by_actor(request),
            )
        )
        audit_log(
            db,
            request,
            action="VOID",
            entity_type="ticket",
            entity_id=ticket.id,
            summary=f"Voided ticket {ticket.ticket_no}",
            details={
                "ticket_no": ticket.ticket_no,
                "reason_id": reason_id,
                "note": note or "No note provided.",
            },
        )
        db.commit()
        return RedirectResponse(url=f"/tickets/{ticket_id}?voided=1", status_code=303)

    ticket.direction = payload["direction"]
    ticket.transaction_type = payload["transaction_type"]
    weight_warning = _net_negative_values(payload["gross_kg"], payload["tare_kg"])
    payload["errors"].extend(_validate_ticket_entity_references(ticket, payload, db))
    lookup_errors = _validate_lookup_fields(ticket, payload, db)
    payload["errors"].extend(lookup_errors)
    _apply_ticket_defaults(db, payload)
    product = _validate_product_ewc(payload, db)
    ewc_snapshot = _resolve_ticket_ewc_snapshot(payload, db, product)
    _coerce_mode_fields(payload, product)
    weight_warning = _net_negative_values(payload["gross_kg"], payload["tare_kg"])
    weighing_order_errors = _validate_weighing_order(
        payload["direction"], payload["gross_kg"], payload["tare_kg"]
    )
    if (
        weighing_order_errors == ["Weigh-in (gross) is required before tare."]
        and payload.get("gross_kg") is None
        and payload.get("tare_kg") is not None
        and payload.get("vehicle_id")
    ):
        vehicle = db.get(Vehicle, payload["vehicle_id"])
        if vehicle and vehicle.default_tare_kg is not None:
            default_tare_kg = float(vehicle.default_tare_kg)
            if default_tare_kg > 0 and abs(payload["tare_kg"] - default_tare_kg) < 0.0001:
                weighing_order_errors = []
    payload["errors"].extend(weighing_order_errors)
    _apply_destination_default(ticket, payload, product)
    haulier = _validate_carrier_licence(payload, db, errors=payload["errors"])
    _resolve_waste_producer_snapshot(payload, db)
    is_waste_tx = _is_waste_transaction(payload.get("transaction_type"))
    if payload["errors"]:
        ticket.vehicle_reg_text = payload["vehicle_reg_text"]
        ticket.walk_in = payload["walk_in"]
        ticket.walk_in_sale = payload["walk_in_sale"]
        ticket.updated_at = utcnow()
        db.commit()
        return _render_ticket_edit(
            request,
            ticket,
            db,
            errors=payload["errors"],
            warnings=payload.get("warnings"),
            form=payload["form"],
            vehicle_reg=payload["vehicle_reg_text"],
            weight_warning=weight_warning,
            status_code=400,
        )

    _apply_ticket_updates(ticket, payload)
    _apply_ticket_pricing(ticket, payload, product)
    _apply_ticket_ewc_snapshot(ticket, ewc_snapshot)
    _apply_carrier_licence_snapshot(ticket, haulier)
    _apply_waste_producer_snapshot(ticket, payload)
    if original_snapshot is not None:
        updated_snapshot = _ticket_audit_snapshot(ticket)
        if updated_snapshot != original_snapshot:
            updated_audit_values = _ticket_audit_values(ticket)
            update_change_details = audit_diff(
                original_audit_values,
                updated_audit_values,
                _TICKET_AUDIT_DIFF_KEYS,
            )
            audit_log(
                db,
                request,
                action="UPDATE",
                entity_type="ticket",
                entity_id=ticket.id,
                summary=f"Updated ticket {ticket.ticket_no}",
                details=update_change_details,
            )
    db.commit()
    return RedirectResponse(url=f"/tickets/{ticket_id}?saved=1", status_code=303)


@router.post("/tickets/vehicle-suggestion/apply", response_class=HTMLResponse)
async def tickets_apply_vehicle_suggestion(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    _require_tickets_manage(request)
    form = await request.form()
    ticket_id = _parse_int(_form_value(form, "ticket_id"))
    if not ticket_id:
        return HTMLResponse("Ticket not found.", status_code=404)

    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return HTMLResponse("Ticket not found.", status_code=404)
    if _is_ticket_locked(ticket):
        return _render_vehicle_suggestions(
            request,
            ticket,
            None,
            None,
            None,
            None,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            errors=[_ticket_locked_message(ticket)],
            status_code=400,
        )

    reg = _form_value(form, "reg")
    if not reg:
        return _render_vehicle_suggestions(
            request,
            ticket,
            None,
            None,
            None,
            None,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            errors=["Registration is required."],
            status_code=400,
        )

    vehicle = _find_vehicle_by_reg(db, reg)
    if not vehicle:
        return _render_vehicle_suggestions(
            request,
            ticket,
            None,
            None,
            None,
            None,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            errors=["Vehicle not found."],
            status_code=404,
        )

    default_customer = (
        db.get(Customer, vehicle.default_customer_id)
        if vehicle.default_customer_id
        else None
    )
    default_haulier = (
        db.get(Haulier, vehicle.default_haulier_id)
        if vehicle.default_haulier_id
        else None
    )
    default_driver = (
        db.get(Driver, vehicle.default_driver_id)
        if vehicle.default_driver_id
        else None
    )
    suggested_tare_kg = None
    if vehicle.default_tare_kg is not None:
        candidate_tare = Decimal(str(vehicle.default_tare_kg))
        if candidate_tare > 0:
            suggested_tare_kg = candidate_tare

    errors: list[str] = []
    warnings: list[str] = []
    applied = False
    tare_applied = False

    if ticket.vehicle_id != vehicle.id:
        ticket.vehicle_id = vehicle.id
        applied = True
    if (ticket.vehicle_reg_text or "") != vehicle.registration:
        ticket.vehicle_reg_text = vehicle.registration
        applied = True

    if default_customer and ticket.customer_id is None:
        if ticket.customer_id != default_customer.id:
            ticket.customer_id = default_customer.id
            applied = True
        if default_customer.on_stop:
            warnings.append(
                "Customer is ON STOP - allowed to record ticket; cannot complete/invoice."
            )

    if default_haulier and ticket.haulier_id is None:
        if not default_haulier.is_active:
            errors.append("Suggested haulier is inactive.")
        else:
            if ticket.haulier_id != default_haulier.id:
                ticket.haulier_id = default_haulier.id
                applied = True
            if _is_haulier_on_stop(default_haulier):
                warnings.append(
                    "Haulier is ON STOP - allowed to record ticket; cannot complete/invoice."
                )

    if default_driver and ticket.driver_id is None:
        if not default_driver.is_active:
            errors.append("Suggested driver is inactive.")
        elif ticket.driver_id != default_driver.id:
            ticket.driver_id = default_driver.id
            applied = True

    if suggested_tare_kg is not None:
        current_tare = Decimal(str(ticket.tare_kg)) if ticket.tare_kg is not None else None
        if current_tare is None or current_tare != suggested_tare_kg:
            ticket.tare_kg = suggested_tare_kg
            applied = True
            tare_applied = True
        gross_value = Decimal(str(ticket.gross_kg)) if ticket.gross_kg is not None else None
        ticket.net_kg = (
            gross_value - ticket.tare_kg
            if gross_value is not None and ticket.tare_kg is not None
            else None
        )

    if not applied:
        return _render_vehicle_suggestions(
            request,
            ticket,
            vehicle,
            default_customer,
            default_haulier,
            default_driver,
            suggested_tare_kg=suggested_tare_kg,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            warnings=warnings,
            errors=errors or ["No defaults available to apply for this vehicle."],
            status_code=400,
        )

    ticket.updated_at = utcnow()
    db.commit()

    options = _load_ticket_options(db)
    active_options = _active_lookup_options(ticket, db)
    return _render_vehicle_suggestions(
        request,
        ticket,
        vehicle,
        default_customer,
        default_haulier,
        default_driver,
        suggested_tare_kg=suggested_tare_kg,
        ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
        current_customer_id=ticket.customer_id,
        current_haulier_id=ticket.haulier_id,
        current_driver_id=ticket.driver_id,
        oob_customer_options=options["customers"],
        oob_haulier_options=active_options["hauliers"],
        oob_driver_options=active_options["drivers"],
        oob_weights_form=_ticket_to_form(ticket) if tare_applied else None,
        oob_tare_auto_hint="Tare applied from vehicle default." if tare_applied else None,
        oob_status_warnings=warnings,
        show_panel=False,
        warnings=warnings,
        errors=errors,
    )


@router.post("/tickets/vehicle-suggestion/dismiss", response_class=HTMLResponse)
def tickets_dismiss_vehicle_suggestion(request: Request) -> HTMLResponse:
    _require_tickets_manage(request)
    return HTMLResponse("", status_code=200)


@router.post("/tickets/{ticket_id}/apply-default-customer", response_class=HTMLResponse)
async def tickets_apply_default_customer(
    ticket_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    _require_tickets_manage(request)
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return HTMLResponse("Ticket not found.", status_code=404)
    if _is_ticket_locked(ticket):
        return _render_vehicle_suggestions(
            request,
            ticket,
            None,
            None,
            None,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            errors=[_ticket_locked_message(ticket)],
            status_code=400,
        )
    vehicle = await _resolve_vehicle_for_defaults(request, ticket, db)
    if not vehicle:
        return _render_vehicle_suggestions(
            request,
            ticket,
            None,
            None,
            None,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            errors=["Vehicle must be selected to apply defaults."],
            status_code=400,
        )

    default_customer = (
        db.get(Customer, vehicle.default_customer_id)
        if vehicle.default_customer_id
        else None
    )
    default_haulier = (
        db.get(Haulier, vehicle.default_haulier_id)
        if vehicle.default_haulier_id
        else None
    )
    warnings: list[str] = []

    if ticket.customer_id:
        return _render_vehicle_suggestions(
            request,
            ticket,
            vehicle,
            default_customer,
            default_haulier,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            errors=["Customer already set."],
            status_code=400,
        )
    if not default_customer:
        return _render_vehicle_suggestions(
            request,
            ticket,
            vehicle,
            default_customer,
            default_haulier,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            errors=["Default customer not configured for this vehicle."],
            status_code=400,
        )
    if default_customer.on_stop:
        warnings.append(
            "Customer is ON STOP - allowed to record ticket; cannot complete/invoice."
        )

    ticket.customer_id = default_customer.id
    ticket.updated_at = utcnow()
    db.commit()
    oob_customer_options, oob_haulier_options = _oob_lookup_options(ticket, db)
    show_panel = not (ticket.haulier_id is not None or default_haulier is None)
    return _render_vehicle_suggestions(
        request,
        ticket,
        vehicle,
        default_customer,
        default_haulier,
        ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
        oob_customer_options=oob_customer_options,
        oob_haulier_options=oob_haulier_options,
        oob_status_warnings=warnings,
        show_panel=show_panel,
        warnings=warnings,
    )


@router.post("/tickets/{ticket_id}/apply-default-haulier", response_class=HTMLResponse)
async def tickets_apply_default_haulier(
    ticket_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    _require_tickets_manage(request)
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return HTMLResponse("Ticket not found.", status_code=404)
    if _is_ticket_locked(ticket):
        return _render_vehicle_suggestions(
            request,
            ticket,
            None,
            None,
            None,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            errors=[_ticket_locked_message(ticket)],
            status_code=400,
        )
    vehicle = await _resolve_vehicle_for_defaults(request, ticket, db)
    if not vehicle:
        return _render_vehicle_suggestions(
            request,
            ticket,
            None,
            None,
            None,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            errors=["Vehicle must be selected to apply defaults."],
            status_code=400,
        )

    default_customer = (
        db.get(Customer, vehicle.default_customer_id)
        if vehicle.default_customer_id
        else None
    )
    default_haulier = (
        db.get(Haulier, vehicle.default_haulier_id)
        if vehicle.default_haulier_id
        else None
    )

    if ticket.haulier_id:
        return _render_vehicle_suggestions(
            request,
            ticket,
            vehicle,
            default_customer,
            default_haulier,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            errors=["Haulier already set."],
            status_code=400,
        )
    if not default_haulier:
        return _render_vehicle_suggestions(
            request,
            ticket,
            vehicle,
            default_customer,
            default_haulier,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            errors=["Default haulier not configured for this vehicle."],
            status_code=400,
        )
    if not default_haulier.is_active:
        return _render_vehicle_suggestions(
            request,
            ticket,
            vehicle,
            default_customer,
            default_haulier,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            errors=["Default haulier is inactive."],
            status_code=400,
        )

    warnings: list[str] = []
    ticket.haulier_id = default_haulier.id
    if _is_haulier_on_stop(default_haulier):
        warnings.append(
            "Haulier is ON STOP - allowed to record ticket; cannot complete/invoice."
        )
    ticket.updated_at = utcnow()
    db.commit()
    oob_customer_options, oob_haulier_options = _oob_lookup_options(ticket, db)
    show_panel = not (ticket.customer_id is not None or default_customer is None)
    return _render_vehicle_suggestions(
        request,
        ticket,
        vehicle,
        default_customer,
        default_haulier,
        ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
        oob_customer_options=oob_customer_options,
        oob_haulier_options=oob_haulier_options,
        oob_status_warnings=warnings,
        show_panel=show_panel,
        warnings=warnings,
    )


@router.post("/tickets/{ticket_id}/apply-defaults", response_class=HTMLResponse)
async def tickets_apply_defaults(
    ticket_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    _require_tickets_manage(request)
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return HTMLResponse("Ticket not found.", status_code=404)
    if _is_ticket_locked(ticket):
        return _render_vehicle_suggestions(
            request,
            ticket,
            None,
            None,
            None,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            errors=[_ticket_locked_message(ticket)],
            status_code=400,
        )
    vehicle = await _resolve_vehicle_for_defaults(request, ticket, db)
    if not vehicle:
        return _render_vehicle_suggestions(
            request,
            ticket,
            None,
            None,
            None,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            errors=["Vehicle must be selected to apply defaults."],
            status_code=400,
        )

    default_customer = (
        db.get(Customer, vehicle.default_customer_id)
        if vehicle.default_customer_id
        else None
    )
    default_haulier = (
        db.get(Haulier, vehicle.default_haulier_id)
        if vehicle.default_haulier_id
        else None
    )

    errors: list[str] = []
    warnings: list[str] = []
    updates = 0

    if ticket.customer_id:
        errors.append("Customer already set.")
    elif not default_customer:
        errors.append("Default customer not configured for this vehicle.")
    elif default_customer.on_stop:
        ticket.customer_id = default_customer.id
        updates += 1
        warnings.append(
            "Customer is ON STOP - allowed to record ticket; cannot complete/invoice."
        )
    else:
        ticket.customer_id = default_customer.id
        updates += 1

    if ticket.haulier_id:
        errors.append("Haulier already set.")
    elif not default_haulier:
        errors.append("Default haulier not configured for this vehicle.")
    elif not default_haulier.is_active:
        errors.append("Default haulier is inactive.")
    else:
        ticket.haulier_id = default_haulier.id
        updates += 1
        if _is_haulier_on_stop(default_haulier):
            warnings.append(
                "Haulier is ON STOP - allowed to record ticket; cannot complete/invoice."
            )

    if updates:
        ticket.updated_at = utcnow()
        db.commit()
        oob_customer_options, oob_haulier_options = _oob_lookup_options(ticket, db)
        show_panel = bool(errors)
        return _render_vehicle_suggestions(
            request,
            ticket,
            vehicle,
            default_customer,
            default_haulier,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            oob_customer_options=oob_customer_options,
            oob_haulier_options=oob_haulier_options,
            oob_status_warnings=warnings,
            show_panel=show_panel,
            warnings=warnings,
            errors=errors,
        )

    return _render_vehicle_suggestions(
        request,
        ticket,
        vehicle,
        default_customer,
        default_haulier,
        ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
        warnings=warnings,
        errors=errors or ["No defaults applied."],
        status_code=400,
    )


@router.post("/tickets/{ticket_id}/set-vehicle-from-reg", response_class=HTMLResponse)
async def tickets_set_vehicle_from_reg(
    ticket_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    _require_tickets_manage(request)
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return HTMLResponse("Ticket not found.", status_code=404)
    if _is_ticket_locked(ticket):
        return _render_vehicle_suggestions(
            request,
            ticket,
            None,
            None,
            None,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            errors=[_ticket_locked_message(ticket)],
            status_code=400,
        )

    form = await request.form()
    reg = str(form.get("reg", "")).strip()
    if not reg:
        return _render_vehicle_suggestions(
            request,
            ticket,
            None,
            None,
            None,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            errors=["Registration is required."],
            status_code=400,
        )

    vehicle = _find_vehicle_by_reg(db, reg)
    if not vehicle:
        return _render_vehicle_suggestions(
            request,
            ticket,
            None,
            None,
            None,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            errors=["Vehicle not found."],
            status_code=404,
        )

    default_customer = (
        db.get(Customer, vehicle.default_customer_id)
        if vehicle.default_customer_id
        else None
    )
    default_haulier = (
        db.get(Haulier, vehicle.default_haulier_id)
        if vehicle.default_haulier_id
        else None
    )

    if ticket.vehicle_id:
        current_vehicle = db.get(Vehicle, ticket.vehicle_id)
        if current_vehicle and current_vehicle.id == vehicle.id:
            return _render_vehicle_suggestions(
                request,
                ticket,
                vehicle,
                default_customer,
                default_haulier,
                ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
                status_code=200,
            )
        current_reg = current_vehicle.registration if current_vehicle else "(unknown)"
        return _render_vehicle_suggestions(
            request,
            ticket,
            vehicle,
            default_customer,
            default_haulier,
            ticket_vehicle_reg=current_reg,
            errors=[
                (
                    "Ticket is linked to "
                    f"{current_reg}. This registration matches {vehicle.registration}."
                )
            ],
            status_code=400,
        )

    ticket.vehicle_id = vehicle.id
    ticket.updated_at = utcnow()
    db.commit()
    return _render_vehicle_suggestions(
        request,
        ticket,
        vehicle,
        default_customer,
        default_haulier,
        ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
    )


@router.post("/tickets/{ticket_id}/weights/gross", response_class=HTMLResponse)
async def tickets_capture_gross(
    ticket_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    _require_tickets_manage(request)
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return HTMLResponse("Ticket not found.", status_code=404)
    if _is_ticket_locked(ticket):
        return _render_weights_partial(
            request,
            ticket,
            errors=[_ticket_locked_message(ticket)],
            status_code=400,
        )
    if ticket.gross_kg is not None:
        return _render_weights_partial(
            request, ticket, errors=["Gross weight already recorded."], status_code=400
        )
    if _expected_weigh_in_field(ticket.direction) == "tare_kg" and ticket.tare_kg is None:
        return _render_weights_partial(
            request,
            ticket,
            errors=["Weigh-in (tare) is required before gross."],
            status_code=400,
        )

    form = await request.form()
    errors: list[str] = []
    gross_value = _parse_weight_value(
        _form_value(form, "weight_value"), "Gross weight", errors
    )
    if gross_value is None:
        if not errors:
            errors.append("Gross weight is required.")
        return _render_weights_partial(
            request, ticket, errors=errors, status_code=400
        )

    ticket.gross_kg = gross_value
    ticket.net_kg = (
        ticket.gross_kg - ticket.tare_kg
        if ticket.gross_kg is not None and ticket.tare_kg is not None
        else None
    )
    ticket.updated_at = utcnow()
    db.commit()
    return _render_weights_partial(request, ticket, errors=[])




@router.post("/tickets/{ticket_id}/weights/tare", response_class=HTMLResponse)
async def tickets_capture_tare(
    ticket_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    _require_tickets_manage(request)
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return HTMLResponse("Ticket not found.", status_code=404)
    if _is_ticket_locked(ticket):
        return _render_weights_partial(
            request,
            ticket,
            errors=[_ticket_locked_message(ticket)],
            status_code=400,
        )
    if ticket.tare_kg is not None:
        return _render_weights_partial(
            request, ticket, errors=["Tare weight already recorded."], status_code=400
        )
    if _expected_weigh_in_field(ticket.direction) == "gross_kg" and ticket.gross_kg is None:
        return _render_weights_partial(
            request,
            ticket,
            errors=["Weigh-in (gross) is required before tare."],
            status_code=400,
        )

    form = await request.form()
    errors: list[str] = []
    tare_value = _parse_weight_value(
        _form_value(form, "weight_value"), "Tare weight", errors
    )
    if tare_value is None:
        if not errors:
            errors.append("Tare weight is required.")
        return _render_weights_partial(
            request, ticket, errors=errors, status_code=400
        )

    ticket.tare_kg = tare_value
    ticket.net_kg = (
        ticket.gross_kg - ticket.tare_kg
        if ticket.gross_kg is not None and ticket.tare_kg is not None
        else None
    )
    ticket.updated_at = utcnow()
    db.commit()
    return _render_weights_partial(request, ticket, errors=[])


@router.post("/tickets/weights/read", response_class=HTMLResponse)
async def tickets_read_weight(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    _require_tickets_manage(request)
    form = await request.form()
    ticket_id = _parse_int(_form_value(form, "ticket_id"))
    if not ticket_id:
        return HTMLResponse("Ticket not found.", status_code=404)

    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return HTMLResponse("Ticket not found.", status_code=404)
    if _is_ticket_locked(ticket):
        return _render_weights_partial(
            request,
            ticket,
            errors=[_ticket_locked_message(ticket)],
            status_code=400,
        )
    return _render_weights_partial(
        request,
        ticket,
        errors=["Not implemented: live weighbridge readout integration is not wired yet."],
        status_code=400,
    )


@router.post("/tickets/weights/read-apply", response_class=HTMLResponse)
async def tickets_read_weight_apply(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    _require_tickets_manage(request)
    form = await request.form()
    ticket_id = _parse_int(_form_value(form, "ticket_id"))
    if not ticket_id:
        return HTMLResponse("Ticket not found.", status_code=404)

    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return HTMLResponse("Ticket not found.", status_code=404)
    if _is_ticket_locked(ticket):
        return _render_weights_partial(
            request,
            ticket,
            errors=[_ticket_locked_message(ticket)],
            status_code=400,
        )
    return _render_weights_partial(
        request,
        ticket,
        errors=["Not implemented: live weighbridge readout integration is not wired yet."],
        status_code=400,
    )


@router.post("/tickets/weights/swap-preview", response_class=HTMLResponse)
async def tickets_swap_weights_preview(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    _require_tickets_manage(request)
    form = await request.form()
    ticket_id = _parse_int(_form_value(form, "ticket_id"))
    if not ticket_id:
        return HTMLResponse("Ticket not found.", status_code=404)

    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return HTMLResponse("Ticket not found.", status_code=404)
    if _is_ticket_locked(ticket):
        return _render_weights_partial(
            request,
            ticket,
            errors=[_ticket_locked_message(ticket)],
            status_code=400,
        )

    gross_raw = _form_value(form, "gross_kg")
    tare_raw = _form_value(form, "tare_kg")
    product_id = (
        _parse_int(_first_non_empty_form_value(form, "product_id"))
        or _parse_int(_first_non_empty_form_value(form, "weights_product_id"))
        or ticket.product_id
    )
    product = db.get(Product, product_id) if product_id else None
    unit_type = product.unit.unit_type if product and product.unit else ""
    weights_locked = str(unit_type or "").upper() == "COUNT"
    errors: list[str] = []

    gross_value = _parse_weight_value(gross_raw, "Gross weight", errors)
    tare_value = _parse_weight_value(tare_raw, "Tare weight", errors)

    if weights_locked:
        net_value = (
            gross_value - tare_value
            if gross_value is not None and tare_value is not None
            else None
        )
        form_data = _weights_form_from_values(
            ticket=ticket,
            gross_raw=gross_raw,
            tare_raw=tare_raw,
            net_value=net_value,
        )
        form_data["product_id"] = str(product_id or "")
        return templates.TemplateResponse(
            request,
            "tickets/_weights_preview.html",
            {
                "request": request,
                "ticket": ticket,
                "errors": ["Swap weights not available for COUNT products."],
                "is_admin": False,
                "is_open": _is_open_ticket(ticket),
                "weights_locked": True,
                "unit_type": unit_type,
                "form": form_data,
                "show_weight_errors": True,
                "weight_warning": _net_negative_values(gross_value, tare_value),
            },
            status_code=400,
        )

    if gross_value is None and not gross_raw:
        errors.append("Gross weight is required.")
    if tare_value is None and not tare_raw:
        errors.append("Tare weight is required.")

    if errors:
        form_data = _weights_form_from_values(
            ticket=ticket,
            gross_raw=gross_raw,
            tare_raw=tare_raw,
            net_value=None,
        )
        form_data["product_id"] = str(product_id or "")
        return templates.TemplateResponse(
            request,
            "tickets/_weights_preview.html",
            {
                "request": request,
                "ticket": ticket,
                "errors": errors,
                "is_admin": False,
                "is_open": _is_open_ticket(ticket),
                "weights_locked": False,
                "unit_type": unit_type,
                "form": form_data,
                "show_weight_errors": True,
                "weight_warning": _net_negative_values(gross_value, tare_value),
            },
            status_code=400,
        )

    swapped_gross = tare_raw
    swapped_tare = gross_raw
    net_value = tare_value - gross_value
    form_data = _weights_form_from_values(
        ticket=ticket,
        gross_raw=swapped_gross,
        tare_raw=swapped_tare,
        net_value=net_value,
    )
    form_data["product_id"] = str(product_id or "")

    return templates.TemplateResponse(
        request,
        "tickets/_weights_preview.html",
        {
            "request": request,
            "ticket": ticket,
            "errors": [],
            "is_admin": False,
            "is_open": _is_open_ticket(ticket),
            "weights_locked": False,
            "unit_type": unit_type,
            "form": form_data,
            "show_weight_errors": True,
            "weight_warning": _net_negative_values(tare_value, gross_value),
        },
    )


def _ticket_audit_snapshot(ticket: Ticket) -> tuple:
    return (
        str(ticket.status),
        str(ticket.direction),
        str(ticket.transaction_type),
        ticket.datetime,
        ticket.customer_id,
        ticket.vehicle_id,
        ticket.vehicle_reg_text,
        ticket.product_id,
        ticket.haulier_id,
        ticket.driver_id,
        ticket.container_id,
        ticket.destination_id,
        ticket.yard_id,
        ticket.area_id,
        ticket.gross_kg,
        ticket.tare_kg,
        ticket.net_kg,
        ticket.qty,
        ticket.unit_price,
        ticket.total,
        ticket.po_number,
        bool(ticket.final_disposal),
        bool(ticket.used_on_site),
        bool(ticket.dont_invoice),
    )


def _ticket_audit_values(ticket: Ticket) -> dict[str, object]:
    return {
        "status": _status_value(ticket.status),
        "product_id": ticket.product_id,
        "customer_id": ticket.customer_id,
        "final_disposal": bool(ticket.final_disposal),
        "used_on_site": bool(ticket.used_on_site),
        "net_kg": ticket.net_kg,
        "qty": ticket.qty,
        "unit_price": ticket.unit_price,
        "total": ticket.total,
    }


def _apply_ticket_updates(ticket: Ticket, payload: dict) -> None:
    ticket.datetime = payload["ticket_datetime"]
    ticket.status = payload["status"]
    ticket.customer_id = payload["customer_id"]
    ticket.vehicle_id = payload["vehicle_id"]
    ticket.vehicle_reg_text = payload["vehicle_reg_text"]
    ticket.walk_in = payload["walk_in"]
    ticket.walk_in_sale = payload["walk_in_sale"]
    ticket.final_disposal = bool(payload.get("final_disposal"))
    ticket.used_on_site = bool(payload.get("used_on_site"))
    ticket.product_id = payload["product_id"]
    ticket.haulier_id = payload["haulier_id"]
    ticket.driver_id = payload["driver_id"]
    ticket.container_id = payload["container_id"]
    ticket.destination_id = payload["destination_id"]
    ticket.yard_id = payload["yard_id"]
    ticket.area_id = payload["area_id"]
    ticket.waste_code_id = payload["waste_code_id"]
    ticket.waste_producer_customer_id = None
    source_value = payload.get("waste_producer_source")
    ticket.waste_producer_source = (
        WasteProducerSourceEnum(source_value) if source_value else None
    )
    ticket.gross_kg = payload["gross_kg"]
    ticket.tare_kg = payload["tare_kg"]
    ticket.net_kg = payload["net_kg"]
    ticket.qty = payload["qty"]
    ticket.unit_id = payload["unit_id"]
    ticket.unit_price = payload["unit_price"]
    ticket.total = payload["total"]
    ticket.po_number = payload["po_number"]
    ticket.dont_invoice = payload["dont_invoice"]
    ticket.updated_at = utcnow()


def _parse_ticket_form(
    form,
    current_status: str | None = None,
    *,
    validate_ewc: bool = True,
) -> dict:
    errors: list[str] = []

    datetime_raw = _form_value(form, "datetime")
    direction_raw = _form_value(form, "direction")
    transaction_type_raw = _form_value(form, "transaction_type")
    direction = direction_raw or None
    transaction_type = transaction_type_raw or None
    status = current_status or TicketStatusEnum.OPEN.value
    customer_id = _parse_int(_form_value(form, "customer_id"))
    po_number_raw = _form_value(form, "po_number")
    vehicle_id = _parse_int(_form_value(form, "vehicle_id"))
    vehicle_reg_text = _normalize_reg_text(_form_value(form, "reg"))
    walk_in = _form_value(form, "walk_in") == "on"
    walk_in_sale = _form_value(form, "walk_in_sale") == "on"
    final_disposal = _form_value(form, "final_disposal") == "on"
    used_on_site = _form_value(form, "used_on_site") == "on"
    product_id_raw = _first_non_empty_form_value(form, "product_id")
    product_id = _parse_int(product_id_raw)
    same_as_customer_present = bool(
        _form_value(form, "waste_producer_same_as_customer_present")
    )
    if same_as_customer_present:
        waste_producer_same_as_customer = (
            _form_value(form, "waste_producer_same_as_customer").lower()
            in {"on", "1", "true", "yes"}
        )
    else:
        # Backward compatibility for callers that do not yet send the new UI fields.
        waste_producer_same_as_customer = True
    waste_producer_name = _form_value(form, "waste_producer_name")
    waste_producer_address_line_1 = _form_value(form, "waste_producer_address_line_1")
    waste_producer_address_line_2 = _form_value(form, "waste_producer_address_line_2")
    waste_producer_address_line_3 = _form_value(form, "waste_producer_address_line_3")
    waste_producer_postcode = _form_value(form, "waste_producer_postcode")
    ewc_code_raw = _form_value(form, "ewc_code")
    ewc_manual_override = _form_value(form, "ewc_manual_override").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    ewc_hazardous = _form_value(form, "ewc_hazardous") == "1"
    ewc_auto_code_6 = _normalize_ewc_digits(_form_value(form, "ewc_auto_code_6"))
    ewc_product_default_code_6 = _normalize_ewc_digits(
        _form_value(form, "ewc_product_default_code_6")
    )
    ewc_product_default_display = _form_value(form, "ewc_product_default_display")
    yard_raw = _form_value(form, "yard_id")
    area_raw = _form_value(form, "area_id")

    validate_no_html_fields(
        {
            "Vehicle registration": vehicle_reg_text,
            "Yard": yard_raw,
            "Area": area_raw,
            "Waste producer name": waste_producer_name,
            "Waste producer address line 1": waste_producer_address_line_1,
            "Waste producer address line 2": waste_producer_address_line_2,
            "Waste producer address line 3": waste_producer_address_line_3,
            "Waste producer postcode": waste_producer_postcode,
            "EWC code": ewc_code_raw,
        },
        errors,
    )

    if vehicle_reg_text and len(vehicle_reg_text) > REG_MAX:
        errors.append(f"Vehicle registration must be {REG_MAX} characters or fewer.")
    if waste_producer_name and len(waste_producer_name) > NAME_MAX:
        errors.append(f"Waste producer name must be {NAME_MAX} characters or fewer.")
    if waste_producer_address_line_1 and len(waste_producer_address_line_1) > ADDRESS_LINE_MAX:
        errors.append(
            f"Waste producer address line 1 must be {ADDRESS_LINE_MAX} characters or fewer."
        )
    if waste_producer_address_line_2 and len(waste_producer_address_line_2) > ADDRESS_LINE_MAX:
        errors.append(
            f"Waste producer address line 2 must be {ADDRESS_LINE_MAX} characters or fewer."
        )
    if waste_producer_address_line_3 and len(waste_producer_address_line_3) > ADDRESS_LINE_MAX:
        errors.append(
            f"Waste producer address line 3 must be {ADDRESS_LINE_MAX} characters or fewer."
        )
    if waste_producer_postcode and len(waste_producer_postcode) > POSTCODE_MAX:
        errors.append(
            f"Waste producer postcode must be {POSTCODE_MAX} characters or fewer."
        )

    if not datetime_raw:
        errors.append("Date/time is required.")
    # Customer/vehicle/product can be left blank on open tickets.

    if direction and direction not in _ticket_enums()["directions"]:
        errors.append("Direction must be INWARD or OUTWARD.")
    if transaction_type and transaction_type not in _ticket_enums()["transaction_types"]:
        errors.append("Transaction type is invalid.")
    if status and status not in _ticket_enums()["statuses"]:
        errors.append("Status is invalid.")

    ticket_datetime: datetime | None = None
    if datetime_raw:
        try:
            ticket_datetime = datetime.fromisoformat(datetime_raw)
        except ValueError:
            errors.append("Date/time must be valid.")

    gross_raw = _form_value(form, "gross_kg")
    tare_raw = _form_value(form, "tare_kg")
    gross_kg = _parse_weight_value(gross_raw, "Gross weight", errors)
    tare_kg = _parse_weight_value(tare_raw, "Tare weight", errors)
    qty = _parse_float(_form_value(form, "qty"))
    unit_price_raw = _form_value(form, "unit_price")
    unit_price = _parse_decimal(unit_price_raw)
    net_kg = (
        gross_kg - tare_kg if gross_kg is not None and tare_kg is not None else None
    )
    total = (
        Decimal(str(qty)) * unit_price
        if qty is not None and unit_price is not None
        else None
    )
    if validate_ewc:
        ewc_code_6 = _parse_ewc_code_value(ewc_code_raw, errors)
    else:
        normalized_ewc_code = _normalize_ewc_digits(ewc_code_raw)
        ewc_code_6 = normalized_ewc_code if len(normalized_ewc_code) == 6 else None
    dont_invoice = _form_value(form, "dont_invoice") == "on"
    po_number = _parse_po_number(po_number_raw, errors)

    form_data = {
        "datetime": datetime_raw,
        "direction": direction or "",
        "transaction_type": transaction_type or "",
        "status": status,
        "customer_id": _form_value(form, "customer_id"),
        "po_number": po_number_raw,
        "vehicle_id": _form_value(form, "vehicle_id"),
        "vehicle_reg_text": vehicle_reg_text,
        "walk_in": "on" if walk_in else "",
        "walk_in_sale": "on" if walk_in_sale else "",
        "final_disposal": "on" if final_disposal else "",
        "used_on_site": "on" if used_on_site else "",
        "product_id": product_id_raw,
        "haulier_id": _form_value(form, "haulier_id"),
        "driver_id": _form_value(form, "driver_id"),
        "container_id": _form_value(form, "container_id"),
        "destination_id": _form_value(form, "destination_id"),
        "yard_id": _form_value(form, "yard_id"),
        "area_id": _form_value(form, "area_id"),
        "waste_code_id": _form_value(form, "waste_code_id"),
        "waste_producer_same_as_customer": "on"
        if waste_producer_same_as_customer
        else "",
        "waste_producer_name": waste_producer_name,
        "waste_producer_address_line_1": waste_producer_address_line_1,
        "waste_producer_address_line_2": waste_producer_address_line_2,
        "waste_producer_address_line_3": waste_producer_address_line_3,
        "waste_producer_postcode": waste_producer_postcode,
        "ewc_code": _format_ewc_code_display(ewc_code_6)
        if ewc_code_6
        else ewc_code_raw,
        "ewc_manual_override": "1" if ewc_manual_override else "0",
        "ewc_hazardous": "1" if ewc_hazardous else "0",
        "ewc_auto_code_6": _format_ewc_code_display(ewc_auto_code_6)
        if ewc_auto_code_6
        else "",
        "ewc_product_default_code_6": ewc_product_default_code_6,
        "ewc_product_default_display": ewc_product_default_display,
        "gross_kg": gross_raw if gross_kg is None else f"{gross_kg:.0f}",
        "tare_kg": tare_raw if tare_kg is None else f"{tare_kg:.0f}",
        "net_kg": f"{net_kg:.0f}" if net_kg is not None else "",
        "qty": _form_value(form, "qty"),
        "unit_id": _form_value(form, "unit_id"),
        "unit_price": _form_value(form, "unit_price"),
        "total": f"{total:.2f}" if total is not None else "",
        "dont_invoice": "on" if dont_invoice else "",
    }

    return {
        "errors": errors,
        "warnings": [],
        "form": form_data,
        "ticket_datetime": ticket_datetime or utcnow(),
        "direction": direction,
        "transaction_type": transaction_type,
        "direction_raw": direction_raw,
        "transaction_type_raw": transaction_type_raw,
        "status": status,
        "customer_id": customer_id,
        "po_number": po_number,
        "vehicle_id": vehicle_id,
        "vehicle_reg_text": vehicle_reg_text,
        "walk_in": walk_in,
        "walk_in_sale": walk_in_sale,
        "final_disposal": final_disposal,
        "used_on_site": used_on_site,
        "product_id": product_id,
        "haulier_id": _parse_int(_form_value(form, "haulier_id")),
        "driver_id": _parse_int(_form_value(form, "driver_id")),
        "container_id": _parse_int(_form_value(form, "container_id")),
        "destination_id": _parse_int(_form_value(form, "destination_id")),
        "yard_id": _parse_int(_form_value(form, "yard_id")),
        "area_id": _parse_int(_form_value(form, "area_id")),
        "waste_code_id": _parse_int(_form_value(form, "waste_code_id")),
        "waste_producer_same_as_customer": waste_producer_same_as_customer,
        "waste_producer_name": waste_producer_name,
        "waste_producer_address_line_1": waste_producer_address_line_1,
        "waste_producer_address_line_2": waste_producer_address_line_2,
        "waste_producer_address_line_3": waste_producer_address_line_3,
        "waste_producer_postcode": waste_producer_postcode,
        "ewc_code_raw": ewc_code_raw,
        "ewc_code_6": ewc_code_6,
        "ewc_manual_override": ewc_manual_override,
        "ewc_hazardous": ewc_hazardous,
        "ewc_auto_code_6": ewc_auto_code_6,
        "ewc_product_default_code_6": ewc_product_default_code_6,
        "ewc_product_default_display": ewc_product_default_display,
        "waste_producer_source": (
            WasteProducerSourceEnum.CUSTOMER.value
            if waste_producer_same_as_customer
            else WasteProducerSourceEnum.MANUAL.value
        ),
        "gross_kg": gross_kg,
        "tare_kg": tare_kg,
        "net_kg": net_kg,
        "qty": qty,
        "unit_id": _parse_int(_form_value(form, "unit_id")),
        "unit_price": unit_price,
        "total": total,
        "dont_invoice": dont_invoice,
        "unit_price_raw": unit_price_raw,
    }


def _ticket_to_form(ticket: Ticket) -> dict:
    source_value = (
        ticket.waste_producer_source.value
        if hasattr(ticket.waste_producer_source, "value")
        else str(ticket.waste_producer_source or "")
    )
    if source_value:
        same_as_customer = source_value == WasteProducerSourceEnum.CUSTOMER.value
    elif ticket.waste_producer_customer_id is not None:
        same_as_customer = True
    elif ticket.waste_producer_name or ticket.waste_producer_address:
        same_as_customer = False
    else:
        same_as_customer = True
    if same_as_customer:
        producer_name = ""
        producer_line_1 = ""
        producer_line_2 = ""
        producer_line_3 = ""
        producer_postcode = ""
    else:
        producer_name = ticket.waste_producer_name or ""
        (
            producer_line_1,
            producer_line_2,
            producer_line_3,
            producer_postcode,
        ) = _split_waste_producer_address(ticket.waste_producer_address)
    ticket_ewc_code_6 = _normalize_ewc_digits(ticket.ewc_code_6)
    product_ewc = ticket.product.ewc_code if ticket.product and ticket.product.ewc_code else None
    product_default_ewc_code_6 = _normalize_ewc_digits(
        product_ewc.code_6 if product_ewc else ""
    )
    product_default_ewc_display = (
        product_ewc.code_display
        if product_ewc and product_ewc.code_display
        else _format_ewc_code_display(product_default_ewc_code_6)
    )
    ewc_value = ""
    if ticket_ewc_code_6:
        ewc_value = (
            ticket.ewc_code_display
            or _format_ewc_code_display(ticket_ewc_code_6)
        )
    elif product_default_ewc_code_6:
        ewc_value = product_default_ewc_display
    ewc_manual_override = bool(ticket.ewc_manual_override)
    ewc_auto_code_6 = ""
    if ticket_ewc_code_6 and not ewc_manual_override:
        ewc_auto_code_6 = ticket_ewc_code_6
    elif not ticket_ewc_code_6 and product_default_ewc_code_6:
        ewc_auto_code_6 = product_default_ewc_code_6
    return {
        "datetime": ticket.datetime.isoformat(timespec="minutes")
        if ticket.datetime
        else "",
        "direction": ticket.direction.value if ticket.direction else "",
        "transaction_type": ticket.transaction_type.value if ticket.transaction_type else "",
        "status": ticket.status.value if ticket.status else "",
        "customer_id": str(ticket.customer_id or ""),
        "po_number": ticket.po_number or "",
        "vehicle_id": str(ticket.vehicle_id or ""),
        "vehicle_reg_text": ticket.vehicle_reg_text or "",
        "walk_in": "on" if ticket.walk_in else "",
        "walk_in_sale": "on" if ticket.walk_in_sale else "",
        "final_disposal": "on" if ticket.final_disposal else "",
        "used_on_site": "on" if ticket.used_on_site else "",
        "product_id": str(ticket.product_id or ""),
        "haulier_id": str(ticket.haulier_id or ""),
        "driver_id": str(ticket.driver_id or ""),
        "container_id": str(ticket.container_id or ""),
        "destination_id": str(ticket.destination_id or ""),
        "yard_id": str(ticket.yard_id or ""),
        "area_id": str(ticket.area_id or ""),
        "waste_code_id": str(ticket.waste_code_id or ""),
        "waste_producer_same_as_customer": "on" if same_as_customer else "",
        "waste_producer_name": producer_name,
        "waste_producer_address_line_1": producer_line_1,
        "waste_producer_address_line_2": producer_line_2,
        "waste_producer_address_line_3": producer_line_3,
        "waste_producer_postcode": producer_postcode,
        "ewc_code": ewc_value,
        "ewc_manual_override": "1" if ewc_manual_override else "0",
        "ewc_hazardous": "1" if _is_ticket_compliance_hazardous(ticket) else "0",
        "ewc_auto_code_6": _format_ewc_code_display(ewc_auto_code_6)
        if ewc_auto_code_6
        else "",
        "ewc_product_default_code_6": product_default_ewc_code_6,
        "ewc_product_default_display": product_default_ewc_display or "",
        "gross_kg": f"{ticket.gross_kg:.0f}" if ticket.gross_kg is not None else "",
        "tare_kg": f"{ticket.tare_kg:.0f}" if ticket.tare_kg is not None else "",
        "net_kg": f"{ticket.net_kg:.0f}" if ticket.net_kg is not None else "",
        "qty": f"{ticket.qty}" if ticket.qty is not None else "",
        "unit_id": str(ticket.unit_id or ""),
        "unit_price": f"{ticket.unit_price:.2f}" if ticket.unit_price is not None else "",
        "total": f"{ticket.total:.2f}" if ticket.total is not None else "",
        "dont_invoice": "on" if ticket.dont_invoice else "",
    }


def _parse_po_number(raw: str, errors: list[str]) -> str | None:
    po_number = str(raw or "").strip()
    validate_no_html(po_number, "PO number", errors)
    if po_number and len(po_number) > PO_NUMBER_MAX_LENGTH:
        errors.append(f"PO number must be {PO_NUMBER_MAX_LENGTH} characters or fewer.")
    return po_number or None


def _normalize_ticket_search_query(raw: str | None) -> str:
    if raw is None:
        return ""
    collapsed = re.sub(r"\s+", " ", str(raw).strip())
    return collapsed[:TICKET_SEARCH_MAX_LEN]


def _escape_like_term(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _ticket_contains_like_pattern(value: str) -> str:
    return f"%{_escape_like_term(value)}%"


def _form_value(form, key: str) -> str:
    return str(form.get(key, "")).strip()


def _parse_bool_text(value: str | None) -> bool | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    return None


def _form_value_enabled(value: object | None) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized in {"1", "true", "yes", "on"}


def _first_non_empty_form_value(form, key: str) -> str:
    if hasattr(form, "getlist"):
        values = form.getlist(key)
        for value in values:
            text = str(value).strip()
            if text:
                return text
        if values:
            return str(values[-1]).strip()
    return _form_value(form, key)


def _normalize_number(value: str) -> str:
    return str(value).replace(",", "").strip()


def _normalize_ewc_digits(value: str | None) -> str:
    return re.sub(r"\D", "", str(value or ""))[:6]


def _format_ewc_code_display(code_6: str | None) -> str:
    digits = _normalize_ewc_digits(code_6)
    if len(digits) != 6:
        return digits
    return f"{digits[0:2]} {digits[2:4]} {digits[4:6]}"


def _parse_ewc_code_value(raw: str, errors: list[str]) -> str | None:
    normalized = _normalize_ewc_digits(raw)
    if not raw:
        return None
    if len(normalized) != 6:
        errors.append("EWC code must be 6 digits (for example, 01 01 01).")
        return None
    return normalized


def _parse_weight_value(raw: str, label: str, errors: list[str]) -> float | None:
    normalized = _normalize_number(raw)
    if not normalized:
        return None
    try:
        value = Decimal(normalized)
    except (InvalidOperation, ValueError):
        errors.append(f"{label} must be a number.")
        return None
    if value < 0:
        errors.append(f"{label} must be 0 or greater.")
        return None
    if value > WEIGHT_MAX_KG:
        errors.append(f"{label} must be {WEIGHT_MAX_KG:.0f} kg or less.")
        return None
    value = value.quantize(WEIGHT_QUANTIZE, rounding=ROUND_HALF_UP)
    return float(value)


def _apply_ticket_defaults(db: Session, payload: dict) -> None:
    vehicle = db.get(Vehicle, payload["vehicle_id"]) if payload.get("vehicle_id") else None
    warnings = payload.setdefault("warnings", [])

    if (
        payload["customer_id"] is None
        and not payload.get("walk_in")
        and not payload.get("walk_in_sale")
        and vehicle
    ):
        candidate_customer_id = vehicle.default_customer_id or vehicle.owner_customer_id
        if candidate_customer_id:
            customer = db.get(Customer, candidate_customer_id)
            if customer:
                payload["customer_id"] = customer.id
                payload["form"]["customer_id"] = str(customer.id)
                if customer.on_stop:
                    warning = (
                        "Customer is ON STOP - allowed to record ticket; cannot complete/invoice."
                    )
                    if warning not in warnings:
                        warnings.append(warning)

    if (
        payload.get("haulier_id") is None
        and not payload.get("walk_in_sale")
        and vehicle
        and vehicle.default_haulier_id
    ):
        default_haulier = db.get(Haulier, vehicle.default_haulier_id)
        if default_haulier and default_haulier.is_active:
            payload["haulier_id"] = default_haulier.id
            payload["form"]["haulier_id"] = str(default_haulier.id)

    if (
        payload.get("driver_id") is None
        and not payload.get("walk_in_sale")
        and vehicle
        and vehicle.default_driver_id
    ):
        default_driver = db.get(Driver, vehicle.default_driver_id)
        if default_driver and default_driver.is_active:
            payload["driver_id"] = default_driver.id
            payload["form"]["driver_id"] = str(default_driver.id)

    if (
        vehicle
        and (payload.get("tare_kg") is None or payload.get("tare_kg") <= 0)
        and vehicle.default_tare_kg is not None
    ):
        default_tare_kg = float(vehicle.default_tare_kg)
        if default_tare_kg > 0:
            payload["tare_kg"] = default_tare_kg
            payload["form"]["tare_kg"] = f"{default_tare_kg:.0f}"
            gross_kg = payload.get("gross_kg")
            if gross_kg is not None:
                payload["net_kg"] = gross_kg - default_tare_kg
                payload["form"]["net_kg"] = f"{payload['net_kg']:.0f}"

    if payload.get("unit_price_raw") in ("", None) or payload.get("unit_price") is None:
        product_id = payload.get("product_id")
        if product_id:
            product = (
                db.execute(
                    select(Product)
                    .options(joinedload(Product.unit))
                    .where(Product.id == product_id)
                )
                .scalars()
                .first()
            )
            resolved_unit_price, _using_override = resolve_unit_price_for_customer_product(
                db,
                customer_id=payload.get("customer_id"),
                product=product,
            )
            if resolved_unit_price is not None:
                payload["unit_price"] = resolved_unit_price
                logger.info(
                    "Defaulted unit_price from product_id=%s to %s",
                    product_id,
                    resolved_unit_price,
                )

    if payload.get("qty") is not None and payload.get("unit_price") is not None:
        payload["total"] = Decimal(str(payload["qty"])) * payload["unit_price"]


def _product_unit_type(product: Product | None) -> str:
    unit = getattr(product, "unit", None) if product is not None else None
    unit_type = getattr(unit, "unit_type", None) if unit is not None else None
    return str(unit_type or "").upper()


def _coerce_qty_for_weight_product(payload: dict, product: Product | None) -> None:
    if _product_unit_type(product) != "WEIGHT":
        return
    payload["qty"] = None
    payload["total"] = None
    form_data = payload.get("form")
    if isinstance(form_data, dict):
        form_data["qty"] = ""
        form_data["total"] = ""


def _coerce_weights_for_count_product(payload: dict, product: Product | None) -> None:
    if _product_unit_type(product) != "COUNT":
        return
    payload["gross_kg"] = None
    payload["tare_kg"] = None
    payload["net_kg"] = None
    form_data = payload.get("form")
    if isinstance(form_data, dict):
        form_data["gross_kg"] = ""
        form_data["tare_kg"] = ""
        form_data["net_kg"] = ""


def _coerce_mode_fields(payload: dict, product: Product | None) -> None:
    _coerce_qty_for_weight_product(payload, product)
    _coerce_weights_for_count_product(payload, product)


def _apply_destination_default(
    ticket: Ticket, payload: dict, product: Product | None
) -> None:
    if payload.get("destination_id") is not None:
        return
    if payload.get("used_on_site"):
        return
    if ticket.destination_id is not None:
        return
    if not product or not product.default_destination_id:
        return
    payload["destination_id"] = product.default_destination_id
    payload["form"]["destination_id"] = str(product.default_destination_id)


def _apply_ticket_pricing(
    ticket: Ticket, payload: dict, product: Product | None
) -> dict:
    qty = payload.get("qty")
    unit_price = payload.get("unit_price")
    has_qty = qty is not None and qty > 0
    has_weights = (
        payload.get("gross_kg") is not None and payload.get("tare_kg") is not None
    )
    unit = getattr(product, "unit", None) if product else None

    ticket.total = None
    ticket.pricing_basis = None
    pricing_info = {"basis": None, "billable_qty": None, "net_kg": None}
    if product is not None and not _product_matches_transaction_type(
        product, payload.get("transaction_type")
    ):
        return pricing_info

    if has_qty and unit_price is not None:
        ticket.total = Decimal(str(qty)) * unit_price
        ticket.pricing_basis = "QTY"
        pricing_info["basis"] = "QTY"
        pricing_info["billable_qty"] = qty
        pricing_info["net_kg"] = payload.get("net_kg")

    if (
        pricing_info["basis"] != "QTY"
        and has_weights
        and unit_price is not None
        and unit
        and unit.unit_type == "WEIGHT"
    ):
        net_kg = payload["gross_kg"] - payload["tare_kg"]
        ticket.net_kg = net_kg
        unit_name = (unit.name or "").strip().lower()
        billable = None
        if unit_name in ("tonne", "tonnes"):
            billable = Decimal(str(net_kg)) / Decimal("1000")
            billable = billable.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
        elif unit_name == "kg":
            billable = Decimal(str(net_kg)).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        else:
            billable = None
            ticket.total = None
            ticket.pricing_basis = None
        if billable is not None:
            ticket.total = billable * unit_price
            ticket.pricing_basis = "WEIGHT"
            pricing_info["basis"] = "WEIGHT"
            pricing_info["billable_qty"] = billable
            pricing_info["net_kg"] = net_kg
    return pricing_info


def _validate_product_ewc(payload: dict, db: Session) -> Product | None:
    product_id = payload.get("product_id")
    if not product_id:
        return None
    product = (
        db.execute(
            select(Product)
            .options(joinedload(Product.unit), joinedload(Product.ewc_code))
            .where(Product.id == product_id)
        )
        .scalars()
        .first()
    )
    if not product:
        payload["errors"].append("Product not found.")
        return None
    if not _product_has_active_unit(product):
        payload["errors"].append(INACTIVE_PRODUCT_UNIT_ERROR)
        return None
    product_mismatch_error = _product_transaction_mismatch_error(
        product,
        payload.get("transaction_type"),
    )
    if product_mismatch_error:
        payload["errors"].append(product_mismatch_error)
        return None
    return product


def _product_has_active_unit(product: Product | None) -> bool:
    if product is None:
        return False
    unit = product.unit
    return bool(unit and unit.is_active)


def _product_default_ewc(product: Product | None) -> EwcCode | None:
    if not product or not product.ewc_code_id:
        return None
    return product.ewc_code


def _sync_form_product_ewc_defaults(payload: dict, product: Product | None) -> None:
    default_ewc = _product_default_ewc(product)
    default_code_6 = _normalize_ewc_digits(default_ewc.code_6 if default_ewc else "")
    default_display = (
        default_ewc.code_display
        if default_ewc and default_ewc.code_display
        else _format_ewc_code_display(default_code_6)
    )
    payload["ewc_product_default_code_6"] = default_code_6
    payload["ewc_product_default_display"] = default_display or ""
    form_data = payload.get("form")
    if isinstance(form_data, dict):
        form_data["ewc_product_default_code_6"] = default_code_6
        form_data["ewc_product_default_display"] = default_display or ""


def _build_ewc_snapshot(db: Session, code_6: str | None) -> dict[str, object]:
    normalized = _normalize_ewc_digits(code_6)
    if not normalized:
        return {
            "ewc_code_6": None,
            "ewc_code_display": None,
            "ewc_description": None,
            "ewc_hazardous": None,
        }

    ewc = (
        db.execute(select(EwcCode).where(EwcCode.code_6 == normalized))
        .scalars()
        .first()
    )
    if ewc:
        return {
            "ewc_code_6": ewc.code_6,
            "ewc_code_display": ewc.code_display,
            "ewc_description": ewc.description,
            "ewc_hazardous": ewc.hazardous,
        }

    return {
        "ewc_code_6": normalized,
        "ewc_code_display": _format_ewc_code_display(normalized),
        "ewc_description": None,
        "ewc_hazardous": None,
    }


def _resolve_ticket_ewc_snapshot(
    payload: dict, db: Session, product: Product | None
) -> dict[str, object]:
    _sync_form_product_ewc_defaults(payload, product)

    is_waste_tx = _is_waste_transaction(payload.get("transaction_type"))
    entered_code_6 = _normalize_ewc_digits(payload.get("ewc_code_6"))
    auto_code_6 = _normalize_ewc_digits(payload.get("ewc_auto_code_6"))
    manual_override = bool(payload.get("ewc_manual_override"))
    default_ewc = _product_default_ewc(product)
    default_code_6 = _normalize_ewc_digits(default_ewc.code_6 if default_ewc else "")

    resolved_code_6 = entered_code_6
    if is_waste_tx and default_code_6:
        should_fill_blank = not entered_code_6
        should_update_previous_auto = (
            not manual_override
            and bool(auto_code_6)
            and entered_code_6 == auto_code_6
            and auto_code_6 != default_code_6
        )
        should_sync_current_default = (
            not manual_override and entered_code_6 == default_code_6
        )
        if should_fill_blank or should_update_previous_auto or should_sync_current_default:
            resolved_code_6 = default_code_6
            manual_override = False

    snapshot = _build_ewc_snapshot(db, resolved_code_6)
    resolved_code_6 = _normalize_ewc_digits(snapshot.get("ewc_code_6"))

    if not resolved_code_6:
        manual_override = False
    elif default_code_6 and resolved_code_6 != default_code_6:
        manual_override = True
    elif not default_code_6:
        manual_override = True
    else:
        manual_override = False

    snapshot["ewc_manual_override"] = manual_override

    payload["ewc_code_6"] = resolved_code_6 or None
    payload["ewc_manual_override"] = manual_override
    payload["ewc_auto_code_6"] = resolved_code_6 if (resolved_code_6 and not manual_override) else ""
    payload["ewc_hazardous"] = bool(snapshot.get("ewc_hazardous"))

    form_data = payload.get("form")
    if isinstance(form_data, dict):
        form_data["ewc_code"] = str(snapshot.get("ewc_code_display") or "")
        form_data["ewc_manual_override"] = "1" if manual_override else "0"
        form_data["ewc_hazardous"] = "1" if bool(snapshot.get("ewc_hazardous")) else "0"
        form_data["ewc_auto_code_6"] = _format_ewc_code_display(
            payload.get("ewc_auto_code_6")
        )

    return snapshot


def _validate_waste_ewc_on_complete(payload: dict, ewc_snapshot: dict[str, object]) -> None:
    if not _is_waste_transaction(payload.get("transaction_type")):
        return
    if not _normalize_ewc_digits(ewc_snapshot.get("ewc_code_6")):
        payload["errors"].append("EWC code is required to complete a waste ticket.")


def _validate_waste_producer_on_complete(payload: dict) -> None:
    if not _is_waste_transaction(payload.get("transaction_type")):
        return
    producer_name = _normalize_optional_text(payload.get("waste_producer_name_snapshot"))
    if not producer_name:
        payload["errors"].append(
            "Waste producer name is required to complete a waste ticket."
        )


def _validate_required_on_complete(payload: dict) -> None:
    transaction_type = payload.get("transaction_type")
    is_waste_tx = _is_waste_transaction(transaction_type)
    is_sale = transaction_type == TransactionTypeEnum.SALE.value
    used_on_site = bool(payload.get("used_on_site"))
    final_disposal = bool(payload.get("final_disposal"))
    destination_required_for_movement = final_disposal and not used_on_site

    if not payload.get("direction"):
        payload["errors"].append("Direction is required.")
    if not payload.get("transaction_type"):
        payload["errors"].append("Transaction type is required.")

    if is_waste_tx:
        if not payload.get("customer_id"):
            payload["errors"].append(
                "Customer is required to complete a waste ticket."
            )
        if not payload.get("product_id"):
            payload["errors"].append("Product is required to complete a ticket.")
        if not used_on_site and not payload.get("destination_id"):
            payload["errors"].append(
                "Destination is required to complete a waste ticket."
            )
    elif is_sale:
        if not payload.get("customer_id") and not payload.get("walk_in_sale"):
            payload["errors"].append(
                "Customer is required to complete a sale ticket."
            )
        if not payload.get("product_id"):
            payload["errors"].append("Product is required to complete a ticket.")

    if (
        destination_required_for_movement
        and not payload.get("destination_id")
        and not is_waste_tx
    ):
        payload["errors"].append(FINAL_DISPOSAL_DESTINATION_ERROR)


def _is_waste_transaction(transaction_type: str | None) -> bool:
    return transaction_type in (
        TransactionTypeEnum.WASTEIN.value,
        TransactionTypeEnum.WASTEOUT.value,
    )


def _normalize_product_type(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in (PRODUCT_TYPE_SALE, PRODUCT_TYPE_WASTE):
        return normalized
    return ""


def _required_product_type_for_transaction(
    transaction_type: str | None,
) -> str | None:
    normalized = _enum_value_or_text(transaction_type).upper()
    if normalized == TransactionTypeEnum.SALE.value:
        return PRODUCT_TYPE_SALE
    if _is_waste_transaction(normalized):
        return PRODUCT_TYPE_WASTE
    return None


def _product_filter_for_transaction(transaction_type: str | None):
    required_type = _required_product_type_for_transaction(transaction_type)
    if required_type == PRODUCT_TYPE_SALE:
        return or_(
            func.lower(Product.product_type) == PRODUCT_TYPE_SALE,
            Product.product_type.is_(None),
        )
    if required_type == PRODUCT_TYPE_WASTE:
        return or_(
            func.lower(Product.product_type) == PRODUCT_TYPE_WASTE,
            and_(Product.product_type.is_(None), Product.sales_only.is_(False)),
        )
    return None


def _product_matches_transaction_type(
    product: Product | None,
    transaction_type: str | None,
) -> bool:
    if product is None:
        return False
    required_type = _required_product_type_for_transaction(transaction_type)
    if required_type is None:
        return True
    product_type = _normalize_product_type(getattr(product, "product_type", None))
    if product_type:
        return product_type == required_type
    if required_type == PRODUCT_TYPE_SALE:
        # Legacy rows without product_type should remain usable on sale tickets.
        return True
    return not bool(getattr(product, "sales_only", False))


def _product_transaction_mismatch_error(
    product: Product | None,
    transaction_type: str | None,
    *,
    warning: bool = False,
) -> str | None:
    if product is None:
        return None
    required_type = _required_product_type_for_transaction(transaction_type)
    if required_type is None:
        return None
    product_type = _normalize_product_type(getattr(product, "product_type", None))
    if product_type == required_type:
        return None
    if required_type == PRODUCT_TYPE_WASTE:
        if product_type == PRODUCT_TYPE_SALE or (
            not product_type and bool(getattr(product, "sales_only", False))
        ):
            return SALES_ONLY_WASTE_WARNING if warning else SALES_ONLY_WASTE_ERROR
        return None
    if required_type == PRODUCT_TYPE_SALE and product_type == PRODUCT_TYPE_WASTE:
        return WASTE_ONLY_SALE_WARNING if warning else WASTE_ONLY_SALE_ERROR
    return None


def _product_option_empty_label(transaction_type: str | None) -> str:
    required_type = _required_product_type_for_transaction(transaction_type)
    if required_type == PRODUCT_TYPE_SALE:
        return "No sale products available."
    if required_type == PRODUCT_TYPE_WASTE:
        return "No waste products available."
    return "No products available."


def _is_product_final_disposal_default(product: Product | None) -> bool:
    if product is None:
        return False
    return bool(getattr(product, "final_disposal", False)) or bool(
        getattr(product, "final_disposal_wip", False)
    )


def _is_product_used_on_site_default(product: Product | None) -> bool:
    if product is None:
        return False
    return bool(getattr(product, "used_on_site", False)) or bool(
        getattr(product, "used_on_site_wip", False)
    )


def _ticket_operation_context(*, final_disposal: bool, used_on_site: bool) -> dict[str, bool]:
    return {
        "final_disposal": bool(final_disposal),
        "used_on_site": bool(used_on_site),
        "destination_required": bool(final_disposal) and not bool(used_on_site),
    }


def _ticket_operation_form_values(*, final_disposal: bool, used_on_site: bool) -> dict[str, str]:
    return {
        "final_disposal": "on" if final_disposal else "",
        "used_on_site": "on" if used_on_site else "",
    }


def _ticket_operation_context_from_form(form: dict[str, object] | None) -> dict[str, bool]:
    if form is None:
        return _ticket_operation_context(final_disposal=False, used_on_site=False)
    return _ticket_operation_context(
        final_disposal=_form_value_enabled(form.get("final_disposal")),
        used_on_site=_form_value_enabled(form.get("used_on_site")),
    )


def _resolve_ticket_operation_context(
    *,
    db: Session,
    ticket_id: int | None,
    requested_final_disposal: bool | None,
    requested_used_on_site: bool | None,
) -> dict[str, bool]:
    final_disposal = requested_final_disposal
    used_on_site = requested_used_on_site
    if (final_disposal is None or used_on_site is None) and ticket_id:
        ticket = db.get(Ticket, ticket_id)
        if ticket is not None:
            if final_disposal is None:
                final_disposal = bool(ticket.final_disposal)
            if used_on_site is None:
                used_on_site = bool(ticket.used_on_site)
    return _ticket_operation_context(
        final_disposal=bool(final_disposal),
        used_on_site=bool(used_on_site),
    )


def _resolve_ticket_operation_flag_values(
    *,
    product: Product | None,
    ticket: Ticket | None,
    apply_product_defaults: bool,
    requested_final_disposal: bool | None,
    requested_used_on_site: bool | None,
) -> tuple[bool, bool]:
    default_final_disposal = _is_product_final_disposal_default(product)
    default_used_on_site = _is_product_used_on_site_default(product)
    if apply_product_defaults:
        return default_final_disposal, default_used_on_site

    final_disposal = requested_final_disposal
    used_on_site = requested_used_on_site
    if ticket is not None:
        if final_disposal is None:
            final_disposal = bool(ticket.final_disposal)
        if used_on_site is None:
            used_on_site = bool(ticket.used_on_site)

    if final_disposal is None:
        final_disposal = default_final_disposal
    if used_on_site is None:
        used_on_site = default_used_on_site
    return bool(final_disposal), bool(used_on_site)


def _apply_ticket_ewc_snapshot(ticket: Ticket, snapshot: dict[str, object]) -> None:
    ticket.ewc_code_6 = snapshot.get("ewc_code_6")
    ticket.ewc_code_display = snapshot.get("ewc_code_display")
    ticket.ewc_description = snapshot.get("ewc_description")
    ticket.ewc_hazardous = snapshot.get("ewc_hazardous")
    ticket.ewc_manual_override = bool(snapshot.get("ewc_manual_override"))


def _validate_carrier_licence(
    payload: dict,
    db: Session,
    *,
    errors: list[str] | None = None,
) -> Haulier | None:
    haulier_id = payload.get("haulier_id")
    if not haulier_id:
        return None
    haulier = db.get(Haulier, haulier_id)
    if (
        errors is not None
        and haulier
        and haulier.carrier_licence_number
        and len(haulier.carrier_licence_number) > CODE_MAX
    ):
        errors.append(
            f"Carrier licence number must be {CODE_MAX} characters or fewer."
        )
    return haulier


def _apply_carrier_licence_snapshot(
    ticket: Ticket, haulier: Haulier | None
) -> None:
    ticket.carrier_licence_number = (
        haulier.carrier_licence_number if haulier else None
    )


def _format_customer_address(customer: Customer) -> str | None:
    parts = [
        customer.address_line1,
        customer.address_line2,
        customer.city,
        customer.postcode,
        customer.country,
    ]
    cleaned = [part.strip() for part in parts if part and part.strip()]
    if not cleaned:
        return None
    return ", ".join(cleaned)


def _normalize_optional_text(value: str | None) -> str | None:
    normalized = (value or "").strip()
    return normalized or None


def _compose_manual_waste_producer_address(payload: dict) -> str | None:
    parts = [
        payload.get("waste_producer_address_line_1"),
        payload.get("waste_producer_address_line_2"),
        payload.get("waste_producer_address_line_3"),
        payload.get("waste_producer_postcode"),
    ]
    cleaned = [part.strip() for part in parts if part and part.strip()]
    if not cleaned:
        return None
    return ", ".join(cleaned)


def _split_waste_producer_address(address: str | None) -> tuple[str, str, str, str]:
    if not address:
        return ("", "", "", "")
    normalized = address.replace("\r", "\n")
    if "\n" in normalized:
        parts = [part.strip() for part in normalized.split("\n") if part.strip()]
    else:
        parts = [part.strip() for part in normalized.split(",") if part.strip()]
    padded_parts = parts[:4] + [""] * (4 - min(len(parts), 4))
    line_1, line_2, line_3, postcode = padded_parts
    return (line_1, line_2, line_3, postcode)


def _resolve_waste_producer_snapshot(
    payload: dict,
    db: Session,
    *,
    require_customer_when_same: bool = False,
    require_manual_name: bool = False,
) -> None:
    same_as_customer = bool(payload.get("waste_producer_same_as_customer"))
    payload["waste_producer_source"] = (
        WasteProducerSourceEnum.CUSTOMER.value
        if same_as_customer
        else WasteProducerSourceEnum.MANUAL.value
    )

    if same_as_customer:
        customer_id = payload.get("customer_id")
        if not customer_id:
            if require_customer_when_same:
                payload["errors"].append(
                    "Waste producer is set to same as customer - select a customer."
                )
            payload["waste_producer_name_snapshot"] = None
            payload["waste_producer_address_snapshot"] = None
            return
        customer = db.get(Customer, customer_id)
        if not customer:
            payload["errors"].append("Waste producer customer not found.")
            payload["waste_producer_name_snapshot"] = None
            payload["waste_producer_address_snapshot"] = None
            return
        payload["waste_producer_name_snapshot"] = _normalize_optional_text(customer.name)
        payload["waste_producer_address_snapshot"] = _format_customer_address(customer)
        return

    producer_name = _normalize_optional_text(payload.get("waste_producer_name"))
    if require_manual_name and not producer_name:
        payload["errors"].append(
            "Enter waste producer name or tick 'Waste producer same as customer'."
        )
    payload["waste_producer_name_snapshot"] = producer_name
    payload["waste_producer_address_snapshot"] = _compose_manual_waste_producer_address(
        payload
    )


def _apply_waste_producer_snapshot(ticket: Ticket, payload: dict) -> None:
    ticket.waste_producer_name = payload.get("waste_producer_name_snapshot")
    ticket.waste_producer_address = payload.get("waste_producer_address_snapshot")

def _net_negative(ticket: Ticket) -> bool:
    if ticket.gross_kg is None or ticket.tare_kg is None:
        return False
    try:
        return Decimal(str(ticket.gross_kg)) - Decimal(str(ticket.tare_kg)) < 0
    except (InvalidOperation, ValueError):
        return False


def _net_negative_values(gross_kg: float | None, tare_kg: float | None) -> bool:
    if gross_kg is None or tare_kg is None:
        return False
    try:
        return Decimal(str(gross_kg)) - Decimal(str(tare_kg)) < 0
    except (InvalidOperation, ValueError):
        return False


def _render_vehicle_suggestions(
    request: Request,
    ticket: Ticket,
    vehicle: Vehicle | None,
    default_customer: Customer | None,
    default_haulier: Haulier | None,
    default_driver: Driver | None = None,
    *,
    suggested_tare_kg: float | None = None,
    ticket_vehicle_reg: str | None = None,
    current_customer_id: int | None = None,
    current_haulier_id: int | None = None,
    current_driver_id: int | None = None,
    oob_customer_options: list[tuple[str, str]] | None = None,
    oob_haulier_options: list[tuple[str, str]] | None = None,
    oob_driver_options: list[tuple[str, str]] | None = None,
    oob_weights_form: dict | None = None,
    oob_tare_auto_hint: str | None = None,
    oob_status_warnings: list[str] | None = None,
    show_panel: bool = True,
    warnings: list[str] | None = None,
    errors: list[str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    def _to_decimal(value: object) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None

    has_vehicle_match = (
        vehicle is not None
        and (ticket.vehicle_id is None or ticket.vehicle_id == vehicle.id)
    )

    selected_customer_id = (
        ticket.customer_id if current_customer_id is None else current_customer_id
    )
    selected_haulier_id = (
        ticket.haulier_id if current_haulier_id is None else current_haulier_id
    )
    selected_driver_id = (
        ticket.driver_id if current_driver_id is None else current_driver_id
    )
    can_apply_customer = (
        has_vehicle_match
        and default_customer is not None
        and selected_customer_id is None
    )
    can_apply_haulier = (
        has_vehicle_match and default_haulier is not None and ticket.haulier_id is None
    )
    can_apply_driver = (
        has_vehicle_match
        and default_driver is not None
        and default_driver.is_active
        and selected_driver_id is None
    )
    suggested_tare_value = _to_decimal(suggested_tare_kg)
    current_tare_value = _to_decimal(ticket.tare_kg)
    can_apply_tare = (
        has_vehicle_match
        and suggested_tare_value is not None
        and suggested_tare_value > 0
        and (current_tare_value is None or current_tare_value != suggested_tare_value)
    )
    can_apply_vehicle = vehicle is not None and ticket.vehicle_id != vehicle.id
    can_apply_suggestion = (
        vehicle is not None
        and (can_apply_vehicle or can_apply_customer or can_apply_haulier or can_apply_driver or can_apply_tare)
    )
    should_show_panel = show_panel and (can_apply_suggestion or bool(errors))
    return templates.TemplateResponse(request, 
        "tickets/_vehicle_suggestions.html",
        {
            "request": request,
            "ticket": ticket,
            "vehicle": vehicle,
            "default_customer": default_customer,
            "default_haulier": default_haulier,
            "default_driver": default_driver,
            "suggested_tare_kg": suggested_tare_kg,
            "show_panel": should_show_panel,
            "vehicle_mismatch": (
                vehicle is not None
                and ticket.vehicle_id is not None
                and ticket.vehicle_id != vehicle.id
            ),
            "ticket_vehicle_reg": ticket_vehicle_reg or "",
            "oob_customer_options": oob_customer_options or [],
            "oob_haulier_options": oob_haulier_options or [],
            "oob_driver_options": oob_driver_options or [],
            "oob_customer_selected": str(selected_customer_id or ""),
            "oob_haulier_selected": str(selected_haulier_id or ""),
            "oob_driver_selected": str(selected_driver_id or ""),
            "can_apply_suggestion": can_apply_suggestion,
            "oob_weights_form": oob_weights_form,
            "oob_tare_auto_hint": oob_tare_auto_hint,
            "oob_status_warnings": oob_status_warnings,
            "warnings": warnings or [],
            "errors": errors or [],
            "is_open": _is_open_ticket(ticket),
        },
        status_code=status_code,
    )


def _render_ticket_edit(
    request: Request,
    ticket: Ticket,
    db: Session,
    *,
    errors: list[str],
    warnings: list[str] | None = None,
    form: dict | None = None,
    vehicle_reg: str | None = None,
    weight_warning: bool | None = None,
    ticket_email_form: dict[str, str] | None = None,
    ticket_email_form_open: bool = False,
    ticket_email_failed: bool = False,
    ticket_email_error: str = "",
    status_code: int = 400,
) -> HTMLResponse:
    _ensure_ticket_void_reasons(db)
    invoice = db.get(Invoice, ticket.invoice_id) if ticket.invoice_id else None
    ticket_void, ticket_void_reason = _latest_ticket_void_with_reason(db, ticket.id)
    resolved_form = form or _ticket_to_form(ticket)
    selected_customer_id = None
    if resolved_form.get("customer_id"):
        selected_customer_id = _parse_int(str(resolved_form.get("customer_id")))
    if selected_customer_id is None:
        selected_customer_id = ticket.customer_id
    selected_po_number = str(resolved_form.get("po_number", "")).strip()
    selected_haulier_id = None
    if resolved_form.get("haulier_id"):
        selected_haulier_id = _parse_int(str(resolved_form.get("haulier_id")))
    if selected_haulier_id is None:
        selected_haulier_id = ticket.haulier_id
    stop_blockers = _on_stop_blockers(db, selected_customer_id, selected_haulier_id)
    product_id = None
    if resolved_form.get("product_id"):
        product_id = _parse_int(str(resolved_form.get("product_id")))
    if product_id is None:
        product_id = ticket.product_id
    selected_transaction_type = None
    if resolved_form.get("transaction_type"):
        selected_transaction_type = str(resolved_form.get("transaction_type"))
    if selected_transaction_type is None:
        selected_transaction_type = _enum_value_or_text(ticket.transaction_type)
    default_destination_id = None
    unit_type = None
    if product_id:
        product = db.get(Product, product_id)
        if product:
            default_destination_id = product.default_destination_id
            unit_type = product.unit.unit_type if product.unit else None
    options = _load_ticket_options_with_enums(
        db,
        transaction_type=selected_transaction_type,
        selected_product_id=product_id,
        direction=resolved_form.get("direction"),
        selected_transaction_type=selected_transaction_type,
    )
    resolved_warnings = list(warnings or [])
    product_warning = _sales_only_selected_product_warning(
        db, selected_transaction_type, product_id
    )
    do_not_invoice_warning, po_required_warning = _customer_invoice_warning_flags(
        db,
        selected_customer_id,
        selected_po_number,
        ticket_status=ticket.status,
    )
    (
        price_override_active,
        price_override_unit_price,
    ) = _ticket_price_override_context(
        db,
        customer_id=selected_customer_id,
        product_id=product_id,
    )
    credit_limit_banner_context = _ticket_credit_limit_banner_context(
        db,
        customer_id=selected_customer_id,
        ticket=ticket,
        form=resolved_form,
    )
    print_actions_context = _ticket_print_actions_context(
        db,
        ticket=ticket,
    )
    wtn_actions_context = _ticket_wtn_actions_context(
        db,
        ticket=ticket,
    )
    resolved_ticket_email_form = ticket_email_form or _ticket_email_form_values(db, ticket)
    return templates.TemplateResponse(request, 
        "tickets/edit.html",
        {
            "request": request,
            "errors": errors,
            "warnings": resolved_warnings,
            "product_usage_warning": product_warning,
            "ticket_operation": _ticket_operation_context_from_form(resolved_form),
            "ticket_operation_form": _ticket_operation_form_values(
                final_disposal=_form_value_enabled(resolved_form.get("final_disposal")),
                used_on_site=_form_value_enabled(resolved_form.get("used_on_site")),
            ),
            "saved": False,
            "completed": False,
            "printed": False,
            "print_failed": False,
            "print_error_label": "",
            "print_error_detail": "",
            "printed_to": "",
            "print_status": False,
            "print_sent_at": "",
            "print_job_id": "",
            "wtn_sent": False,
            "wtn_failed": False,
            "wtn_error_detail": "",
            "wtn_job_id": "",
            "wtn_signature_saved": False,
            "wtn_receiver_signature_applied": False,
            "email_sent": False,
            "ticket_email_failed": ticket_email_failed,
            "ticket_email_error": ticket_email_error,
            "ticket": ticket,
            "ticket_void": ticket_void,
            "ticket_void_reason": ticket_void_reason,
            "invoice": invoice,
            "is_admin": False,
            "allow_dev_preview": (
                _ticket_dev_preview_enabled()
                and _status_value(ticket.status) != TicketStatusEnum.COMPLETE.value
            ),
            "is_open": _is_open_ticket(ticket),
            "is_locked": _is_ticket_locked(ticket),
            "can_void": _can_void_ticket(ticket),
            "void_blocked_message": _ticket_void_blocked_message(ticket)
            if _status_value(ticket.status) != TicketStatusEnum.VOID.value
            and not _can_void_ticket(ticket)
            else "",
            "locked_message": _ticket_locked_message(ticket)
            if _is_ticket_locked(ticket)
            else "",
            "stop_blocked": bool(stop_blockers),
            "stop_banner_message": _on_stop_banner_message(stop_blockers),
            "customer_do_not_invoice_warning": do_not_invoice_warning,
            "customer_po_required_warning": po_required_warning,
            "po_required_banner_text": PO_REQUIRED_INVOICE_BANNER,
            "po_number_max_length": PO_NUMBER_MAX_LENGTH,
            "weight_warning": _net_negative(ticket)
            if weight_warning is None
            else weight_warning,
            "form": resolved_form,
            "vehicle_reg": vehicle_reg if vehicle_reg is not None else _ticket_vehicle_reg(db, ticket),
            "can_edit_complete_po": _can_edit_complete_po(ticket, db),
            "po_locked_invoiced": _po_locked_invoiced(ticket, db),
            "compliance_locked_invoiced": _compliance_locked_invoiced(ticket, db),
            "compliance_locked_invoiced_message": COMPLIANCE_LOCKED_INVOICED_MESSAGE,
            "ticket_compliance_hazardous": _is_ticket_compliance_hazardous(ticket),
            "price_override_active": price_override_active,
            "price_override_unit_price": price_override_unit_price,
            "default_destination_id": default_destination_id,
            "unit_type": unit_type,
            "options": options,
            "product_unit_meta": _load_product_unit_meta(db),
            "enums": _ticket_enums(),
            "receipts_wip_enabled": bool(settings.receipts_wip_enabled),
            "ticket_email_form": resolved_ticket_email_form,
            "ticket_email_form_open": ticket_email_form_open,
            **credit_limit_banner_context,
            **print_actions_context,
            **wtn_actions_context,
            **_active_lookup_options(ticket, db),
        },
        status_code=status_code,
    )


def _ticket_price_override_context(
    db: Session,
    *,
    customer_id: int | None,
    product_id: int | None,
) -> tuple[bool, Decimal | None]:
    if not customer_id or not product_id:
        return False, None
    product = (
        db.execute(
            select(Product)
            .options(joinedload(Product.unit))
            .where(Product.id == product_id)
        )
        .scalars()
        .first()
    )
    if product is None:
        return False, None
    resolved_unit_price, using_override = resolve_unit_price_for_customer_product(
        db,
        customer_id=customer_id,
        product=product,
    )
    if not using_override:
        return False, None
    return True, resolved_unit_price


def _render_weights_partial(
    request: Request, ticket: Ticket, errors: list[str], status_code: int = 200
) -> HTMLResponse:
    return templates.TemplateResponse(request, 
        "tickets/_weights_block.html",
        {
            "request": request,
            "ticket": ticket,
            "errors": errors,
            "is_admin": False,
            "is_open": _is_open_ticket(ticket),
            "form": _ticket_to_form(ticket),
            "show_weight_errors": True,
        },
        status_code=status_code,
    )


def _weights_form_from_values(
    ticket: Ticket,
    gross_raw: str,
    tare_raw: str,
    net_value: float | None,
) -> dict:
    form_data = _ticket_to_form(ticket)
    form_data["gross_kg"] = gross_raw
    form_data["tare_kg"] = tare_raw
    form_data["net_kg"] = f"{net_value:.0f}" if net_value is not None else ""
    return form_data


def _ticket_enums() -> dict[str, list[str]]:
    return {
        "directions": [value.value for value in DirectionEnum],
        "transaction_types": [value.value for value in TransactionTypeEnum],
        "statuses": [value.value for value in TicketStatusEnum],
    }


def _parse_int(value: str) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _parse_float(value: str) -> float | None:
    normalized = _normalize_number(value)
    if not normalized:
        return None
    try:
        return float(normalized)
    except ValueError:
        return None


def _parse_decimal(value: str) -> Decimal | None:
    normalized = _normalize_number(value)
    if not normalized:
        return None
    try:
        return Decimal(str(normalized))
    except (InvalidOperation, ValueError):
        return None

