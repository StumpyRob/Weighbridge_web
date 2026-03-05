from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from .models import AuditEvent, User

logger = logging.getLogger(__name__)

_MAX_DETAILS_ITEMS = 20
_MAX_DETAILS_DEPTH = 3
_MAX_TEXT_LEN = 255


def _trim_text(value: Any, *, max_len: int = _MAX_TEXT_LEN) -> str:
    text = str(value if value is not None else "").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _trim_text(value)
    if depth >= _MAX_DETAILS_DEPTH:
        return _trim_text(value)
    if isinstance(value, Mapping):
        payload: dict[str, Any] = {}
        for idx, (key, item) in enumerate(value.items()):
            if idx >= _MAX_DETAILS_ITEMS:
                payload["__truncated__"] = True
                break
            payload[_trim_text(key, max_len=80)] = _json_safe(item, depth=depth + 1)
        return payload
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        items: list[Any] = []
        for idx, item in enumerate(value):
            if idx >= _MAX_DETAILS_ITEMS:
                items.append("...truncated...")
                break
            items.append(_json_safe(item, depth=depth + 1))
        return items
    return _trim_text(value)


def _request_ip_address(request: Request | None) -> str | None:
    if request is None:
        return None
    forwarded_for = str(request.headers.get("x-forwarded-for", "")).strip()
    if forwarded_for:
        first = forwarded_for.split(",")[0].strip()
        if first:
            return _trim_text(first, max_len=64)
    real_ip = str(request.headers.get("x-real-ip", "")).strip()
    if real_ip:
        return _trim_text(real_ip, max_len=64)
    if request.client and request.client.host:
        return _trim_text(request.client.host, max_len=64)
    return None


def diff(
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    keys: Sequence[str],
) -> dict[str, dict[str, Any]]:
    before_map = before or {}
    after_map = after or {}
    changed: dict[str, dict[str, Any]] = {}
    for key in keys:
        previous = before_map.get(key)
        current = after_map.get(key)
        if previous != current:
            changed[key] = {"from": previous, "to": current}
    return {"changed": changed}


def user_snapshot(user: User | None) -> dict[str, Any]:
    if user is None:
        return {}
    username = str(getattr(user, "username", "") or "").strip() or None
    email = str(getattr(user, "email", "") or "").strip() or None
    snapshot: dict[str, Any] = {
        "username": username,
        "email": email,
        "is_active": bool(getattr(user, "is_active", False)),
    }
    if hasattr(user, "role"):
        role = str(getattr(user, "role", "") or "").strip().upper() or None
        snapshot["role"] = role
    return snapshot


def log(
    db: Session,
    request: Request | None,
    *,
    action: str,
    entity_type: str,
    entity_id: int | str | None = None,
    summary: str | None = None,
    details: Mapping[str, Any] | Sequence[Any] | None = None,
    tenant_id: str | int | None = None,
    user: User | None = None,
) -> AuditEvent | None:
    try:
        resolved_tenant_id = tenant_id
        if resolved_tenant_id is None and request is not None:
            resolved_tenant_id = getattr(getattr(request, "state", None), "tenant_id", None)
        current_user = user
        if current_user is None and request is not None:
            maybe_user = getattr(getattr(request, "state", None), "current_user", None)
            if isinstance(maybe_user, User):
                current_user = maybe_user
        user_id = int(current_user.id) if current_user is not None else None
        record = AuditEvent(
            tenant_id=(
                _trim_text(resolved_tenant_id, max_len=64)
                if resolved_tenant_id is not None
                else None
            ),
            user_id=user_id,
            ip_address=_request_ip_address(request),
            action=_trim_text(action.upper(), max_len=32),
            entity_type=_trim_text(entity_type.lower(), max_len=64),
            entity_id=_trim_text(entity_id, max_len=64) if entity_id is not None else None,
            summary=_trim_text(summary, max_len=255) if summary else None,
            details_json=_json_safe(details) if details is not None else None,
        )
        db.add(record)
        return record
    except Exception:
        logger.warning(
            "Audit logging failed: action=%s entity=%s entity_id=%s",
            action,
            entity_type,
            entity_id,
            exc_info=True,
        )
        return None
