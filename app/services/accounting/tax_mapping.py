from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...models import AccountingSyncJob, AccountingTaxMap, Product, TaxRate
from ..pricing import product_effective_nominal_code
from .quickbooks_client import QuickBooksApiError
from .quickbooks_oauth import QUICKBOOKS_PROVIDER

_PSEUDO_LINE_CODES = {"TAX", "NON"}


class TaxMappingValidationError(ValueError):
    pass


def _normalize_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _normalize_external_code(value: object) -> str | None:
    text = _normalize_text(value)
    if text is None:
        return None
    upper_text = text.upper()
    if upper_text in _PSEUDO_LINE_CODES:
        return upper_text
    return text


def _local_rate_percent(tax_rate: TaxRate | None) -> Decimal:
    if tax_rate is None or tax_rate.rate_percent in (None, ""):
        return Decimal("0")
    return Decimal(str(tax_rate.rate_percent))


def _display_rate_percent(tax_rate: TaxRate | None) -> str | None:
    if tax_rate is None or tax_rate.rate_percent in (None, ""):
        return None
    return str(tax_rate.rate_percent)


def _local_tax_label(tax_rate: TaxRate | None) -> str:
    if tax_rate is None:
        return "Unknown tax rate"
    code = _normalize_text(tax_rate.code)
    description = _normalize_text(tax_rate.description)
    if code and description and description != code:
        return f"{code} ({description})"
    return code or description or f"Tax rate {tax_rate.id}"


@dataclass(frozen=True)
class QuickBooksTaxSelection:
    tax_rate_id: int
    tax_map_id: int
    local_tax_label: str
    local_rate_percent: Decimal
    line_tax_code_ref: str
    txn_tax_code_ref: str | None
    display_label: str | None
    is_taxable: bool
    uses_pseudo_line_code: bool


@dataclass(frozen=True)
class TaxMappingAdminRow:
    tax_rate_id: int
    tax_rate_code: str
    tax_rate_description: str | None
    rate_percent: str | None
    product_count: int
    is_required: bool
    mapping_id: int | None
    external_id: str | None
    external_code: str | None
    mapping_name: str | None
    is_active: bool
    is_usable: bool
    status_label: str
    status_detail: str | None


@dataclass(frozen=True)
class QuickBooksSetupSummary:
    tax_mapping_rows: list[TaxMappingAdminRow]
    required_tax_rate_count: int
    usable_required_tax_mapping_count: int
    missing_tax_mapping_count: int
    products_missing_tax_rate: int
    products_missing_nominal_code: int
    pending_job_count: int
    failed_job_count: int
    required_tax_mappings_complete: bool
    is_ready_for_sandbox: bool
    next_steps: list[str]


@dataclass(frozen=True)
class _ResolvedQuickBooksTaxConfig:
    local_tax_label: str
    local_rate_percent: Decimal
    is_taxable: bool
    line_tax_code_ref: str | None
    txn_tax_code_ref: str | None
    uses_pseudo_line_code: bool
    issue: str | None


@dataclass(frozen=True)
class _QuickBooksTaxMapAssessment:
    config: _ResolvedQuickBooksTaxConfig
    display_label: str | None
    is_present: bool
    is_active: bool
    is_usable: bool
    status_label: str
    status_detail: str | None


def _resolve_quickbooks_tax_configuration(
    tax_rate: TaxRate | None,
    *,
    external_id: str | None,
    external_code: str | None,
) -> _ResolvedQuickBooksTaxConfig:
    local_label = _local_tax_label(tax_rate)
    local_rate_percent = _local_rate_percent(tax_rate)
    is_taxable = local_rate_percent > Decimal("0")
    normalized_external_id = _normalize_text(external_id)
    normalized_external_code = _normalize_external_code(external_code)
    if not normalized_external_id and not normalized_external_code:
        return _ResolvedQuickBooksTaxConfig(
            local_tax_label=local_label,
            local_rate_percent=local_rate_percent,
            is_taxable=is_taxable,
            line_tax_code_ref=None,
            txn_tax_code_ref=None,
            uses_pseudo_line_code=False,
            issue="QuickBooks external ID or external code is required.",
        )

    line_ref = normalized_external_code or normalized_external_id
    assert line_ref is not None
    normalized_line_ref = line_ref.upper()
    uses_pseudo_line_code = normalized_line_ref in _PSEUDO_LINE_CODES
    txn_tax_code_ref: str | None = None
    issue: str | None = None

    if uses_pseudo_line_code:
        if normalized_line_ref == "NON" and is_taxable:
            issue = f"Local tax rate {local_label} is taxable and cannot map to NON."
        elif normalized_line_ref == "TAX":
            txn_tax_code_ref = (
                normalized_external_id
                if normalized_external_id and normalized_external_id.upper() != "TAX"
                else None
            )
            if not txn_tax_code_ref:
                issue = (
                    "QuickBooks TAX mappings require an invoice tax code/group external ID."
                )
            elif not is_taxable:
                issue = f"Local tax rate {local_label} is non-taxable and cannot map to TAX."
        line_ref = normalized_line_ref

    return _ResolvedQuickBooksTaxConfig(
        local_tax_label=local_label,
        local_rate_percent=local_rate_percent,
        is_taxable=is_taxable,
        line_tax_code_ref=line_ref,
        txn_tax_code_ref=txn_tax_code_ref,
        uses_pseudo_line_code=uses_pseudo_line_code,
        issue=issue,
    )


def _assess_quickbooks_tax_mapping(
    tax_rate: TaxRate | None,
    tax_map: AccountingTaxMap | None,
) -> _QuickBooksTaxMapAssessment:
    display_label = _normalize_text(getattr(tax_map, "name", None)) or _local_tax_label(tax_rate)
    if tax_map is None:
        config = _resolve_quickbooks_tax_configuration(
            tax_rate,
            external_id=None,
            external_code=None,
        )
        return _QuickBooksTaxMapAssessment(
            config=config,
            display_label=display_label,
            is_present=False,
            is_active=False,
            is_usable=False,
            status_label="Missing",
            status_detail="No QuickBooks mapping is saved for this local tax rate.",
        )

    config = _resolve_quickbooks_tax_configuration(
        tax_rate,
        external_id=_normalize_text(tax_map.external_id),
        external_code=_normalize_text(tax_map.external_code),
    )
    is_active = bool(tax_map.is_active)
    if config.issue:
        return _QuickBooksTaxMapAssessment(
            config=config,
            display_label=display_label,
            is_present=True,
            is_active=is_active,
            is_usable=False,
            status_label="Invalid",
            status_detail=config.issue,
        )
    if not is_active:
        return _QuickBooksTaxMapAssessment(
            config=config,
            display_label=display_label,
            is_present=True,
            is_active=False,
            is_usable=False,
            status_label="Inactive",
            status_detail="Saved but inactive.",
        )
    return _QuickBooksTaxMapAssessment(
        config=config,
        display_label=display_label,
        is_present=True,
        is_active=True,
        is_usable=True,
        status_label="Ready",
        status_detail=None,
    )


def accounting_tax_map_for_rate(
    db: Session,
    *,
    tenant_id: int,
    provider: str,
    tax_rate_id: int,
) -> tuple[AccountingTaxMap | None, TaxRate | None]:
    row = (
        db.execute(
            select(AccountingTaxMap, TaxRate)
            .outerjoin(TaxRate, TaxRate.id == AccountingTaxMap.tax_rate_id)
            .where(
                AccountingTaxMap.tenant_id == int(tenant_id),
                AccountingTaxMap.provider == str(provider or "").strip().lower(),
                AccountingTaxMap.tax_rate_id == int(tax_rate_id),
            )
        )
        .first()
    )
    if row is None:
        tax_rate = db.get(TaxRate, int(tax_rate_id))
        return None, tax_rate
    tax_map, tax_rate = row
    return tax_map, tax_rate


def require_quickbooks_tax_selection(
    db: Session,
    *,
    tenant_id: int,
    provider: str,
    tax_rate_id: int,
    usage_label: str,
) -> QuickBooksTaxSelection:
    tax_map, tax_rate = accounting_tax_map_for_rate(
        db,
        tenant_id=int(tenant_id),
        provider=provider,
        tax_rate_id=int(tax_rate_id),
    )
    assessment = _assess_quickbooks_tax_mapping(tax_rate, tax_map)
    if not assessment.is_present:
        raise QuickBooksApiError(
            f"{usage_label} uses local tax rate {assessment.config.local_tax_label} with no QuickBooks tax mapping."
        )
    if not assessment.is_active:
        raise QuickBooksApiError(
            f"{usage_label} uses local tax rate {assessment.config.local_tax_label}, but its QuickBooks tax mapping is inactive."
        )
    if not assessment.is_usable:
        raise QuickBooksApiError(
            f"{usage_label} uses local tax rate {assessment.config.local_tax_label}, but its QuickBooks tax mapping is invalid: {assessment.status_detail}"
        )
    assert tax_map is not None
    assert assessment.config.line_tax_code_ref is not None
    return QuickBooksTaxSelection(
        tax_rate_id=int(tax_map.tax_rate_id),
        tax_map_id=int(tax_map.id),
        local_tax_label=assessment.config.local_tax_label,
        local_rate_percent=assessment.config.local_rate_percent,
        line_tax_code_ref=assessment.config.line_tax_code_ref,
        txn_tax_code_ref=assessment.config.txn_tax_code_ref,
        display_label=assessment.display_label,
        is_taxable=assessment.config.is_taxable,
        uses_pseudo_line_code=assessment.config.uses_pseudo_line_code,
    )


def required_local_tax_rate_ids(
    db: Session,
    *,
    tenant_id: int,
) -> set[int]:
    rows = db.execute(
        select(Product.tax_rate_id)
        .where(
            Product.tenant_id == int(tenant_id),
            Product.tax_rate_id.is_not(None),
        )
        .distinct()
    ).all()
    return {
        int(tax_rate_id)
        for (tax_rate_id,) in rows
        if tax_rate_id not in (None, "")
    }


def list_quickbooks_tax_mapping_rows(
    db: Session,
    *,
    tenant_id: int,
    provider: str = QUICKBOOKS_PROVIDER,
) -> list[TaxMappingAdminRow]:
    used_rate_rows = db.execute(
        select(
            TaxRate.id,
            TaxRate.code,
            TaxRate.description,
            TaxRate.rate_percent,
            func.count(Product.id),
        )
        .join(Product, Product.tax_rate_id == TaxRate.id)
        .where(Product.tenant_id == int(tenant_id))
        .group_by(
            TaxRate.id,
            TaxRate.code,
            TaxRate.description,
            TaxRate.rate_percent,
        )
    ).all()
    used_rate_meta = {
        int(tax_rate_id): {
            "tax_rate_id": int(tax_rate_id),
            "tax_rate_code": str(tax_rate_code or "").strip(),
            "tax_rate_description": _normalize_text(tax_rate_description),
            "rate_percent": str(rate_percent) if rate_percent is not None else None,
            "product_count": int(product_count or 0),
        }
        for tax_rate_id, tax_rate_code, tax_rate_description, rate_percent, product_count in used_rate_rows
    }

    mapping_rows = db.execute(
        select(AccountingTaxMap, TaxRate)
        .join(TaxRate, TaxRate.id == AccountingTaxMap.tax_rate_id)
        .where(
            AccountingTaxMap.tenant_id == int(tenant_id),
            AccountingTaxMap.provider == str(provider or "").strip().lower(),
        )
    ).all()
    tax_maps_by_rate_id = {
        int(tax_map.tax_rate_id): (tax_map, tax_rate)
        for tax_map, tax_rate in mapping_rows
    }

    all_rate_ids = sorted(
        set(used_rate_meta.keys()) | {int(tax_map.tax_rate_id) for tax_map, _tax_rate in mapping_rows},
        key=lambda rate_id: (
            0 if rate_id in used_rate_meta else 1,
            str(
                used_rate_meta.get(rate_id, {}).get("tax_rate_code")
                or getattr(tax_maps_by_rate_id.get(rate_id, (None, None))[1], "code", "")
                or ""
            ),
            rate_id,
        ),
    )

    rows: list[TaxMappingAdminRow] = []
    for tax_rate_id in all_rate_ids:
        tax_map, tax_rate = tax_maps_by_rate_id.get(tax_rate_id, (None, None))
        if tax_rate is None:
            tax_rate = db.get(TaxRate, int(tax_rate_id))
        assessment = _assess_quickbooks_tax_mapping(tax_rate, tax_map)
        used_meta = used_rate_meta.get(
            tax_rate_id,
            {
                "tax_rate_id": int(tax_rate_id),
                "tax_rate_code": _normalize_text(getattr(tax_rate, "code", None)) or f"Tax rate {tax_rate_id}",
                "tax_rate_description": _normalize_text(getattr(tax_rate, "description", None)),
                "rate_percent": _display_rate_percent(tax_rate),
                "product_count": 0,
            },
        )
        rows.append(
            TaxMappingAdminRow(
                tax_rate_id=int(used_meta["tax_rate_id"]),
                tax_rate_code=str(used_meta["tax_rate_code"]),
                tax_rate_description=used_meta["tax_rate_description"],
                rate_percent=used_meta["rate_percent"],
                product_count=int(used_meta["product_count"]),
                is_required=int(used_meta["product_count"]) > 0,
                mapping_id=int(tax_map.id) if tax_map is not None else None,
                external_id=_normalize_text(getattr(tax_map, "external_id", None)),
                external_code=_normalize_text(getattr(tax_map, "external_code", None)),
                mapping_name=_normalize_text(getattr(tax_map, "name", None)),
                is_active=bool(getattr(tax_map, "is_active", False)) if tax_map else False,
                is_usable=assessment.is_usable,
                status_label=assessment.status_label,
                status_detail=assessment.status_detail,
            )
        )
    return rows


def _job_counts(
    db: Session,
    *,
    tenant_id: int,
    provider: str,
) -> tuple[int, int]:
    counts = {
        str(status or "").strip().lower(): int(count or 0)
        for status, count in db.execute(
            select(
                AccountingSyncJob.status,
                func.count(AccountingSyncJob.id),
            )
            .where(
                AccountingSyncJob.tenant_id == int(tenant_id),
                AccountingSyncJob.provider == str(provider or "").strip().lower(),
            )
            .group_by(AccountingSyncJob.status)
        ).all()
    }
    pending_job_count = counts.get("pending", 0) + counts.get("running", 0)
    failed_job_count = counts.get("failed", 0)
    return pending_job_count, failed_job_count


def _setup_guidance(
    *,
    connection_status: str | None,
    missing_tax_mapping_count: int,
    products_missing_tax_rate: int,
    products_missing_nominal_code: int,
    pending_job_count: int,
    failed_job_count: int,
    is_ready_for_sandbox: bool,
) -> list[str]:
    steps: list[str] = []
    if str(connection_status or "").strip().lower() != "connected":
        steps.append("Connect this tenant to the QuickBooks sandbox company before running sync jobs.")
    if missing_tax_mapping_count:
        steps.append(
            f"Create usable QuickBooks mappings for {missing_tax_mapping_count} required local tax rate(s)."
        )
    if products_missing_tax_rate:
        steps.append(
            f"Update {products_missing_tax_rate} product(s) so each has a local tax rate."
        )
    if products_missing_nominal_code:
        steps.append(
            f"Update {products_missing_nominal_code} product(s) so each has a nominal code that matches a QuickBooks income account AcctNum."
        )
    if failed_job_count:
        steps.append(
            f"Review or retry {failed_job_count} failed sync job(s) after fixing mappings and product setup."
        )
    elif pending_job_count:
        steps.append(
            f"Run the remaining {pending_job_count} pending sync job(s) once setup blockers are cleared."
        )
    if not steps and is_ready_for_sandbox:
        steps.append(
            "Setup looks ready for QuickBooks sandbox UAT. Review mappings once more, then run a small manual sync batch."
        )
    return steps


def summarize_quickbooks_setup(
    db: Session,
    *,
    tenant_id: int,
    connection_status: str | None,
    provider: str = QUICKBOOKS_PROVIDER,
) -> QuickBooksSetupSummary:
    tax_mapping_rows = list_quickbooks_tax_mapping_rows(
        db,
        tenant_id=int(tenant_id),
        provider=provider,
    )
    required_tax_rate_count = sum(1 for row in tax_mapping_rows if row.is_required)
    usable_required_tax_mapping_count = sum(
        1 for row in tax_mapping_rows if row.is_required and row.is_usable
    )
    missing_tax_mapping_count = required_tax_rate_count - usable_required_tax_mapping_count

    products = (
        db.execute(
            select(Product)
            .where(Product.tenant_id == int(tenant_id))
            .order_by(Product.code.asc(), Product.id.asc())
        )
        .scalars()
        .all()
    )
    products_missing_tax_rate = sum(1 for product in products if product.tax_rate_id is None)
    products_missing_nominal_code = sum(
        1 for product in products if not str(product_effective_nominal_code(product) or "").strip()
    )
    pending_job_count, failed_job_count = _job_counts(
        db,
        tenant_id=int(tenant_id),
        provider=provider,
    )
    required_tax_mappings_complete = missing_tax_mapping_count == 0
    is_ready_for_sandbox = (
        str(connection_status or "").strip().lower() == "connected"
        and required_tax_mappings_complete
        and products_missing_tax_rate == 0
        and products_missing_nominal_code == 0
    )
    next_steps = _setup_guidance(
        connection_status=connection_status,
        missing_tax_mapping_count=missing_tax_mapping_count,
        products_missing_tax_rate=products_missing_tax_rate,
        products_missing_nominal_code=products_missing_nominal_code,
        pending_job_count=pending_job_count,
        failed_job_count=failed_job_count,
        is_ready_for_sandbox=is_ready_for_sandbox,
    )
    return QuickBooksSetupSummary(
        tax_mapping_rows=tax_mapping_rows,
        required_tax_rate_count=required_tax_rate_count,
        usable_required_tax_mapping_count=usable_required_tax_mapping_count,
        missing_tax_mapping_count=missing_tax_mapping_count,
        products_missing_tax_rate=products_missing_tax_rate,
        products_missing_nominal_code=products_missing_nominal_code,
        pending_job_count=pending_job_count,
        failed_job_count=failed_job_count,
        required_tax_mappings_complete=required_tax_mappings_complete,
        is_ready_for_sandbox=is_ready_for_sandbox,
        next_steps=next_steps,
    )


def _required_tax_rate_for_create(
    db: Session,
    *,
    tenant_id: int,
    tax_rate_id: int,
) -> TaxRate:
    allowed_ids = required_local_tax_rate_ids(db, tenant_id=int(tenant_id))
    if int(tax_rate_id) not in allowed_ids:
        raise TaxMappingValidationError(
            "The selected local tax rate is not currently used by this tenant's products."
        )
    tax_rate = db.get(TaxRate, int(tax_rate_id))
    if tax_rate is None:
        raise TaxMappingValidationError("The selected local tax rate was not found.")
    return tax_rate


def _existing_quickbooks_tax_map(
    db: Session,
    *,
    tenant_id: int,
    provider: str,
    tax_rate_id: int,
) -> AccountingTaxMap | None:
    return (
        db.execute(
            select(AccountingTaxMap).where(
                AccountingTaxMap.tenant_id == int(tenant_id),
                AccountingTaxMap.provider == str(provider or "").strip().lower(),
                AccountingTaxMap.tax_rate_id == int(tax_rate_id),
            )
        )
        .scalars()
        .first()
    )


def _duplicate_external_id_owner(
    db: Session,
    *,
    tenant_id: int,
    provider: str,
    external_id: str | None,
    current_mapping_id: int | None,
) -> AccountingTaxMap | None:
    normalized_external_id = _normalize_text(external_id)
    if normalized_external_id is None:
        return None
    query = select(AccountingTaxMap).where(
        AccountingTaxMap.tenant_id == int(tenant_id),
        AccountingTaxMap.provider == str(provider or "").strip().lower(),
        AccountingTaxMap.external_id == normalized_external_id,
    )
    if current_mapping_id is not None:
        query = query.where(AccountingTaxMap.id != int(current_mapping_id))
    return db.execute(query).scalars().first()


def _duplicate_external_code_owner(
    db: Session,
    *,
    tenant_id: int,
    provider: str,
    external_code: str | None,
    current_mapping_id: int | None,
) -> AccountingTaxMap | None:
    normalized_external_code = _normalize_external_code(external_code)
    if normalized_external_code is None or normalized_external_code in _PSEUDO_LINE_CODES:
        return None
    query = select(AccountingTaxMap).where(
        AccountingTaxMap.tenant_id == int(tenant_id),
        AccountingTaxMap.provider == str(provider or "").strip().lower(),
        AccountingTaxMap.external_code == normalized_external_code,
    )
    if current_mapping_id is not None:
        query = query.where(AccountingTaxMap.id != int(current_mapping_id))
    return db.execute(query).scalars().first()


def create_quickbooks_tax_mapping(
    db: Session,
    *,
    tenant_id: int,
    tax_rate_id: int,
    external_id: str | None,
    external_code: str | None,
    name: str | None,
    is_active: bool,
    provider: str = QUICKBOOKS_PROVIDER,
) -> AccountingTaxMap:
    tax_rate = _required_tax_rate_for_create(
        db,
        tenant_id=int(tenant_id),
        tax_rate_id=int(tax_rate_id),
    )
    existing = _existing_quickbooks_tax_map(
        db,
        tenant_id=int(tenant_id),
        provider=provider,
        tax_rate_id=int(tax_rate_id),
    )
    if existing is not None:
        raise TaxMappingValidationError(
            "This local tax rate already has a QuickBooks tax mapping."
        )

    normalized_external_id = _normalize_text(external_id)
    normalized_external_code = _normalize_external_code(external_code)
    duplicate_external_id_owner = _duplicate_external_id_owner(
        db,
        tenant_id=int(tenant_id),
        provider=provider,
        external_id=normalized_external_id,
        current_mapping_id=None,
    )
    if duplicate_external_id_owner is not None:
        raise TaxMappingValidationError(
            "That QuickBooks external ID is already used by another local tax mapping."
        )
    duplicate_external_code_owner = _duplicate_external_code_owner(
        db,
        tenant_id=int(tenant_id),
        provider=provider,
        external_code=normalized_external_code,
        current_mapping_id=None,
    )
    if duplicate_external_code_owner is not None:
        raise TaxMappingValidationError(
            "That QuickBooks external code is already used by another local tax mapping."
        )

    config = _resolve_quickbooks_tax_configuration(
        tax_rate,
        external_id=normalized_external_id,
        external_code=normalized_external_code,
    )
    if config.issue:
        raise TaxMappingValidationError(config.issue)

    tax_map = AccountingTaxMap(
        tenant_id=int(tenant_id),
        provider=str(provider or "").strip().lower(),
        tax_rate_id=int(tax_rate.id),
        external_id=normalized_external_id,
        external_code=normalized_external_code,
        name=_normalize_text(name),
        is_active=bool(is_active),
    )
    db.add(tax_map)
    db.flush()
    return tax_map


def update_quickbooks_tax_mapping(
    db: Session,
    *,
    tenant_id: int,
    mapping_id: int,
    external_id: str | None,
    external_code: str | None,
    name: str | None,
    is_active: bool,
    provider: str = QUICKBOOKS_PROVIDER,
) -> AccountingTaxMap:
    tax_map = db.get(AccountingTaxMap, int(mapping_id))
    if tax_map is None or str(tax_map.provider or "").strip().lower() != str(provider or "").strip().lower():
        raise TaxMappingValidationError("QuickBooks tax mapping was not found.")
    if int(tax_map.tenant_id) != int(tenant_id):
        raise TaxMappingValidationError("QuickBooks tax mapping was not found.")

    tax_rate = db.get(TaxRate, int(tax_map.tax_rate_id))
    if tax_rate is None:
        raise TaxMappingValidationError("The linked local tax rate was not found.")

    normalized_external_id = _normalize_text(external_id)
    normalized_external_code = _normalize_external_code(external_code)
    duplicate_external_id_owner = _duplicate_external_id_owner(
        db,
        tenant_id=int(tenant_id),
        provider=provider,
        external_id=normalized_external_id,
        current_mapping_id=int(tax_map.id),
    )
    if duplicate_external_id_owner is not None:
        raise TaxMappingValidationError(
            "That QuickBooks external ID is already used by another local tax mapping."
        )
    duplicate_external_code_owner = _duplicate_external_code_owner(
        db,
        tenant_id=int(tenant_id),
        provider=provider,
        external_code=normalized_external_code,
        current_mapping_id=int(tax_map.id),
    )
    if duplicate_external_code_owner is not None:
        raise TaxMappingValidationError(
            "That QuickBooks external code is already used by another local tax mapping."
        )

    config = _resolve_quickbooks_tax_configuration(
        tax_rate,
        external_id=normalized_external_id,
        external_code=normalized_external_code,
    )
    if config.issue:
        raise TaxMappingValidationError(config.issue)

    tax_map.external_id = normalized_external_id
    tax_map.external_code = normalized_external_code
    tax_map.name = _normalize_text(name)
    tax_map.is_active = bool(is_active)
    db.flush()
    return tax_map


def delete_quickbooks_tax_mapping(
    db: Session,
    *,
    tenant_id: int,
    mapping_id: int,
    provider: str = QUICKBOOKS_PROVIDER,
) -> None:
    tax_map = db.get(AccountingTaxMap, int(mapping_id))
    if tax_map is None or str(tax_map.provider or "").strip().lower() != str(provider or "").strip().lower():
        raise TaxMappingValidationError("QuickBooks tax mapping was not found.")
    if int(tax_map.tenant_id) != int(tenant_id):
        raise TaxMappingValidationError("QuickBooks tax mapping was not found.")
    db.delete(tax_map)
    db.flush()
