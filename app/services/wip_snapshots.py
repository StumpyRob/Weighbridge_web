from __future__ import annotations

from ..models import Customer, Product


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
    return {
        "final_disposal_wip": bool(product.final_disposal_wip),
        "used_on_site_wip": bool(product.used_on_site_wip),
    }


def ticket_wip_snapshot(
    *, customer: Customer | None, product: Product | None
) -> dict[str, object]:
    return {
        "customer": customer_wip_snapshot(customer),
        "product": product_wip_snapshot(product),
    }
