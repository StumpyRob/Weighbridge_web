from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from ..config import settings
from ..templating import templates
from .print_context import build_print_base_context


def _normalize_template_name(template_name: str) -> str:
    normalized = str(template_name or "").strip()
    if normalized.startswith("print/"):
        normalized = normalized[len("print/") :]
    return normalized.lstrip("/")


def _resolve_builtin_template_path(template_name: str) -> str:
    normalized = _normalize_template_name(template_name)
    return f"print/{normalized}"


def _resolve_override_template_path(template_name: str) -> Path | None:
    override_root_raw = (settings.print_template_override_dir or "").strip()
    if not override_root_raw:
        return None

    override_root = Path(override_root_raw).expanduser().resolve()
    relative_name = _normalize_template_name(template_name)
    candidate = (override_root / relative_name).resolve()
    if candidate != override_root and override_root not in candidate.parents:
        return None
    if not candidate.is_file():
        return None
    return candidate


def _render_from_template_name(
    payload: dict,
    template_name: str,
    *,
    db: Session | None = None,
    extra_context: dict[str, Any] | None = None,
) -> str:
    override_path = _resolve_override_template_path(template_name)
    if override_path is not None:
        template_text = override_path.read_text(encoding="utf-8")
        return render_from_content(
            payload,
            template_text,
            db=db,
            extra_context=extra_context,
        )

    template = templates.env.get_template(_resolve_builtin_template_path(template_name))
    context = _render_context(payload=payload, db=db, extra_context=extra_context)
    return template.render(**context)


def load_template_source(template_name: str) -> str:
    override_path = _resolve_override_template_path(template_name)
    if override_path is not None:
        return override_path.read_text(encoding="utf-8")
    template = templates.env.get_template(_resolve_builtin_template_path(template_name))
    # Jinja keeps the original source in loader-backed templates.
    source, _, _ = template.environment.loader.get_source(  # type: ignore[union-attr]
        template.environment,
        _resolve_builtin_template_path(template_name),
    )
    return source


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


def render_thermal(
    payload: dict,
    template_name: str = "thermal_default.txt",
    *,
    db: Session | None = None,
    extra_context: dict[str, Any] | None = None,
) -> str:
    return _render_from_template_name(
        payload,
        template_name,
        db=db,
        extra_context=extra_context,
    )


def render_a4_html(
    payload: dict,
    template_name: str = "a4_default.html",
    *,
    db: Session | None = None,
    extra_context: dict[str, Any] | None = None,
) -> str:
    return _render_from_template_name(
        payload,
        template_name,
        db=db,
        extra_context=extra_context,
    )
