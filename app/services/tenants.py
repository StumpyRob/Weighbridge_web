from __future__ import annotations

from datetime import datetime, timezone
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Tenant

DEMO_TENANT_NAME = "Demo"
DEMO_TENANT_SUBDOMAIN = "demo"
LEGACY_DEFAULT_TENANT_SUBDOMAIN = "default"
_LEGACY_DEFAULT_TENANT_NAMES = {"", "default", "default tenant"}
TENANT_SUBDOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def normalize_subdomain(raw: str | None) -> str:
    candidate = str(raw or "").strip().lower()
    return candidate


def validate_subdomain(raw: str | None) -> tuple[str, str | None]:
    candidate = normalize_subdomain(raw)
    if not candidate:
        return "", "Subdomain is required."
    if not TENANT_SUBDOMAIN_RE.fullmatch(candidate):
        return "", "Subdomain must be DNS-safe lowercase letters, numbers, and hyphens."
    if candidate in settings.effective_reserved_subdomains:
        return "", "Subdomain is reserved."
    return candidate, None


def get_tenant_by_subdomain(db: Session, subdomain: str | None) -> Tenant | None:
    normalized = normalize_subdomain(subdomain)
    if not normalized:
        return None
    return (
        db.execute(
            select(Tenant).where(func.lower(Tenant.subdomain) == normalized).limit(1)
        )
        .scalars()
        .first()
    )


def _should_rename_to_demo(name: str | None) -> bool:
    return str(name or "").strip().lower() in _LEGACY_DEFAULT_TENANT_NAMES


def ensure_demo_tenant(db: Session, *, create_missing: bool = True) -> Tenant | None:
    demo_subdomain = settings.effective_demo_tenant_subdomain or DEMO_TENANT_SUBDOMAIN
    tenant = get_tenant_by_subdomain(db, demo_subdomain)
    if tenant is not None:
        if _should_rename_to_demo(getattr(tenant, "name", None)):
            tenant.name = DEMO_TENANT_NAME
        if not bool(tenant.is_active):
            tenant.is_active = True
        return tenant

    legacy_tenant = get_tenant_by_subdomain(db, LEGACY_DEFAULT_TENANT_SUBDOMAIN)
    if legacy_tenant is not None:
        legacy_tenant.subdomain = demo_subdomain
        if _should_rename_to_demo(getattr(legacy_tenant, "name", None)):
            legacy_tenant.name = DEMO_TENANT_NAME
        if not bool(legacy_tenant.is_active):
            legacy_tenant.is_active = True
        return legacy_tenant

    if not create_missing:
        return None

    tenant = Tenant(
        name=DEMO_TENANT_NAME,
        subdomain=demo_subdomain,
        is_active=True,
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(tenant)
    db.flush()
    return tenant


def ensure_default_tenant(db: Session) -> Tenant | None:
    return ensure_demo_tenant(db)
