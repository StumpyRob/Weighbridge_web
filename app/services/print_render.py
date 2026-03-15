from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from ..templating import templates
from .print_context import build_print_base_context

_STATUS_BADGE_STYLE_MARKER = "wb-status-badge-palette"
_STATUS_BADGE_CSS = """
      /* wb-status-badge-palette */
      .status-badge {
        align-items: center;
        background: #eff6ff;
        border: 1px solid transparent;
        border-radius: 999px;
        color: #1d4ed8;
        display: inline-flex;
        font-size: 10px;
        font-weight: 600;
        letter-spacing: 0.01em;
        line-height: 1.1;
        padding: 4px 12px;
        white-space: nowrap;
      }

      .status-badge.status-open {
        background: #fef3c7;
        border-color: #fcd34d;
        color: #92400e;
      }

      .status-badge.status-active,
      .status-badge.status-complete,
      .status-badge.status-paid {
        background: #dcfce7;
        border-color: #bbf7d0;
        color: #166534;
      }

      .status-badge.status-draft {
        background: #fef3c7;
        border-color: #fcd34d;
        color: #92400e;
      }

      .status-badge.status-void,
      .status-badge.status-inactive,
      .status-badge.status-disabled {
        background: #f3f4f6;
        border-color: #e5e7eb;
        color: #6b7280;
      }

      .status-badge.status-info {
        background: #eff6ff;
        border-color: #bfdbfe;
        color: #1d4ed8;
      }
"""

_STATUS_CLASS_MAP = {
    "OPEN": "status-open",
    "ACTIVE": "status-active",
    "COMPLETE": "status-complete",
    "PAID": "status-paid",
    "DRAFT": "status-draft",
    "VOID": "status-void",
    "INACTIVE": "status-inactive",
    "DISABLED": "status-disabled",
}


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


def _status_badge_class(status: object) -> str:
    normalized = str(status or "").strip().upper()
    if not normalized:
        return ""
    return _STATUS_CLASS_MAP.get(normalized, "status-info")


def _inject_status_badge_css(rendered: str) -> str:
    if _STATUS_BADGE_STYLE_MARKER in rendered:
        return rendered
    if "</style>" in rendered:
        return rendered.replace("</style>", f"{_STATUS_BADGE_CSS}\n    </style>", 1)
    if "</head>" in rendered:
        return rendered.replace("</head>", f"<style>{_STATUS_BADGE_CSS}\n    </style>\n  </head>", 1)
    return rendered


def _apply_status_badge_class(rendered: str, *, status_class: str) -> str:
    if not status_class:
        return rendered

    def _replace(match: re.Match[str]) -> str:
        quote = match.group(1)
        raw_classes = match.group(2)
        classes = raw_classes.split()
        if "status-badge" not in classes:
            return match.group(0)
        if any(item.startswith("status-") and item != "status-badge" for item in classes):
            return match.group(0)
        classes.append(status_class)
        return f'class={quote}{" ".join(classes)}{quote}'

    return re.sub(
        r'class=(["\'])([^"\']*\bstatus-badge\b[^"\']*)\1',
        _replace,
        rendered,
    )


def _inject_legacy_ticket_status_badge(
    rendered: str,
    *,
    status: str,
    status_class: str,
) -> str:
    if not status or not status_class or "status-badge" in rendered:
        return rendered
    pattern = (
        r'(<div class="title-block">\s*<h1>TICKET</h1>\s*'
        r'<div class="meta">No:.*?</div>\s*<div class="meta">Date:.*?</div>)'
    )
    replacement = (
        r"\1"
        + f'\n          <div class="meta">Status: <span class="status-badge {status_class}">{status}</span></div>'
    )
    return re.sub(pattern, replacement, rendered, count=1, flags=re.DOTALL)


def _normalize_status_markup(rendered: str, *, payload: dict | None) -> str:
    if "<" not in rendered:
        return rendered
    status_text = str((payload or {}).get("status") or "").strip()
    status_class = _status_badge_class(status_text)
    if not status_class:
        return rendered

    updated = _apply_status_badge_class(rendered, status_class=status_class)
    updated = _inject_legacy_ticket_status_badge(
        updated,
        status=status_text,
        status_class=status_class,
    )
    if "status-badge" in updated:
        updated = _inject_status_badge_css(updated)
    return updated


def render_template_content(
    content: str,
    *,
    db: Session | None = None,
    payload: dict | None = None,
    extra_context: dict[str, Any] | None = None,
) -> str:
    context = _render_context(payload=payload, db=db, extra_context=extra_context)
    rendered = templates.env.from_string(content).render(**context)
    return _normalize_status_markup(
        rendered,
        payload=payload if isinstance(payload, dict) else None,
    )


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
