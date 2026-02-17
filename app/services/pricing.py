from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CustomerProductPrice, Product


def _normalize_text(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def product_effective_nominal_code(product: Product | None) -> str | None:
    if product is None:
        return None
    direct = _normalize_text(product.nominal_code)
    if direct:
        return direct
    product_group = getattr(product, "product_group", None)
    group_default = (
        _normalize_text(getattr(product_group, "nominal_code_default", None))
        if product_group is not None
        else None
    )
    return group_default


def customer_product_price_override(
    db: Session,
    *,
    customer_id: int | None,
    product_id: int | None,
) -> CustomerProductPrice | None:
    if not customer_id or not product_id:
        return None
    return (
        db.execute(
            select(CustomerProductPrice)
            .where(
                CustomerProductPrice.customer_id == customer_id,
                CustomerProductPrice.product_id == product_id,
                CustomerProductPrice.is_active.is_(True),
            )
            .order_by(CustomerProductPrice.id.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )


def resolve_unit_price_for_customer_product(
    db: Session,
    *,
    customer_id: int | None,
    product: Product | None,
) -> tuple[Decimal | None, bool]:
    if product is None:
        return None, False
    override = customer_product_price_override(
        db,
        customer_id=customer_id,
        product_id=product.id,
    )
    if override is not None:
        return override.unit_price, True
    return product.unit_price, False
