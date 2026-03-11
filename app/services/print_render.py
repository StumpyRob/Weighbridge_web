from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..templating import templates
from .print_context import build_print_base_context


def _alias_context(payload: dict) -> dict:
    customer = payload.get("customer") if isinstance(payload.get("customer"), dict) else {}
    weights = payload.get("weights") if isinstance(payload.get("weights"), dict) else {}
    pricing = {
        "qty": weights.get("qty"),
        "qty_display": weights.get("qty_display"),
        "unit_price": weights.get("unit_price"),
        "unit_price_display": weights.get("unit_price_display"),
        "total": weights.get("total"),
        "total_display": weights.get("total_display"),
    }
    ticket = {
        "id": payload.get("ticket_id"),
        "number": payload.get("ticket_no"),
        "datetime": payload.get("datetime_display"),
        "datetime_iso": payload.get("datetime_iso"),
        "status": payload.get("status"),
        "direction": payload.get("direction"),
        "transaction_type": payload.get("transaction_type"),
        "po_number": payload.get("po_number"),
    }
    return {
        "ticket": ticket,
        "customer": customer,
        "weights": weights,
        "pricing": pricing,
    }


def _render_context(
    *,
    payload: dict | None = None,
    db: Session | None = None,
    extra_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context = build_print_base_context(db)
    payload_dict = payload if isinstance(payload, dict) else {}
    context["payload"] = payload_dict
    context.update(_alias_context(payload_dict))
    if extra_context:
        context.update(extra_context)
        if "payload" not in context:
            context["payload"] = payload_dict
    return context


def render_template_content(
    content: str,
    *,
    db: Session | None = None,
    payload: dict | None = None,
    extra_context: dict[str, Any] | None = None,
) -> str:
    context = _render_context(payload=payload, db=db, extra_context=extra_context)
    return templates.env.from_string(content).render(**context)


def render_from_content(
    payload: dict,
    content: str,
    *,
    db: Session | None = None,
    extra_context: dict[str, Any] | None = None,
) -> str:
    return render_template_content(
        content,
        db=db,
        payload=payload,
        extra_context=extra_context,
    )
