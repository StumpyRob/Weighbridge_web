from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import Session

from ..models import Customer, Invoice, Ticket, TicketStatusEnum, TransactionTypeEnum, Vehicle
from ..models.base import utcnow
from .credit import (
    INVOICE_OUTSTANDING_EXCLUDED_STATUSES,
    INVOICE_OUTSTANDING_ISSUED_STATUSES,
)

AI_ASSISTANT_SAMPLE_LIMIT = 5
_WASTE_TRANSACTION_TYPES = (
    TransactionTypeEnum.WASTEIN.value,
    TransactionTypeEnum.WASTEOUT.value,
)
_OPEN_TICKET_HINTS = ("open ticket", "open tickets", "still open", "awaiting completion")
_OPEN_WASTE_HINTS = ("open waste", "waste ticket", "waste tickets", "open waste tickets")
_UNINVOICED_HINTS = ("uninvoic", "not invoiced", "ready to invoice", "invoice ready")
_TODAY_WEIGHT_HINTS = ("today", "weight", "throughput", "kg", "tonne", "tonnes")
_UNPAID_INVOICE_HINTS = ("unpaid", "outstanding invoice", "outstanding invoices", "overdue invoice", "overdue invoices")
_OVERDUE_INVOICE_HINTS = (
    "overdue invoice",
    "overdue invoices",
    "invoice overdue",
    "invoices overdue",
    "invoices are overdue",
    "past due invoice",
    "past due invoices",
)
_RECENT_ACTIVITY_HINTS = ("recent", "latest", "activity", "last ticket", "last tickets")
_TOP_CUSTOMER_HINTS = ("top customer", "busiest customer", "top customer today", "busiest customer today")
_QUESTION_CONTEXT_TOPICS = (
    ("open_tickets", _OPEN_TICKET_HINTS),
    ("open_waste_tickets", _OPEN_WASTE_HINTS),
    ("uninvoiced_tickets", _UNINVOICED_HINTS),
    ("today_weight_total", _TODAY_WEIGHT_HINTS),
    ("unpaid_invoices", _UNPAID_INVOICE_HINTS),
    ("overdue_invoices", _OVERDUE_INVOICE_HINTS),
    ("recent_tickets", _RECENT_ACTIVITY_HINTS),
    ("top_customer_today", _TOP_CUSTOMER_HINTS),
)


def _format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.strftime("%d %b %Y %H:%M")


def _format_date(value: date | None) -> str | None:
    if value is None:
        return None
    return value.strftime("%d %b %Y")


def _decimal_to_plain_string(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _format_weight_kg(value: object) -> str:
    amount = Decimal(str(value or 0))
    return f"{_decimal_to_plain_string(amount.quantize(Decimal('0.001')))} kg"


def _format_weight_tonnes(value: object) -> str:
    amount = Decimal(str(value or 0))
    tonnes = (amount / Decimal("1000")).quantize(Decimal("0.001"))
    return f"{_decimal_to_plain_string(tonnes)} tonnes"


def _format_money(value: object) -> str:
    amount = Decimal(str(value or 0)).quantize(Decimal("0.01"))
    return f"{amount}"


def _vehicle_label(registration: str | None, manual_registration: str | None) -> str | None:
    candidate = str(registration or "").strip() or str(manual_registration or "").strip()
    return candidate or None


def _ticket_kind_label(transaction_type: str | None) -> str:
    normalized = str(transaction_type or "").strip().upper()
    if normalized in _WASTE_TRANSACTION_TYPES:
        return "Waste"
    return "Sale"


def _day_bounds(target_date: date) -> tuple[datetime, datetime]:
    day_start = datetime.combine(target_date, time.min)
    return day_start, day_start + timedelta(days=1)


def _completed_ticket_totals_between(
    db: Session,
    tenant_id: int,
    *,
    date_from: datetime,
    date_to: datetime,
) -> tuple[Decimal, int]:
    status_complete = TicketStatusEnum.COMPLETE.value
    total_kg = Decimal(
        str(
            db.execute(
                select(func.coalesce(func.sum(Ticket.net_kg), 0)).where(
                    Ticket.tenant_id == int(tenant_id),
                    Ticket.status == status_complete,
                    Ticket.datetime >= date_from,
                    Ticket.datetime < date_to,
                )
            ).scalar_one()
            or 0
        )
    )
    completed_count = int(
        db.execute(
            select(func.count(Ticket.id)).where(
                Ticket.tenant_id == int(tenant_id),
                Ticket.status == status_complete,
                Ticket.datetime >= date_from,
                Ticket.datetime < date_to,
            )
        ).scalar_one()
        or 0
    )
    return total_kg, completed_count


def _ticket_summary_query(*, include_status: bool = False, include_net: bool = False):
    columns = [
        Ticket.id.label("ticket_id"),
        Ticket.ticket_no.label("ticket_no"),
        Ticket.datetime.label("ticket_datetime"),
        Customer.id.label("customer_id"),
        Customer.name.label("customer_name"),
        Vehicle.id.label("vehicle_id"),
        Vehicle.registration.label("vehicle_registration"),
        Ticket.vehicle_reg_text.label("vehicle_reg_text"),
        Ticket.transaction_type.label("transaction_type"),
    ]
    if include_status:
        columns.append(Ticket.status.label("status"))
    if include_net:
        columns.append(Ticket.net_kg.label("net_kg"))
    return (
        select(*columns)
        .outerjoin(Customer, Ticket.customer_id == Customer.id)
        .outerjoin(Vehicle, Ticket.vehicle_id == Vehicle.id)
    )


def _serialize_ticket_summary_rows(
    rows,
    *,
    include_status: bool = False,
    include_net: bool = False,
) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for row in rows:
        item: dict[str, object] = {
            "ticket_id": int(row.ticket_id),
            "ticket_no": row.ticket_no,
            "datetime": _format_datetime(row.ticket_datetime),
            "customer_id": int(row.customer_id) if row.customer_id is not None else None,
            "customer": str(row.customer_name or "").strip() or None,
            "vehicle_id": int(row.vehicle_id) if row.vehicle_id is not None else None,
            "vehicle": _vehicle_label(row.vehicle_registration, row.vehicle_reg_text),
            "kind": _ticket_kind_label(row.transaction_type),
        }
        if include_status:
            item["status"] = str(row.status or "").strip() or None
        if include_net:
            item["net_kg"] = _format_weight_kg(row.net_kg)
        items.append(item)
    return items


def _outstanding_invoice_filters(tenant_id: int):
    status_upper = func.upper(func.coalesce(Invoice.status, ""))
    return status_upper, (
        Invoice.tenant_id == int(tenant_id),
        status_upper != "",
        or_(
            status_upper.in_(INVOICE_OUTSTANDING_ISSUED_STATUSES),
            ~status_upper.in_(INVOICE_OUTSTANDING_EXCLUDED_STATUSES),
        ),
    )


def _invoice_summary_query():
    return (
        select(
            Invoice.id.label("invoice_id"),
            Invoice.invoice_no.label("invoice_no"),
            Invoice.invoice_date.label("invoice_date"),
            Invoice.due_date.label("due_date"),
            Invoice.status.label("status"),
            Invoice.gross_total.label("gross_total"),
            Customer.id.label("customer_id"),
            Customer.name.label("customer_name"),
        )
        .join(Customer, Invoice.customer_id == Customer.id)
    )


def _serialize_invoice_summary_rows(rows) -> list[dict[str, object]]:
    return [
        {
            "invoice_id": int(row.invoice_id),
            "invoice_no": row.invoice_no,
            "invoice_date": _format_date(row.invoice_date),
            "due_date": _format_date(row.due_date),
            "status": str(row.status or "").strip() or None,
            "customer_id": int(row.customer_id) if row.customer_id is not None else None,
            "customer": str(row.customer_name or "").strip() or None,
            "gross_total": _format_money(row.gross_total),
        }
        for row in rows
    ]


def get_day_weight_total(db: Session, tenant_id: int, *, target_date: date) -> dict[str, object]:
    day_start, day_end = _day_bounds(target_date)
    total_kg, completed_count = _completed_ticket_totals_between(
        db,
        tenant_id,
        date_from=day_start,
        date_to=day_end,
    )
    return {
        "date": target_date.isoformat(),
        "completed_ticket_count": completed_count,
        "total_kg": _format_weight_kg(total_kg),
        "total_tonnes": _format_weight_tonnes(total_kg),
        "total_kg_raw": _decimal_to_plain_string(total_kg.quantize(Decimal("0.001"))),
    }


def get_today_weight_total(db: Session, tenant_id: int, *, today: date | None = None) -> dict[str, object]:
    return get_day_weight_total(db, tenant_id, target_date=today or utcnow().date())


def get_open_tickets(db: Session, tenant_id: int, *, limit: int = AI_ASSISTANT_SAMPLE_LIMIT) -> dict[str, object]:
    rows = db.execute(
        _ticket_summary_query()
        .where(
            Ticket.tenant_id == int(tenant_id),
            Ticket.status == TicketStatusEnum.OPEN.value,
        )
        .order_by(Ticket.datetime.asc(), Ticket.id.asc())
        .limit(limit)
    ).all()
    total = int(
        db.execute(
            select(func.count(Ticket.id)).where(
                Ticket.tenant_id == int(tenant_id),
                Ticket.status == TicketStatusEnum.OPEN.value,
            )
        ).scalar_one()
        or 0
    )
    return {
        "count": total,
        "tickets": _serialize_ticket_summary_rows(rows),
    }


def get_open_waste_tickets(
    db: Session,
    tenant_id: int,
    *,
    limit: int = AI_ASSISTANT_SAMPLE_LIMIT,
) -> dict[str, object]:
    filters = (
        Ticket.tenant_id == int(tenant_id),
        Ticket.status == TicketStatusEnum.OPEN.value,
        Ticket.transaction_type.in_(_WASTE_TRANSACTION_TYPES),
    )
    rows = db.execute(
        _ticket_summary_query()
        .where(*filters)
        .order_by(Ticket.datetime.asc(), Ticket.id.asc())
        .limit(limit)
    ).all()
    total = int(db.execute(select(func.count(Ticket.id)).where(*filters)).scalar_one() or 0)
    return {
        "count": total,
        "tickets": _serialize_ticket_summary_rows(rows),
    }


def get_uninvoiced_tickets(
    db: Session,
    tenant_id: int,
    *,
    limit: int = AI_ASSISTANT_SAMPLE_LIMIT,
) -> dict[str, object]:
    filters = (
        Ticket.tenant_id == int(tenant_id),
        Ticket.status == TicketStatusEnum.COMPLETE.value,
        Ticket.invoice_id.is_(None),
        Ticket.dont_invoice.is_(False),
        Ticket.paid.is_(False),
    )
    rows = db.execute(
        _ticket_summary_query(include_net=True)
        .where(*filters)
        .order_by(Ticket.datetime.desc(), Ticket.id.desc())
        .limit(limit)
    ).all()
    total = int(db.execute(select(func.count(Ticket.id)).where(*filters)).scalar_one() or 0)
    return {
        "count": total,
        "tickets": _serialize_ticket_summary_rows(rows, include_net=True),
    }


def get_unpaid_invoices(
    db: Session,
    tenant_id: int,
    *,
    limit: int = AI_ASSISTANT_SAMPLE_LIMIT,
) -> dict[str, object]:
    _status_upper, filters = _outstanding_invoice_filters(tenant_id)
    rows = db.execute(
        _invoice_summary_query()
        .where(*filters)
        .order_by(Invoice.invoice_date.desc(), Invoice.id.desc())
        .limit(limit)
    ).all()
    total = int(db.execute(select(func.count(Invoice.id)).where(*filters)).scalar_one() or 0)
    return {
        "count": total,
        "invoices": _serialize_invoice_summary_rows(rows),
    }


def get_overdue_invoices(
    db: Session,
    tenant_id: int,
    *,
    limit: int = AI_ASSISTANT_SAMPLE_LIMIT,
    today: date | None = None,
) -> dict[str, object]:
    resolved_today = today or utcnow().date()
    _status_upper, base_filters = _outstanding_invoice_filters(tenant_id)
    filters = (
        *base_filters,
        Invoice.due_date.is_not(None),
        Invoice.due_date < resolved_today,
    )
    rows = db.execute(
        _invoice_summary_query()
        .where(*filters)
        .order_by(Invoice.due_date.asc(), Invoice.id.asc())
        .limit(limit)
    ).all()
    total = int(db.execute(select(func.count(Invoice.id)).where(*filters)).scalar_one() or 0)
    return {
        "count": total,
        "invoices": _serialize_invoice_summary_rows(rows),
    }


def get_recent_tickets(
    db: Session,
    tenant_id: int,
    *,
    limit: int = AI_ASSISTANT_SAMPLE_LIMIT,
) -> dict[str, object]:
    filters = (
        Ticket.tenant_id == int(tenant_id),
        Ticket.status != TicketStatusEnum.VOID.value,
    )
    rows = db.execute(
        _ticket_summary_query(include_status=True, include_net=True)
        .where(*filters)
        .order_by(Ticket.datetime.desc(), Ticket.id.desc())
        .limit(limit)
    ).all()
    total = int(db.execute(select(func.count(Ticket.id)).where(*filters)).scalar_one() or 0)
    return {
        "count": total,
        "tickets": _serialize_ticket_summary_rows(rows, include_status=True, include_net=True),
    }


def get_top_customer_today(
    db: Session,
    tenant_id: int,
    *,
    today: date | None = None,
) -> dict[str, object] | None:
    resolved_today = today or utcnow().date()
    day_start, day_end = _day_bounds(resolved_today)
    total_weight_kg = func.coalesce(func.sum(Ticket.net_kg), 0).label("weight_kg")
    ticket_count = func.count(Ticket.id).label("ticket_count")
    row = db.execute(
        select(
            Customer.id,
            Customer.name,
            ticket_count,
            total_weight_kg,
        )
        .join(Customer, Ticket.customer_id == Customer.id)
        .where(
            Ticket.tenant_id == int(tenant_id),
            Ticket.status == TicketStatusEnum.COMPLETE.value,
            Ticket.datetime >= day_start,
            Ticket.datetime < day_end,
        )
        .group_by(Customer.id, Customer.name)
        .order_by(desc(total_weight_kg), desc(ticket_count), Customer.name.asc())
        .limit(1)
    ).first()
    if row is None:
        return None
    customer_id, customer_name, customer_ticket_count, customer_weight_kg = row
    return {
        "customer_id": int(customer_id),
        "customer": str(customer_name or "").strip() or None,
        "completed_ticket_count": int(customer_ticket_count or 0),
        "total_kg": _format_weight_kg(customer_weight_kg),
        "total_tonnes": _format_weight_tonnes(customer_weight_kg),
    }


def _include_topic(question_lower: str, hints: tuple[str, ...]) -> bool:
    return any(hint in question_lower for hint in hints)


def detect_question_topics(question: str) -> list[str]:
    normalized = str(question or "").strip().lower()
    return [key for key, hints in _QUESTION_CONTEXT_TOPICS if _include_topic(normalized, hints)]


def build_question_context(
    db: Session,
    tenant_id: int,
    question: str,
    *,
    generated_at: datetime | None = None,
    today: date | None = None,
) -> dict[str, object]:
    resolved_generated_at = generated_at or utcnow()
    resolved_today = today or resolved_generated_at.date()
    context: dict[str, object] = {
        "generated_at": resolved_generated_at.isoformat(),
        "tenant_id": int(tenant_id),
    }
    builders = {
        "open_tickets": lambda: get_open_tickets(db, tenant_id),
        "open_waste_tickets": lambda: get_open_waste_tickets(db, tenant_id),
        "uninvoiced_tickets": lambda: get_uninvoiced_tickets(db, tenant_id),
        "today_weight_total": lambda: get_today_weight_total(db, tenant_id, today=resolved_today),
        "unpaid_invoices": lambda: get_unpaid_invoices(db, tenant_id),
        "overdue_invoices": lambda: get_overdue_invoices(db, tenant_id, today=resolved_today),
        "recent_tickets": lambda: get_recent_tickets(db, tenant_id),
        "top_customer_today": lambda: get_top_customer_today(db, tenant_id, today=resolved_today),
    }
    selected_keys = detect_question_topics(question) or list(builders)
    for key in selected_keys:
        context[key] = builders[key]()
    return context


def build_dashboard_insight_metrics(
    db: Session,
    tenant_id: int,
    *,
    today: date | None = None,
) -> dict[str, object]:
    resolved_today = today or utcnow().date()
    yesterday = resolved_today - timedelta(days=1)
    open_tickets = get_open_tickets(db, tenant_id, limit=3)
    open_waste_tickets = get_open_waste_tickets(db, tenant_id, limit=3)
    uninvoiced_tickets = get_uninvoiced_tickets(db, tenant_id, limit=3)
    unpaid_invoices = get_unpaid_invoices(db, tenant_id, limit=3)
    overdue_invoices = get_overdue_invoices(db, tenant_id, limit=3, today=resolved_today)
    return {
        "date": resolved_today.isoformat(),
        "open_tickets": {
            "count": int(open_tickets.get("count", 0) or 0),
            "sample": open_tickets.get("tickets", []),
        },
        "open_waste_tickets": {
            "count": int(open_waste_tickets.get("count", 0) or 0),
            "sample": open_waste_tickets.get("tickets", []),
        },
        "ready_to_invoice": {
            "count": int(uninvoiced_tickets.get("count", 0) or 0),
            "sample": uninvoiced_tickets.get("tickets", []),
        },
        "unpaid_invoices": {
            "count": int(unpaid_invoices.get("count", 0) or 0),
            "sample": unpaid_invoices.get("invoices", []),
        },
        "overdue_invoices": {
            "count": int(overdue_invoices.get("count", 0) or 0),
            "sample": overdue_invoices.get("invoices", []),
        },
        "today": get_day_weight_total(db, tenant_id, target_date=resolved_today),
        "yesterday": get_day_weight_total(db, tenant_id, target_date=yesterday),
        "top_customer_today": get_top_customer_today(db, tenant_id, today=resolved_today),
    }


def dashboard_insight_metrics_have_activity(metrics: dict[str, object]) -> bool:
    top_customer = metrics.get("top_customer_today")
    return any(
        (
            int(((metrics.get("open_tickets") or {}).get("count")) or 0) > 0,
            int(((metrics.get("open_waste_tickets") or {}).get("count")) or 0) > 0,
            int(((metrics.get("ready_to_invoice") or {}).get("count")) or 0) > 0,
            int(((metrics.get("unpaid_invoices") or {}).get("count")) or 0) > 0,
            int(((metrics.get("overdue_invoices") or {}).get("count")) or 0) > 0,
            int(((metrics.get("today") or {}).get("completed_ticket_count")) or 0) > 0,
            Decimal(str(((metrics.get("today") or {}).get("total_kg_raw")) or 0)) > 0,
            int(((metrics.get("yesterday") or {}).get("completed_ticket_count")) or 0) > 0,
            Decimal(str(((metrics.get("yesterday") or {}).get("total_kg_raw")) or 0)) > 0,
            isinstance(top_customer, dict) and bool(top_customer.get("customer")),
        )
    )
