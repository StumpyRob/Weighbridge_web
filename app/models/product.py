from __future__ import annotations

from datetime import datetime

from decimal import Decimal
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..constants import CODE_MAX, DESC_MAX, NOMINAL_CODE_MAX
from .base import Base, utcnow

if TYPE_CHECKING:
    from .ewc_code import EwcCode
    from .lookups import Destination
    from .lookups_misc import ProductGroup, TaxRate, Unit


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "code", name="uq_products_tenant_code"),
        sa.Index("ix_products_tenant_id", "tenant_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, default=1)
    code: Mapped[str] = mapped_column(String(CODE_MAX), nullable=False)
    description: Mapped[str] = mapped_column(String(DESC_MAX), nullable=False)
    product_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    sales_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("product_groups.id"))
    product_group: Mapped[ProductGroup | None] = relationship("ProductGroup")
    unit_id: Mapped[int | None] = mapped_column(ForeignKey("units.id"), nullable=True)
    unit: Mapped[Unit | None] = relationship("Unit")
    tax_rate_id: Mapped[int | None] = mapped_column(ForeignKey("tax_rates.id"))
    tax_rate: Mapped[TaxRate | None] = relationship("TaxRate")
    nominal_code_id: Mapped[int | None] = mapped_column(ForeignKey("nominal_codes.id"))
    nominal_code: Mapped[str | None] = mapped_column(String(NOMINAL_CODE_MAX), nullable=True)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    account_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    cash_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    min_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    max_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    max_qty: Mapped[float | None] = mapped_column(Numeric(12, 3))
    excess_trigger: Mapped[float | None] = mapped_column(Numeric(12, 3))
    excess_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    is_hazardous: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    final_disposal: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    used_on_site: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    final_disposal_wip: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    used_on_site_wip: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    ewc_code_id: Mapped[int | None] = mapped_column(
        ForeignKey("ewc_codes.id", ondelete="SET NULL")
    )
    default_destination_id: Mapped[int | None] = mapped_column(
        ForeignKey("destinations.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )
    ewc_code: Mapped[EwcCode | None] = relationship("EwcCode")
    default_destination: Mapped[Destination | None] = relationship("Destination")
