from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import httpx
from sqlalchemy.orm import Session

from ..config import settings
from ..models.base import utcnow
from .ai_assistant_data import (
    AI_ASSISTANT_SAMPLE_LIMIT,
    build_dashboard_insight_metrics as _build_dashboard_insight_metrics,
    build_question_context as _build_question_context,
    dashboard_insight_metrics_have_activity,
    detect_question_topics,
    get_day_weight_total,
    get_open_tickets,
    get_open_waste_tickets,
    get_overdue_invoices,
    get_recent_tickets,
    get_today_weight_total,
    get_top_customer_today,
    get_uninvoiced_tickets,
    get_unpaid_invoices,
)

logger = logging.getLogger(__name__)

AI_ASSISTANT_MODEL = "gpt-5-mini"
SUPPORTED_ASSISTANT_MODELS = (
    AI_ASSISTANT_MODEL,
    "gpt-5",
)
SUPPORTED_ASSISTANT_RESPONSE_STYLES = (
    "concise",
    "balanced",
    "detailed",
)
SUPPORTED_ASSISTANT_FOCUS_AREAS = (
    "operations",
    "accounts",
    "mixed",
)
AI_ASSISTANT_MAX_OUTPUT_TOKENS = 320
AI_ASSISTANT_QUESTION_MAX_CHARS = 500
AI_ASSISTANT_CUSTOM_INSTRUCTIONS_MAX_CHARS = 240
AI_ASSISTANT_REASONING_EFFORT = "minimal"
AI_ASSISTANT_TEXT_VERBOSITY = "low"
AI_DASHBOARD_INSIGHTS_MAX_OUTPUT_TOKENS = 220
AI_ASSISTANT_HTTP_TIMEOUT_SECONDS = 30.0
AI_DASHBOARD_INSIGHTS_CACHE_TTL_SECONDS = 600
AI_ASSISTANT_SYSTEM_PROMPT = (
    "You are the Weighbridge Web operational assistant. "
    "Answer operational questions for weighbridge operators and admins in a concise, practical, factual way. "
    "Use only the tenant-scoped data provided in this request. "
    "Never invent or assume tickets, invoices, customers, vehicles, weights, totals, dates, or statuses. "
    "If the data is unavailable or the answer is not present, say so clearly. "
    "You are read-only. Never create, edit, delete, complete, void, pay, print, email, or invoice records. "
    "If asked to perform an action, briefly say that you cannot do that. "
    "Keep answers short and useful. Put the direct answer on the first line. "
    "When listing records, prefer short bullet points and include ticket numbers or invoice numbers when relevant. "
    "Avoid generic chatbot wording, long explanations, or speculation.\n"
    "\n"
    "Examples:\n"
    "Q: Which tickets are still open?\n"
    "A: There are 4 open tickets.\n"
    "- Oldest: 26-00024 for Premier Groundworks\n"
    "- Latest: 26-00031 for ACME Recycling\n"
    "\n"
    "Q: How much weight did we process today?\n"
    "A: Today we processed 255.5 tonnes across 26 completed tickets.\n"
    "\n"
    "Q: Which invoices are unpaid?\n"
    "A: There are 3 unpaid invoices.\n"
    "- INV-1024 for Premier Groundworks, 1240.00\n"
    "- INV-1027 for North Aggregates, 980.00\n"
    "\n"
    "Q: Show recent activity for Premier Groundworks.\n"
    "A: I can only report activity that appears in the provided tenant data.\n"
    "- Latest ticket: 26-00031\n"
    "- Status: complete"
)
AI_DASHBOARD_INSIGHTS_PROMPT = (
    "For dashboard insights, return 3 to 5 short operational bullet points only. "
    "No heading, no intro, and no closing sentence. "
    "Use only the provided tenant-scoped metrics. "
    "Keep each bullet short, high-level, and easy to scan. "
    "Do not list ticket numbers, invoice numbers, or long record lists. "
    "Do not repeat multiple customer names or low-level details already visible elsewhere on the dashboard. "
    "You may mention at most one named customer example in a bullet if it adds useful context. "
    "Do not invent trends or comparisons that are not supported by the metrics. "
    "If the metrics are insufficient, say exactly: Not enough recent activity to generate insights yet."
)
AI_DASHBOARD_INSIGHTS_FALLBACK = "Not enough recent activity to generate insights yet."
AI_DASHBOARD_INSIGHTS_UNAVAILABLE = "AI insights are temporarily unavailable."
_dashboard_insights_cache: dict[str, tuple[float, tuple[str, ...]]] = {}

_WRITE_REQUEST_PATTERNS = (
    re.compile(r"^\s*(create|add|edit|update|change|delete|remove|void)\b", re.IGNORECASE),
    re.compile(r"\b(can you|please)\s+(create|add|edit|update|change|delete|remove|void)\b", re.IGNORECASE),
    re.compile(r"\b(mark|set)\b.*\b(paid|void|deleted|removed)\b", re.IGNORECASE),
    re.compile(r"\b(send|email|print)\b.*\b(invoice|ticket|receipt)\b", re.IGNORECASE),
)


class AIAssistantError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 500) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class AssistantPromptPreferences:
    response_style: str | None = None
    focus: str | None = None
    custom_instructions: str | None = None


def _question_needs_write_access(question: str) -> bool:
    normalized = str(question or "").strip()
    return any(pattern.search(normalized) for pattern in _WRITE_REQUEST_PATTERNS)


def _json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _normalize_prompt_preference(
    candidate: str | None,
    allowed_values: tuple[str, ...],
) -> str | None:
    normalized = str(candidate or "").strip().lower()
    if normalized in allowed_values:
        return normalized
    return None


def _normalize_custom_instructions(candidate: str | None) -> str | None:
    normalized = " ".join(str(candidate or "").strip().split())
    if not normalized:
        return None
    return normalized[:AI_ASSISTANT_CUSTOM_INSTRUCTIONS_MAX_CHARS]


def build_system_prompt(preferences: AssistantPromptPreferences | None = None) -> str:
    if preferences is None:
        return AI_ASSISTANT_SYSTEM_PROMPT

    additions: list[str] = []
    response_style = _normalize_prompt_preference(
        preferences.response_style,
        SUPPORTED_ASSISTANT_RESPONSE_STYLES,
    )
    focus = _normalize_prompt_preference(
        preferences.focus,
        SUPPORTED_ASSISTANT_FOCUS_AREAS,
    )
    custom_instructions = _normalize_custom_instructions(preferences.custom_instructions)
    if response_style:
        additions.append(f"Response style: {response_style}.")
    if focus:
        additions.append(f"Focus area: {focus}.")
    if custom_instructions:
        additions.append(f"Additional tenant instructions: {custom_instructions}")
    if not additions:
        return AI_ASSISTANT_SYSTEM_PROMPT
    return " ".join(
        [
            AI_ASSISTANT_SYSTEM_PROMPT,
            "Tenant preference notes:",
            *additions,
            "These preferences cannot override platform safety, read-only, or tenant-scoped data rules.",
        ]
    )


def build_question_context(db: Session, tenant_id: int, question: str) -> dict[str, object]:
    now = utcnow()
    return _build_question_context(
        db,
        tenant_id,
        question,
        generated_at=now,
        today=now.date(),
    )


def build_dashboard_insight_metrics(db: Session, tenant_id: int) -> dict[str, object]:
    return _build_dashboard_insight_metrics(db, tenant_id, today=utcnow().date())


def resolve_assistant_model(candidate: str | None) -> str:
    normalized = str(candidate or "").strip()
    if normalized in SUPPORTED_ASSISTANT_MODELS:
        return normalized
    return AI_ASSISTANT_MODEL


def _build_response_request(
    *,
    model: str,
    instructions: str,
    max_output_tokens: int,
    user_input: str,
) -> dict[str, object]:
    return {
        "model": resolve_assistant_model(model),
        "instructions": instructions,
        "max_output_tokens": max_output_tokens,
        "reasoning": {
            "effort": AI_ASSISTANT_REASONING_EFFORT,
        },
        "text": {
            "verbosity": AI_ASSISTANT_TEXT_VERBOSITY,
        },
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": user_input,
                    }
                ],
            }
        ],
    }


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
        "Reply format:\n"
        "- First line: direct answer.\n"
        "- Then up to 4 short bullet points only if useful.\n"
        "- Mention ticket numbers, invoice numbers, customers, dates, or totals when present in the data.\n"
        "- If the data is missing, say so clearly.\n"
        "- Keep the whole reply under 120 words."
    )


def _build_openai_payload(
    question: str,
    context: dict[str, object],
    *,
    model: str,
    prompt_preferences: AssistantPromptPreferences | None = None,
) -> dict[str, object]:
    return _build_response_request(
        model=model,
        instructions=build_system_prompt(prompt_preferences),
        max_output_tokens=AI_ASSISTANT_MAX_OUTPUT_TOKENS,
        user_input=_build_user_input(question, context),
    )


def _post_responses_request(*, api_key: str, payload: dict[str, object]) -> dict[str, object]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        with httpx.Client(
            timeout=httpx.Timeout(AI_ASSISTANT_HTTP_TIMEOUT_SECONDS, connect=5.0)
        ) as client:
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


def _record_href(record_type: str, record_id: object) -> str | None:
    try:
        resolved_id = int(record_id or 0)
    except (TypeError, ValueError):
        return None
    if resolved_id <= 0:
        return None
    return {
        "ticket": f"/tickets/{resolved_id}",
        "invoice": f"/invoices/{resolved_id}",
        "customer": f"/customers/{resolved_id}",
        "vehicle": f"/vehicles/{resolved_id}",
    }.get(record_type)


def _build_related_link(record_type: str, record_id: object, label: object) -> dict[str, object] | None:
    href = _record_href(record_type, record_id)
    title = str(label or "").strip()
    if not href or not title:
        return None
    return {
        "record_type": record_type,
        "record_id": int(record_id),
        "label": title,
        "href": href,
    }


def _join_meta_parts(*parts: object) -> str:
    return " | ".join(str(part).strip() for part in parts if str(part or "").strip())


def _dedupe_links(links: list[dict[str, object]]) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for link in links:
        href = str(link.get("href") or "").strip()
        label = str(link.get("label") or "").strip()
        if not href or not label:
            continue
        key = (href, label)
        if key in seen:
            continue
        seen.add(key)
        items.append(link)
    return items


def _build_ticket_result_item(record: dict[str, object]) -> dict[str, object] | None:
    ticket_id = record.get("ticket_id")
    title = str(record.get("ticket_no") or "").strip()
    href = _record_href("ticket", ticket_id)
    if not href or not title:
        return None
    links = _dedupe_links(
        [
            link
            for link in (
                _build_related_link("customer", record.get("customer_id"), record.get("customer")),
                _build_related_link("vehicle", record.get("vehicle_id"), record.get("vehicle")),
            )
            if link
        ]
    )
    return {
        "record_type": "ticket",
        "record_id": int(ticket_id),
        "title": title,
        "href": href,
        "meta": _join_meta_parts(
            record.get("datetime"),
            record.get("customer"),
            record.get("vehicle"),
            record.get("status"),
            record.get("net_kg"),
        ),
        "links": links,
    }


def _build_invoice_result_item(record: dict[str, object]) -> dict[str, object] | None:
    invoice_id = record.get("invoice_id")
    title = str(record.get("invoice_no") or "").strip()
    href = _record_href("invoice", invoice_id)
    if not href or not title:
        return None
    due_date = str(record.get("due_date") or "").strip()
    invoice_date = str(record.get("invoice_date") or "").strip()
    when_label = f"Due {due_date}" if due_date else invoice_date
    links = _dedupe_links(
        [
            link
            for link in (
                _build_related_link("customer", record.get("customer_id"), record.get("customer")),
            )
            if link
        ]
    )
    return {
        "record_type": "invoice",
        "record_id": int(invoice_id),
        "title": title,
        "href": href,
        "meta": _join_meta_parts(
            record.get("customer"),
            when_label,
            record.get("gross_total"),
            record.get("status"),
        ),
        "links": links,
    }


def _build_customer_result_item(record: dict[str, object]) -> dict[str, object] | None:
    customer_id = record.get("customer_id")
    title = str(record.get("customer") or "").strip()
    href = _record_href("customer", customer_id)
    if not href or not title:
        return None
    ticket_count = int(record.get("completed_ticket_count") or 0)
    tonnes = str(record.get("total_tonnes") or "").strip()
    ticket_label = f"{ticket_count} ticket today" if ticket_count == 1 else f"{ticket_count} tickets today"
    return {
        "record_type": "customer",
        "record_id": int(customer_id),
        "title": title,
        "href": href,
        "meta": _join_meta_parts(ticket_label if ticket_count else "", tonnes),
        "links": [],
    }


def _build_structured_result_items(question: str, context: dict[str, object]) -> list[dict[str, object]]:
    topics = detect_question_topics(question)
    if not topics:
        return []

    candidate_items: list[dict[str, object]] = []
    for topic in topics:
        topic_payload = context.get(topic)
        if topic in {"open_tickets", "open_waste_tickets", "uninvoiced_tickets", "recent_tickets"}:
            records = ((topic_payload or {}).get("tickets")) if isinstance(topic_payload, dict) else []
            for record in records or []:
                if isinstance(record, dict):
                    item = _build_ticket_result_item(record)
                    if item:
                        candidate_items.append(item)
        elif topic in {"unpaid_invoices", "overdue_invoices"}:
            records = ((topic_payload or {}).get("invoices")) if isinstance(topic_payload, dict) else []
            for record in records or []:
                if isinstance(record, dict):
                    item = _build_invoice_result_item(record)
                    if item:
                        candidate_items.append(item)
        elif topic == "top_customer_today" and isinstance(topic_payload, dict):
            item = _build_customer_result_item(topic_payload)
            if item:
                candidate_items.append(item)

    items: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for item in candidate_items:
        href = str(item.get("href") or "").strip()
        title = str(item.get("title") or "").strip()
        key = (str(item.get("record_type") or "").strip(), href or title)
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
        if len(items) >= AI_ASSISTANT_SAMPLE_LIMIT:
            break
    return items


def _dashboard_insight_prompt_metrics(metrics: dict[str, object]) -> dict[str, object]:
    compact = {
        "date": metrics.get("date"),
        "open_tickets": {"count": int(((metrics.get("open_tickets") or {}).get("count")) or 0)},
        "open_waste_tickets": {
            "count": int(((metrics.get("open_waste_tickets") or {}).get("count")) or 0)
        },
        "ready_to_invoice": {
            "count": int(((metrics.get("ready_to_invoice") or {}).get("count")) or 0)
        },
        "unpaid_invoices": {
            "count": int(((metrics.get("unpaid_invoices") or {}).get("count")) or 0)
        },
        "overdue_invoices": {
            "count": int(((metrics.get("overdue_invoices") or {}).get("count")) or 0)
        },
        "today": {
            "completed_ticket_count": int(((metrics.get("today") or {}).get("completed_ticket_count")) or 0),
            "total_tonnes": str(((metrics.get("today") or {}).get("total_tonnes")) or "").strip(),
        },
        "yesterday": {
            "completed_ticket_count": int(((metrics.get("yesterday") or {}).get("completed_ticket_count")) or 0),
            "total_tonnes": str(((metrics.get("yesterday") or {}).get("total_tonnes")) or "").strip(),
        },
    }
    top_customer = metrics.get("top_customer_today")
    if isinstance(top_customer, dict) and str(top_customer.get("customer") or "").strip():
        compact["top_customer_today"] = {
            "customer": str(top_customer.get("customer") or "").strip(),
            "completed_ticket_count": int(top_customer.get("completed_ticket_count") or 0),
            "total_tonnes": str(top_customer.get("total_tonnes") or "").strip(),
        }
    return compact


def _build_dashboard_insights_input(metrics: dict[str, object]) -> str:
    compact_metrics = json.dumps(
        _dashboard_insight_prompt_metrics(metrics),
        default=_json_default,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (
        "Generate 3 to 5 short operational dashboard insights from these tenant-scoped metrics.\n"
        "Return bullet points only.\n"
        f"Metrics: {compact_metrics}"
    )


def _extract_dashboard_insight_items(payload: dict[str, object]) -> list[str]:
    items: list[str] = []
    for raw_line in re.split(r"[\r\n]+", _extract_response_text(payload)):
        normalized = re.sub(r"^(?:[-*]|[0-9]+\.)\s*", "", str(raw_line or "").strip()).strip()
        if normalized:
            items.append(normalized)
    if not items:
        return [AI_DASHBOARD_INSIGHTS_FALLBACK]
    return items[:5]


def _dashboard_insights_cache_key(
    tenant_id: int,
    metrics: dict[str, object],
    model: str | None,
) -> str:
    serialized = json.dumps(
        {
            "tenant_id": int(tenant_id),
            "model": resolve_assistant_model(model),
            "metrics": _dashboard_insight_prompt_metrics(metrics),
        },
        default=_json_default,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha1(serialized.encode("utf-8")).hexdigest()


def _get_cached_dashboard_insights(cache_key: str) -> list[str] | None:
    cached = _dashboard_insights_cache.get(cache_key)
    if cached is None:
        return None
    cached_at, items = cached
    if time.monotonic() - cached_at > AI_DASHBOARD_INSIGHTS_CACHE_TTL_SECONDS:
        _dashboard_insights_cache.pop(cache_key, None)
        return None
    return list(items)


def _store_cached_dashboard_insights(cache_key: str, items: list[str]) -> None:
    _dashboard_insights_cache[cache_key] = (
        time.monotonic(),
        tuple(str(item or "").strip() for item in items if str(item or "").strip()),
    )


def _normalize_question(question: str) -> str:
    clean_question = str(question or "").strip()
    if not clean_question:
        raise AIAssistantError("Question is required.", status_code=400)
    if len(clean_question) > AI_ASSISTANT_QUESTION_MAX_CHARS:
        raise AIAssistantError(
            f"Question must be {AI_ASSISTANT_QUESTION_MAX_CHARS} characters or fewer.",
            status_code=400,
        )
    return clean_question


def answer_question_with_results(
    db: Session,
    tenant_id: int,
    question: str,
    *,
    model: str | None = None,
) -> dict[str, object]:
    clean_question = _normalize_question(question)
    if _question_needs_write_access(clean_question):
        return {
            "answer": (
                "The assistant is read-only. It can answer questions about current tenant data, "
                "but it cannot create, edit, delete, void, pay, email, or print records."
            ),
            "items": [],
        }

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
    return {
        "answer": _extract_response_text(response_payload),
        "items": _build_structured_result_items(clean_question, context),
    }


def answer_question(
    db: Session,
    tenant_id: int,
    question: str,
    *,
    model: str | None = None,
) -> str:
    return str(
        answer_question_with_results(
            db,
            tenant_id,
            question,
            model=model,
        ).get("answer")
        or ""
    )


def generate_dashboard_insights(
    db: Session,
    tenant_id: int,
    *,
    model: str | None = None,
) -> dict[str, object]:
    metrics = build_dashboard_insight_metrics(db, tenant_id)
    if not dashboard_insight_metrics_have_activity(metrics):
        return {"items": [], "message": AI_DASHBOARD_INSIGHTS_FALLBACK, "metrics": metrics}

    cache_key = _dashboard_insights_cache_key(tenant_id, metrics, model)
    cached_items = _get_cached_dashboard_insights(cache_key)
    if cached_items:
        return {"items": cached_items, "message": "", "metrics": metrics}

    api_key = str(settings.openai_api_key or "").strip()
    if not api_key:
        return {"items": [], "message": AI_DASHBOARD_INSIGHTS_UNAVAILABLE, "metrics": metrics}

    payload = _build_response_request(
        model=resolve_assistant_model(model),
        instructions=f"{build_system_prompt()} {AI_DASHBOARD_INSIGHTS_PROMPT}",
        max_output_tokens=AI_DASHBOARD_INSIGHTS_MAX_OUTPUT_TOKENS,
        user_input=_build_dashboard_insights_input(metrics),
    )
    try:
        items = _extract_dashboard_insight_items(
            _post_responses_request(api_key=api_key, payload=payload)
        )
    except AIAssistantError:
        if cached_items:
            return {"items": cached_items, "message": "", "metrics": metrics}
        return {"items": [], "message": AI_DASHBOARD_INSIGHTS_UNAVAILABLE, "metrics": metrics}
    _store_cached_dashboard_insights(cache_key, items)
    return {
        "items": items,
        "message": "",
        "metrics": metrics,
    }


__all__ = [
    "AI_ASSISTANT_CUSTOM_INSTRUCTIONS_MAX_CHARS",
    "AI_ASSISTANT_MAX_OUTPUT_TOKENS",
    "AI_ASSISTANT_MODEL",
    "AI_ASSISTANT_QUESTION_MAX_CHARS",
    "AI_ASSISTANT_REASONING_EFFORT",
    "AI_ASSISTANT_SAMPLE_LIMIT",
    "AI_ASSISTANT_HTTP_TIMEOUT_SECONDS",
    "AI_ASSISTANT_SYSTEM_PROMPT",
    "AI_ASSISTANT_TEXT_VERBOSITY",
    "AI_DASHBOARD_INSIGHTS_CACHE_TTL_SECONDS",
    "AI_DASHBOARD_INSIGHTS_FALLBACK",
    "AI_DASHBOARD_INSIGHTS_MAX_OUTPUT_TOKENS",
    "AI_DASHBOARD_INSIGHTS_PROMPT",
    "AI_DASHBOARD_INSIGHTS_UNAVAILABLE",
    "AIAssistantError",
    "AssistantPromptPreferences",
    "SUPPORTED_ASSISTANT_FOCUS_AREAS",
    "SUPPORTED_ASSISTANT_MODELS",
    "SUPPORTED_ASSISTANT_RESPONSE_STYLES",
    "_build_openai_payload",
    "_extract_response_text",
    "_post_responses_request",
    "answer_question",
    "answer_question_with_results",
    "build_dashboard_insight_metrics",
    "build_question_context",
    "build_system_prompt",
    "generate_dashboard_insights",
    "get_day_weight_total",
    "get_open_tickets",
    "get_open_waste_tickets",
    "get_overdue_invoices",
    "get_recent_tickets",
    "get_today_weight_total",
    "get_top_customer_today",
    "get_uninvoiced_tickets",
    "get_unpaid_invoices",
    "resolve_assistant_model",
]
