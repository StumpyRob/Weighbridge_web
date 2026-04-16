from __future__ import annotations

from ..models import Customer, Product, TaxRate
from .pricing import product_effective_nominal_code


def _snapshot_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def customer_wip_snapshot(customer: Customer | None) -> dict[str, object]:
    if customer is None:
        return {
            "vat_number": None,
            "is_cash_account": False,
            "credit_limit_pence": None,
        }
    return {
        "vat_number": customer.vat_number,
        "is_cash_account": bool(customer.is_cash_account),
        "credit_limit_pence": customer.credit_limit_pence,
    }


def product_wip_snapshot(product: Product | None) -> dict[str, object]:
    if product is None:
        return {
            "final_disposal_wip": False,
            "used_on_site_wip": False,
        }
    final_disposal = bool(getattr(product, "final_disposal", False)) or bool(
        getattr(product, "final_disposal_wip", False)
    )
    used_on_site = bool(getattr(product, "used_on_site", False)) or bool(
        getattr(product, "used_on_site_wip", False)
    )
    return {
        "final_disposal_wip": final_disposal,
        "used_on_site_wip": used_on_site,
    }


def invoice_product_snapshot(
    product: Product | None,
    *,
    tax_rate: TaxRate | None = None,
) -> dict[str, object]:
    snapshot = product_wip_snapshot(product)
    if product is None:
        snapshot.update(
            {
                "product_id": None,
                "product_code": None,
                "tax_rate_id": None,
                "tax_rate_code": None,
                "tax_rate_percent": None,
                "nominal_code": None,
            }
        )
        return snapshot

    resolved_tax_rate = tax_rate or getattr(product, "tax_rate", None)
    snapshot.update(
        {
            "product_id": getattr(product, "id", None),
            "product_code": _snapshot_text(getattr(product, "code", None)),
            "tax_rate_id": getattr(product, "tax_rate_id", None),
            "tax_rate_code": _snapshot_text(getattr(resolved_tax_rate, "code", None)),
            "tax_rate_percent": _snapshot_text(
                getattr(resolved_tax_rate, "rate_percent", None)
            ),
            "nominal_code": product_effective_nominal_code(product),
        }
    )
    return snapshot


def ticket_wip_snapshot(
    *, customer: Customer | None, product: Product | None
) -> dict[str, object]:
    return {
        "customer": customer_wip_snapshot(customer),
        "product": product_wip_snapshot(product),
    }
