from __future__ import annotations

ROLE_SUPERADMIN = "superadmin"
ROLE_TENANT_ADMIN = "tenant_admin"
ROLE_OPERATOR = "operator"
ROLE_ACCOUNTS = "accounts"
ROLE_READ_ONLY = "read_only"

# Backward compatibility for older code/tests that still reference the legacy user role.
ROLE_USER = ROLE_OPERATOR

VALID_ROLES: tuple[str, ...] = (
    ROLE_SUPERADMIN,
    ROLE_TENANT_ADMIN,
    ROLE_OPERATOR,
    ROLE_ACCOUNTS,
    ROLE_READ_ONLY,
)

_ROLE_ALIASES = {
    "superadmin": ROLE_SUPERADMIN,
    "admin": ROLE_TENANT_ADMIN,
    "tenant_admin": ROLE_TENANT_ADMIN,
    "operator": ROLE_OPERATOR,
    "user": ROLE_OPERATOR,
    "accounts": ROLE_ACCOUNTS,
    "read_only": ROLE_READ_ONLY,
    "readonly": ROLE_READ_ONLY,
}

ROLE_LABELS: dict[str, str] = {
    ROLE_SUPERADMIN: "Superadmin",
    ROLE_TENANT_ADMIN: "Tenant Admin",
    ROLE_OPERATOR: "Operator",
    ROLE_ACCOUNTS: "Accounts",
    ROLE_READ_ONLY: "Read Only",
}


def normalize_role(value: str | None, *, default: str | None = None) -> str | None:
    raw = str(value or "").strip().lower()
    if not raw:
        return default
    return _ROLE_ALIASES.get(raw, default)


def role_label(value: str | None) -> str:
    normalized = normalize_role(value, default=None)
    if normalized is None:
        return "Unknown"
    return ROLE_LABELS.get(normalized, normalized.replace("_", " ").title())


def legacy_role_for_tenant_id(tenant_id: object) -> str:
    return ROLE_SUPERADMIN if tenant_id is None else ROLE_TENANT_ADMIN
