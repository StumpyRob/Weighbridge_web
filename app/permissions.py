from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from fastapi import HTTPException, Request

from .models import User
from .user_roles import (
    ROLE_ACCOUNTS,
    ROLE_OPERATOR,
    ROLE_READ_ONLY,
    ROLE_SUPERADMIN,
    ROLE_TENANT_ADMIN,
    normalize_role,
)

PERM_ACCESS_WORKSPACE = "access_workspace"
PERM_MANAGE_USERS = "manage_users"
PERM_MANAGE_SETTINGS = "manage_settings"
PERM_MANAGE_TICKETS = "manage_tickets"
PERM_COMPLETE_TICKETS = "complete_tickets"
PERM_VIEW_INVOICES = "view_invoices"
PERM_MARK_INVOICES_PAID = "mark_invoices_paid"
PERM_USE_AI_ASSISTANT = "use_ai_assistant"
PERM_VIEW_AI_INSIGHTS = "view_ai_insights"

# Compatibility aliases for existing routes/templates. The effective
# permission model stays small even if older call sites still use these names.
PERM_VIEW_TICKETS = PERM_ACCESS_WORKSPACE
PERM_VOID_TICKETS = PERM_MANAGE_SETTINGS
PERM_VIEW_CUSTOMERS = PERM_ACCESS_WORKSPACE
PERM_MANAGE_CUSTOMERS = PERM_MANAGE_SETTINGS
PERM_VIEW_VEHICLES = PERM_ACCESS_WORKSPACE
PERM_MANAGE_VEHICLES = PERM_MANAGE_SETTINGS
PERM_VIEW_PRODUCTS = PERM_ACCESS_WORKSPACE
PERM_MANAGE_PRODUCTS = PERM_MANAGE_SETTINGS
PERM_VIEW_LOOKUPS = PERM_ACCESS_WORKSPACE
PERM_MANAGE_LOOKUPS = PERM_MANAGE_SETTINGS
PERM_GENERATE_INVOICES = PERM_MANAGE_SETTINGS
PERM_VOID_INVOICES = PERM_MANAGE_SETTINGS

_ADMIN_PERMISSIONS = {
    PERM_ACCESS_WORKSPACE,
    PERM_MANAGE_USERS,
    PERM_MANAGE_SETTINGS,
    PERM_MANAGE_TICKETS,
    PERM_COMPLETE_TICKETS,
    PERM_VIEW_INVOICES,
    PERM_MARK_INVOICES_PAID,
    PERM_USE_AI_ASSISTANT,
    PERM_VIEW_AI_INSIGHTS,
}

_ROLE_PERMISSIONS: dict[str, set[str]] = {
    ROLE_SUPERADMIN: set(_ADMIN_PERMISSIONS),
    ROLE_TENANT_ADMIN: set(_ADMIN_PERMISSIONS),
    ROLE_OPERATOR: {
        PERM_ACCESS_WORKSPACE,
        PERM_MANAGE_TICKETS,
        PERM_COMPLETE_TICKETS,
        PERM_USE_AI_ASSISTANT,
        PERM_VIEW_AI_INSIGHTS,
    },
    ROLE_ACCOUNTS: {
        PERM_ACCESS_WORKSPACE,
        PERM_VIEW_INVOICES,
        PERM_MARK_INVOICES_PAID,
        PERM_USE_AI_ASSISTANT,
        PERM_VIEW_AI_INSIGHTS,
    },
    ROLE_READ_ONLY: {
        PERM_ACCESS_WORKSPACE,
        PERM_VIEW_INVOICES,
        PERM_VIEW_AI_INSIGHTS,
    },
}


@dataclass(frozen=True)
class PermissionState:
    access_workspace: bool
    manage_users: bool
    manage_settings: bool
    manage_tickets: bool
    complete_tickets: bool
    view_invoices: bool
    mark_invoices_paid: bool
    use_ai_assistant: bool
    view_ai_insights: bool

    _ALIASES: ClassVar[dict[str, str]] = {
        "view_dashboard": "access_workspace",
        "view_reports": "access_workspace",
        "view_tickets": "access_workspace",
        "void_tickets": "manage_settings",
        "view_customers": "access_workspace",
        "manage_customers": "manage_settings",
        "view_vehicles": "access_workspace",
        "manage_vehicles": "manage_settings",
        "view_products": "access_workspace",
        "manage_products": "manage_settings",
        "view_lookups": "access_workspace",
        "manage_lookups": "manage_settings",
        "generate_invoices": "manage_settings",
        "void_invoices": "manage_settings",
    }

    def __getattr__(self, name: str) -> bool:
        alias = self._ALIASES.get(name)
        if alias is None:
            raise AttributeError(name)
        return bool(getattr(self, alias))


def _normalized_active_role(user: User | None) -> str | None:
    if user is None or not bool(getattr(user, "is_active", False)):
        return None
    return normalize_role(getattr(user, "role", None), default=None)


def has_permission(user: User | None, permission: str) -> bool:
    role = _normalized_active_role(user)
    if role is None:
        return False
    return permission in _ROLE_PERMISSIONS.get(role, set())


def _permission_state(user: User | None) -> PermissionState:
    return PermissionState(
        access_workspace=has_permission(user, PERM_ACCESS_WORKSPACE),
        manage_users=has_permission(user, PERM_MANAGE_USERS),
        manage_settings=has_permission(user, PERM_MANAGE_SETTINGS),
        manage_tickets=has_permission(user, PERM_MANAGE_TICKETS),
        complete_tickets=has_permission(user, PERM_COMPLETE_TICKETS),
        view_invoices=has_permission(user, PERM_VIEW_INVOICES),
        mark_invoices_paid=has_permission(user, PERM_MARK_INVOICES_PAID),
        use_ai_assistant=has_permission(user, PERM_USE_AI_ASSISTANT),
        view_ai_insights=has_permission(user, PERM_VIEW_AI_INSIGHTS),
    )


def _request_user(request: Request) -> User | None:
    current_user = getattr(getattr(request, "state", None), "current_user", None)
    if not isinstance(current_user, User):
        return None
    return current_user


def permission_context_for_request(request: Request) -> PermissionState:
    return _permission_state(_request_user(request))


def require_permission(request: Request, permission: str) -> User:
    current_user = _request_user(request)
    if current_user is None or not has_permission(current_user, permission):
        raise HTTPException(status_code=403, detail="Forbidden")
    return current_user


def require_any_permission(request: Request, *permissions: str) -> User:
    current_user = _request_user(request)
    if current_user is None:
        raise HTTPException(status_code=403, detail="Forbidden")
    if any(has_permission(current_user, permission) for permission in permissions):
        return current_user
    raise HTTPException(status_code=403, detail="Forbidden")
