from __future__ import annotations

from contextvars import ContextVar, Token
import re
from typing import Any

from fastapi import HTTPException, Request

from .config import settings
from .models import Tenant

_SUBDOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_NIP_IO_RE = re.compile(
    r"^(?P<sub>[a-z0-9-]+)\.(?:\d{1,3}\.){3}\d{1,3}\.nip\.io$",
    re.IGNORECASE,
)

_tenant_id_ctx: ContextVar[int | None] = ContextVar("tenant_id", default=None)
_platform_mode_ctx: ContextVar[bool] = ContextVar("platform_mode", default=False)


def normalize_subdomain(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if not _SUBDOMAIN_RE.fullmatch(normalized):
        return ""
    return normalized


def host_without_port(raw_host: str | None) -> str:
    value = str(raw_host or "").strip().lower()
    if not value:
        return ""
    if value.startswith("[") and "]" in value:
        return value.split("]")[0].removeprefix("[")
    if ":" in value:
        return value.split(":", 1)[0]
    return value


def resolve_subdomain(host: str) -> str:
    cleaned = host_without_port(host)
    if not cleaned:
        return ""

    if cleaned in {"localhost", "127.0.0.1", "testserver"}:
        return settings.effective_default_tenant_subdomain

    if cleaned.endswith(".localhost"):
        return normalize_subdomain(cleaned[: -len(".localhost")].split(".")[0])

    nip_match = _NIP_IO_RE.fullmatch(cleaned)
    if nip_match:
        return normalize_subdomain(nip_match.group("sub"))

    base_domain = settings.effective_base_domain
    if base_domain and cleaned == base_domain:
        return settings.effective_default_tenant_subdomain
    if base_domain and cleaned.endswith(f".{base_domain}"):
        candidate = cleaned[: -(len(base_domain) + 1)]
        return normalize_subdomain(candidate.split(".")[0])

    parts = cleaned.split(".")
    if len(parts) <= 2:
        return settings.effective_default_tenant_subdomain
    if len(parts) >= 3:
        return normalize_subdomain(parts[0])
    return ""


def set_request_tenant_context(*, tenant_id: int | None, platform_mode: bool) -> tuple[Token, Token]:
    tenant_token = _tenant_id_ctx.set(tenant_id)
    platform_token = _platform_mode_ctx.set(bool(platform_mode))
    return tenant_token, platform_token


def reset_request_tenant_context(tokens: tuple[Token, Token]) -> None:
    tenant_token, platform_token = tokens
    _tenant_id_ctx.reset(tenant_token)
    _platform_mode_ctx.reset(platform_token)


def current_tenant_id() -> int | None:
    return _tenant_id_ctx.get()


def current_platform_mode() -> bool:
    return bool(_platform_mode_ctx.get())


def get_current_tenant(request: Request) -> Tenant:
    tenant = getattr(getattr(request, "state", None), "tenant", None)
    if isinstance(tenant, Tenant):
        return tenant
    raise HTTPException(status_code=404, detail="Unknown tenant")


def require_tenant(request: Request) -> Tenant:
    if bool(getattr(getattr(request, "state", None), "platform_mode", False)):
        raise HTTPException(status_code=404, detail="Unknown tenant")
    return get_current_tenant(request)


def request_tenant_id(request: Request) -> int:
    tenant = require_tenant(request)
    return int(tenant.id)


def request_platform_mode(request: Request) -> bool:
    return bool(getattr(getattr(request, "state", None), "platform_mode", False))


def request_subdomain(request: Request) -> str:
    return str(getattr(getattr(request, "state", None), "request_subdomain", "") or "")


def is_tenant_scoped_entity(entity: Any) -> bool:
    table_name = str(getattr(entity, "__tablename__", "") or "").strip().lower()
    if not table_name:
        return False
    if table_name in {"tenants", "audit_events", "ewc_codes", "ewc_import_logs"}:
        return False
    return hasattr(entity, "tenant_id")
