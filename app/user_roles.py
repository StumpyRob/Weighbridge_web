from __future__ import annotations

ROLE_SUPERADMIN = "superadmin"
ROLE_TENANT_ADMIN = "tenant_admin"
ROLE_USER = "user"

VALID_ROLES: tuple[str, ...] = (
    ROLE_SUPERADMIN,
    ROLE_TENANT_ADMIN,
    ROLE_USER,
)

_ROLE_ALIASES = {
    "superadmin": ROLE_SUPERADMIN,
    "admin": ROLE_TENANT_ADMIN,
    "tenant_admin": ROLE_TENANT_ADMIN,
    "operator": ROLE_USER,
    "user": ROLE_USER,
}


def normalize_role(value: str | None, *, default: str | None = None) -> str | None:
    raw = str(value or "").strip().lower()
    if not raw:
        return default
    return _ROLE_ALIASES.get(raw, default)


def legacy_role_for_tenant_id(tenant_id: object) -> str:
    return ROLE_SUPERADMIN if tenant_id is None else ROLE_TENANT_ADMIN
