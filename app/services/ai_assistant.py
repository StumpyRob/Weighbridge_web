from __future__ import annotations

import json
import logging
import re
from datetime import datetime, time, timedelta
from decimal import Decimal

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Customer, Invoice, Ticket, TicketStatusEnum, Vehicle
from ..models.base import utcnow
from .credit import (
    INVOICE_OUTSTANDING_EXCLUDED_STATUSES,
    INVOICE_OUTSTANDING_ISSUED_STATUSES,
)

logger = logging.getLogger(__name__)

AI_ASSISTANT_MODEL = "gpt-5-mini"
SUPPORTED_ASSISTANT_MODELS = (
    AI_ASSISTANT_MODEL,
    "gpt-5",
)
AI_ASSISTANT_MAX_OUTPUT_TOKENS = 400
AI_ASSISTANT_SAMPLE_LIMIT = 5
AI_ASSISTANT_QUESTION_MAX_CHARS = 500
AI_ASSISTANT_SYSTEM_PROMPT = (
    "You are an assistant for a weighbridge management system. "
    "Answer operational questions about tickets, invoices, vehicles, customers, and waste processing. "
    "Keep responses short and practical for operators. "
    "You are read-only and must only use the provided tenant-scoped data. "
    "If the answer is not in the data, say so."
)

_WRITE_REQUEST_PATTERNS = (
    re.compile(r"^\s*(create|add|edit|update|change|delete|remove|void)\b", re.IGNORECASE),
    re.compile(r"\b(can you|please)\s+(create|add|edit|update|change|delete|remove|void)\b", re.IGNORECASE),
    re.compile(r"\b(mark|set)\b.*\b(paid|void|deleted|removed)\b", re.IGNORECASE),
    re.compile(r"\b(send|email|print)\b.*\b(invoice|ticket|receipt)\b", re.IGNORECASE),
)

_OPEN_TICKET_HINTS = ("open ticket", "open tickets", "still open", "awaiting completion")
_UNINVOICED_HINTS = ("uninvoic", "not invoiced", "ready to invoice", "invoice ready")
_TODAY_WEIGHT_HINTS = ("today", "weight", "throughput", "kg", "tonne", "tonnes")
_UNPAID_INVOICE_HINTS = ("unpaid", "outstanding invoice", "outstanding invoices", "overdue invoice", "overdue invoices")
_RECENT_ACTIVITY_HINTS = ("recent", "latest", "activity", "last ticket", "last tickets")


class AIAssistantError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 500) -> None:
        super().__init__(message)
        self.status_code = status_code


def _format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.strftime("%d %b %Y %H:%M")


def _format_date(value) -> str | None:
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


def _format_money(value: object) -> str:
    amount = Decimal(str(value or 0)).quantize(Decimal("0.01"))
    return f"{amount}"


def _vehicle_label(registration: str | None, manual_registration: str | None) -> str | None:
    candidate = str(registration or "").strip() or str(manual_registration or "").strip()
    return candidate or None


def get_open_tickets(db: Session, tenant_id: int, *, limit: int = AI_ASSISTANT_SAMPLE_LIMIT) -> dict[str, object]:
    status_open = TicketStatusEnum.OPEN.value
    total = int(
        db.execute(
            select(func.count(Ticket.id)).where(
                Ticket.tenant_id == int(tenant_id),
                Ticket.status == status_open,
            )
        ).scalar_one()
        or 0
    )
    rows = db.execute(
        select(
            Ticket.ticket_no,
            Ticket.datetime,
            Customer.name,
            Vehicle.registration,
            Ticket.vehicle_reg_text,
        )
        .outerjoin(Customer, Ticket.customer_id == Customer.id)
        .outerjoin(Vehicle, Ticket.vehicle_id == Vehicle.id)
        .where(
            Ticket.tenant_id == int(tenant_id),
            Ticket.status == status_open,
        )
        .order_by(Ticket.datetime.asc(), Ticket.id.asc())
        .limit(limit)
    ).all()
    return {
        "count": total,
        "tickets": [
            {
                "ticket_no": ticket_no,
                "datetime": _format_datetime(ticket_datetime),
                "customer": str(customer_name or "").strip() or None,
                "vehicle": _vehicle_label(vehicle_registration, vehicle_reg_text),
            }
            for ticket_no, ticket_datetime, customer_name, vehicle_registration, vehicle_reg_text in rows
        ],
    }


def get_uninvoiced_tickets(
    db: Session,
    tenant_id: int,
    *,
    limit: int = AI_ASSISTANT_SAMPLE_LIMIT,
) -> dict[str, object]:
    status_complete = TicketStatusEnum.COMPLETE.value
    filters = (
        Ticket.tenant_id == int(tenant_id),
        Ticket.status == status_complete,
        Ticket.invoice_id.is_(None),
        Ticket.dont_invoice.is_(False),
        Ticket.paid.is_(False),
    )
    total = int(
        db.execute(select(func.count(Ticket.id)).where(*filters)).scalar_one()
        or 0
    )
    rows = db.execute(
        select(
            Ticket.ticket_no,
            Ticket.datetime,
            Customer.name,
            Vehicle.registration,
            Ticket.vehicle_reg_text,
            Ticket.net_kg,
        )
        .outerjoin(Customer, Ticket.customer_id == Customer.id)
        .outerjoin(Vehicle, Ticket.vehicle_id == Vehicle.id)
        .where(*filters)
        .order_by(Ticket.datetime.desc(), Ticket.id.desc())
        .limit(limit)
    ).all()
    return {
        "count": total,
        "tickets": [
            {
                "ticket_no": ticket_no,
                "datetime": _format_datetime(ticket_datetime),
                "customer": str(customer_name or "").strip() or None,
                "vehicle": _vehicle_label(vehicle_registration, vehicle_reg_text),
                "net_kg": _format_weight_kg(net_kg),
            }
            for ticket_no, ticket_datetime, customer_name, vehicle_registration, vehicle_reg_text, net_kg in rows
        ],
    }


def get_today_weight_total(db: Session, tenant_id: int) -> dict[str, object]:
    today = utcnow().date()
    day_start = datetime.combine(today, time.min)
    day_end = day_start + timedelta(days=1)
    status_complete = TicketStatusEnum.COMPLETE.value
    total_kg = Decimal(
        str(
            db.execute(
                select(func.coalesce(func.sum(Ticket.net_kg), 0)).where(
                    Ticket.tenant_id == int(tenant_id),
                    Ticket.status == status_complete,
                    Ticket.datetime >= day_start,
                    Ticket.datetime < day_end,
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
                Ticket.datetime >= day_start,
                Ticket.datetime < day_end,
            )
        ).scalar_one()
        or 0
    )
    tonnes = (total_kg / Decimal("1000")).quantize(Decimal("0.001"))
    return {
        "date": today.isoformat(),
        "completed_ticket_count": completed_count,
        "total_kg": _format_weight_kg(total_kg),
        "total_tonnes": f"{_decimal_to_plain_string(tonnes)} tonnes",
    }


def get_unpaid_invoices(
    db: Session,
    tenant_id: int,
    *,
    limit: int = AI_ASSISTANT_SAMPLE_LIMIT,
) -> dict[str, object]:
    status_upper = func.upper(func.coalesce(Invoice.status, ""))
    outstanding_filter = or_(
        status_upper.in_(INVOICE_OUTSTANDING_ISSUED_STATUSES),
        ~status_upper.in_(INVOICE_OUTSTANDING_EXCLUDED_STATUSES),
    )
    filters = (
        Invoice.tenant_id == int(tenant_id),
        status_upper != "",
        outstanding_filter,
    )
    total = int(
        db.execute(select(func.count(Invoice.id)).where(*filters)).scalar_one()
        or 0
    )
    rows = db.execute(
        select(
            Invoice.invoice_no,
            Invoice.invoice_date,
            Invoice.status,
            Invoice.gross_total,
            Customer.name,
        )
        .join(Customer, Invoice.customer_id == Customer.id)
        .where(*filters)
        .order_by(Invoice.invoice_date.desc(), Invoice.id.desc())
        .limit(limit)
    ).all()
    return {
        "count": total,
        "invoices": [
            {
                "invoice_no": invoice_no,
                "invoice_date": _format_date(invoice_date),
                "status": str(status or "").strip() or None,
                "customer": str(customer_name or "").strip() or None,
                "gross_total": _format_money(gross_total),
            }
            for invoice_no, invoice_date, status, gross_total, customer_name in rows
        ],
    }


def get_recent_tickets(
    db: Session,
    tenant_id: int,
    *,
    limit: int = AI_ASSISTANT_SAMPLE_LIMIT,
) -> dict[str, object]:
    total = int(
        db.execute(
            select(func.count(Ticket.id)).where(
                Ticket.tenant_id == int(tenant_id),
                Ticket.status != TicketStatusEnum.VOID.value,
            )
        ).scalar_one()
        or 0
    )
    rows = db.execute(
        select(
            Ticket.ticket_no,
            Ticket.datetime,
            Ticket.status,
            Ticket.net_kg,
            Customer.name,
            Vehicle.registration,
            Ticket.vehicle_reg_text,
        )
        .outerjoin(Customer, Ticket.customer_id == Customer.id)
        .outerjoin(Vehicle, Ticket.vehicle_id == Vehicle.id)
        .where(
            Ticket.tenant_id == int(tenant_id),
            Ticket.status != TicketStatusEnum.VOID.value,
        )
        .order_by(Ticket.datetime.desc(), Ticket.id.desc())
        .limit(limit)
    ).all()
    return {
        "count": total,
        "tickets": [
            {
                "ticket_no": ticket_no,
                "datetime": _format_datetime(ticket_datetime),
                "status": str(status or "").strip() or None,
                "customer": str(customer_name or "").strip() or None,
                "vehicle": _vehicle_label(vehicle_registration, vehicle_reg_text),
                "net_kg": _format_weight_kg(net_kg),
            }
            for ticket_no, ticket_datetime, status, net_kg, customer_name, vehicle_registration, vehicle_reg_text in rows
        ],
    }


def _question_needs_write_access(question: str) -> bool:
    normalized = str(question or "").strip()
    return any(pattern.search(normalized) for pattern in _WRITE_REQUEST_PATTERNS)


def _include_topic(question_lower: str, hints: tuple[str, ...]) -> bool:
    return any(hint in question_lower for hint in hints)


def build_question_context(db: Session, tenant_id: int, question: str) -> dict[str, object]:
    normalized = str(question or "").strip().lower()
    context: dict[str, object] = {
        "generated_at": utcnow().isoformat(),
        "tenant_id": int(tenant_id),
    }

    include_open = _include_topic(normalized, _OPEN_TICKET_HINTS)
    include_uninvoiced = _include_topic(normalized, _UNINVOICED_HINTS)
    include_today_weight = _include_topic(normalized, _TODAY_WEIGHT_HINTS)
    include_unpaid = _include_topic(normalized, _UNPAID_INVOICE_HINTS)
    include_recent = _include_topic(normalized, _RECENT_ACTIVITY_HINTS)

    if not any((include_open, include_uninvoiced, include_today_weight, include_unpaid, include_recent)):
        include_open = include_uninvoiced = include_today_weight = include_unpaid = include_recent = True

    if include_open:
        context["open_tickets"] = get_open_tickets(db, tenant_id)
    if include_uninvoiced:
        context["uninvoiced_tickets"] = get_uninvoiced_tickets(db, tenant_id)
    if include_today_weight:
        context["today_weight_total"] = get_today_weight_total(db, tenant_id)
    if include_unpaid:
        context["unpaid_invoices"] = get_unpaid_invoices(db, tenant_id)
    if include_recent:
        context["recent_tickets"] = get_recent_tickets(db, tenant_id)

    return context


def _json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _build_user_input(question: str, context: dict[str, object]) -> str:
    compact_context = json.dumps(
        context,
        default=_json_default,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (
        "Use only this tenant-scoped operational data.\n"
        f"Question: {question.strip()}\n"
        f"Data: {compact_context}\n"
        "Answer in no more than 4 short sentences."
    )


def resolve_assistant_model(candidate: str | None) -> str:
    normalized = str(candidate or "").strip()
    if normalized in SUPPORTED_ASSISTANT_MODELS:
        return normalized
    return AI_ASSISTANT_MODEL


def _build_openai_payload(
    question: str,
    context: dict[str, object],
    *,
    model: str,
) -> dict[str, object]:
    return {
        "model": resolve_assistant_model(model),
        "instructions": AI_ASSISTANT_SYSTEM_PROMPT,
        "max_output_tokens": AI_ASSISTANT_MAX_OUTPUT_TOKENS,
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": _build_user_input(question, context),
                    }
                ],
            }
        ],
    }


def _post_responses_request(*, api_key: str, payload: dict[str, object]) -> dict[str, object]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        with httpx.Client(timeout=httpx.Timeout(20.0, connect=5.0)) as client:
            response = client.post(
                "https://api.openai.com/v1/responses",
                headers=headers,
                json=payload,
            )
    except httpx.HTTPError as exc:
        logger.warning("AI assistant OpenAI request failed: %s", exc)
        raise AIAssistantError("AI assistant is temporarily unavailable.", status_code=502) from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise AIAssistantError("AI assistant returned an invalid response.", status_code=502) from exc

    if response.status_code >= 400:
        error_message = str(((data.get("error") or {}).get("message")) or "").strip()
        if response.status_code in {401, 403}:
            raise AIAssistantError("AI assistant is not configured correctly.", status_code=503)
        raise AIAssistantError(
            error_message or "AI assistant request failed.",
            status_code=502,
        )

    return data


def _extract_response_text(payload: dict[str, object]) -> str:
    top_level = str(payload.get("output_text") or "").strip()
    if top_level:
        return top_level

    fragments: list[str] = []
    for item in payload.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for content in item.get("content", []) or []:
                if not isinstance(content, dict):
                    continue
                text = str(content.get("text") or "").strip()
                if text:
                    fragments.append(text)
        else:
            text = str(item.get("text") or "").strip()
            if text:
                fragments.append(text)

    if fragments:
        return "\n".join(fragment for fragment in fragments if fragment).strip()

    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if isinstance(message, dict):
                content = str(message.get("content") or "").strip()
                if content:
                    return content

    raise AIAssistantError("AI assistant returned an empty response.", status_code=502)


def answer_question(
    db: Session,
    tenant_id: int,
    question: str,
    *,
    model: str | None = None,
) -> str:
    clean_question = str(question or "").strip()
    if not clean_question:
        raise AIAssistantError("Question is required.", status_code=400)
    if len(clean_question) > AI_ASSISTANT_QUESTION_MAX_CHARS:
        raise AIAssistantError(
            f"Question must be {AI_ASSISTANT_QUESTION_MAX_CHARS} characters or fewer.",
            status_code=400,
        )

    if _question_needs_write_access(clean_question):
        return (
            "The assistant is read-only. It can answer questions about current tenant data, "
            "but it cannot create, edit, delete, void, pay, email, or print records."
        )

    api_key = str(settings.openai_api_key or "").strip()
    if not api_key:
        raise AIAssistantError("AI assistant is not configured.", status_code=503)

    context = build_question_context(db, tenant_id, clean_question)
    payload = _build_openai_payload(
        clean_question,
        context,
        model=resolve_assistant_model(model),
    )
    response_payload = _post_responses_request(api_key=api_key, payload=payload)
    return _extract_response_text(response_payload)
