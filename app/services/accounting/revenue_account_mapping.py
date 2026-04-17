from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import AccountingRevenueAccountMap
from .jobs import get_active_accounting_connection
from .quickbooks_client import QuickBooksApiError, quickbooks_client_for_connection
from .quickbooks_oauth import QUICKBOOKS_PROVIDER

if TYPE_CHECKING:
    from .quickbooks_client import QuickBooksClient, QuickBooksRevenueAccount


REVENUE_ACCOUNT_SCOPE_GLOBAL_DEFAULT = "global_default"
REVENUE_ACCOUNT_SCOPE_PRODUCT = "product"
REVENUE_ACCOUNT_SCOPE_PRODUCT_GROUP = "product_group"


class RevenueAccountMappingValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ProviderRevenueAccount:
    remote_account_id: str
    remote_account_code: str | None
    remote_account_name: str
    remote_account_type: str | None
    remote_account_detail_type: str | None
    is_active: bool
    is_usable: bool
    display_label: str


@dataclass(frozen=True)
class ResolvedRevenueAccount:
    remote_account_id: str
    remote_account_code: str | None
    remote_account_name: str
    remote_account_type: str | None
    remote_account_detail_type: str | None
    resolution_source: str
    mapping_id: int | None
    nominal_code: str | None


def _normalize_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _display_label(*, code: str | None, name: str, account_type: str | None, detail_type: str | None) -> str:
    label = f"{code} - {name}" if code else name
    type_bits = [bit for bit in (account_type, detail_type) if bit]
    if type_bits:
        label = f"{label} ({' / '.join(type_bits)})"
    return label


def _provider_revenue_account(account: "QuickBooksRevenueAccount") -> ProviderRevenueAccount:
    name = str(account.remote_account_name or "").strip() or f"Account {account.remote_account_id}"
    code = _normalize_text(account.remote_account_code)
    account_type = _normalize_text(account.remote_account_type)
    detail_type = _normalize_text(account.remote_account_detail_type)
    return ProviderRevenueAccount(
        remote_account_id=str(account.remote_account_id),
        remote_account_code=code,
        remote_account_name=name,
        remote_account_type=account_type,
        remote_account_detail_type=detail_type,
        is_active=bool(account.is_active),
        is_usable=bool(account.is_usable),
        display_label=_display_label(
            code=code,
            name=name,
            account_type=account_type,
            detail_type=detail_type,
        ),
    )


def _connected_quickbooks_client(
    db: Session,
    *,
    tenant_id: int,
    provider: str,
) -> "QuickBooksClient":
    connection = get_active_accounting_connection(
        db,
        int(tenant_id),
        provider=provider,
    )
    if connection is None:
        raise RevenueAccountMappingValidationError(
            "QuickBooks connection is not active for this tenant."
        )
    return quickbooks_client_for_connection(db, connection)


def list_quickbooks_revenue_accounts(
    db: Session,
    *,
    tenant_id: int,
    provider: str = QUICKBOOKS_PROVIDER,
) -> list[ProviderRevenueAccount]:
    client = _connected_quickbooks_client(
        db,
        tenant_id=int(tenant_id),
        provider=provider,
    )
    return [_provider_revenue_account(account) for account in client.list_revenue_accounts()]


def list_provider_revenue_accounts(
    db: Session,
    *,
    tenant_id: int,
    provider: str = QUICKBOOKS_PROVIDER,
) -> list[ProviderRevenueAccount]:
    resolved_provider = str(provider or "").strip().lower()
    if resolved_provider == QUICKBOOKS_PROVIDER:
        return list_quickbooks_revenue_accounts(
            db,
            tenant_id=int(tenant_id),
            provider=resolved_provider,
        )
    raise RevenueAccountMappingValidationError("Unsupported accounting provider.")


def get_default_revenue_account_mapping(
    db: Session,
    *,
    tenant_id: int,
    provider: str = QUICKBOOKS_PROVIDER,
    active_only: bool = True,
) -> AccountingRevenueAccountMap | None:
    statement = select(AccountingRevenueAccountMap).where(
        AccountingRevenueAccountMap.tenant_id == int(tenant_id),
        AccountingRevenueAccountMap.provider == str(provider or "").strip().lower(),
        AccountingRevenueAccountMap.local_scope_type == REVENUE_ACCOUNT_SCOPE_GLOBAL_DEFAULT,
        AccountingRevenueAccountMap.local_scope_id.is_(None),
        AccountingRevenueAccountMap.local_nominal_code.is_(None),
    )
    if active_only:
        statement = statement.where(AccountingRevenueAccountMap.is_active.is_(True))
    return db.execute(statement.order_by(AccountingRevenueAccountMap.id.asc())).scalars().first()


def save_default_revenue_account_mapping(
    db: Session,
    *,
    tenant_id: int,
    provider: str = QUICKBOOKS_PROVIDER,
    remote_account_id: str,
) -> AccountingRevenueAccountMap:
    requested_account_id = str(remote_account_id or "").strip()
    if not requested_account_id:
        raise RevenueAccountMappingValidationError("A default revenue account is required.")

    available_accounts = list_provider_revenue_accounts(
        db,
        tenant_id=int(tenant_id),
        provider=provider,
    )
    selected_account = next(
        (
            account
            for account in available_accounts
            if str(account.remote_account_id) == requested_account_id and account.is_usable
        ),
        None,
    )
    if selected_account is None:
        raise RevenueAccountMappingValidationError(
            "The selected QuickBooks revenue account was not found."
        )

    mapping = get_default_revenue_account_mapping(
        db,
        tenant_id=int(tenant_id),
        provider=provider,
        active_only=False,
    )
    if mapping is None:
        mapping = AccountingRevenueAccountMap(
            tenant_id=int(tenant_id),
            provider=str(provider or "").strip().lower(),
            local_scope_type=REVENUE_ACCOUNT_SCOPE_GLOBAL_DEFAULT,
            local_scope_id=None,
            local_nominal_code=None,
            remote_account_id=selected_account.remote_account_id,
            remote_account_name=selected_account.remote_account_name,
            is_active=True,
        )
        db.add(mapping)

    mapping.provider = str(provider or "").strip().lower()
    mapping.local_scope_type = REVENUE_ACCOUNT_SCOPE_GLOBAL_DEFAULT
    mapping.local_scope_id = None
    mapping.local_nominal_code = None
    mapping.remote_account_id = selected_account.remote_account_id
    mapping.remote_account_code = selected_account.remote_account_code
    mapping.remote_account_name = selected_account.remote_account_name
    mapping.remote_account_type = selected_account.remote_account_type
    mapping.is_active = True
    db.flush()
    return mapping


def clear_default_revenue_account_mapping(
    db: Session,
    *,
    tenant_id: int,
    provider: str = QUICKBOOKS_PROVIDER,
) -> bool:
    mapping = get_default_revenue_account_mapping(
        db,
        tenant_id=int(tenant_id),
        provider=provider,
        active_only=False,
    )
    if mapping is None:
        return False
    db.delete(mapping)
    db.flush()
    return True


def resolve_revenue_account(
    db: Session,
    *,
    tenant_id: int,
    provider: str,
    product_label: str,
    nominal_code: str | None,
    client: "QuickBooksClient",
) -> ResolvedRevenueAccount:
    mapping = get_default_revenue_account_mapping(
        db,
        tenant_id=int(tenant_id),
        provider=provider,
    )
    if mapping is not None:
        try:
            account = client.resolve_income_account_by_id(
                remote_account_id=str(mapping.remote_account_id or "").strip()
            )
        except QuickBooksApiError as exc:
            raise QuickBooksApiError(
                f"Default QuickBooks revenue account mapping {mapping.remote_account_name or mapping.remote_account_id} is not usable: {exc}",
                detail_json={
                    "resolution_source": "global_default_mapping",
                    "mapping_id": int(mapping.id),
                    "remote_account_id": str(mapping.remote_account_id or "").strip() or None,
                    "remote_account_code": str(mapping.remote_account_code or "").strip() or None,
                },
            ) from exc
        return ResolvedRevenueAccount(
            remote_account_id=account.remote_account_id,
            remote_account_code=account.remote_account_code,
            remote_account_name=account.remote_account_name,
            remote_account_type=account.remote_account_type,
            remote_account_detail_type=account.remote_account_detail_type,
            resolution_source="global_default_mapping",
            mapping_id=int(mapping.id),
            nominal_code=_normalize_text(nominal_code),
        )

    normalized_nominal_code = _normalize_text(nominal_code)
    if normalized_nominal_code:
        account = client.resolve_income_account_by_nominal_code(
            nominal_code=normalized_nominal_code
        )
        return ResolvedRevenueAccount(
            remote_account_id=account.remote_account_id,
            remote_account_code=account.remote_account_code,
            remote_account_name=account.remote_account_name,
            remote_account_type=account.remote_account_type,
            remote_account_detail_type=account.remote_account_detail_type,
            resolution_source="nominal_code_fallback",
            mapping_id=None,
            nominal_code=normalized_nominal_code,
        )

    raise QuickBooksApiError(
        f"No default revenue account is selected and Product {product_label} has no nominal code fallback, so its QuickBooks income account cannot be resolved safely.",
        detail_json={
            "resolution_source": "unresolved",
            "product_label": str(product_label or "").strip() or None,
        },
    )
