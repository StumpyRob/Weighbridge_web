from __future__ import annotations

from datetime import datetime, timezone
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Tenant

DEFAULT_TENANT_NAME = "Default"
DEFAULT_TENANT_SUBDOMAIN = "default"
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


def ensure_default_tenant(db: Session) -> Tenant:
    tenant = get_tenant_by_subdomain(db, settings.effective_default_tenant_subdomain)
    if tenant is not None:
        if not bool(tenant.is_active):
            tenant.is_active = True
            db.flush()
        return tenant

    tenant = Tenant(
        name=DEFAULT_TENANT_NAME,
        subdomain=settings.effective_default_tenant_subdomain or DEFAULT_TENANT_SUBDOMAIN,
        is_active=True,
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(tenant)
    db.flush()
    return tenant
