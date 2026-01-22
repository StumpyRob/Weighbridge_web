from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("product_groups.id"))
    unit_id: Mapped[int | None] = mapped_column(ForeignKey("units.id"))
    tax_rate_id: Mapped[int | None] = mapped_column(ForeignKey("tax_rates.id"))
    nominal_code_id: Mapped[int | None] = mapped_column(ForeignKey("nominal_codes.id"))
    account_price: Mapped[float | None] = mapped_column(Numeric(12, 2))
    cash_price: Mapped[float | None] = mapped_column(Numeric(12, 2))
    min_price: Mapped[float | None] = mapped_column(Numeric(12, 2))
    max_price: Mapped[float | None] = mapped_column(Numeric(12, 2))
    max_qty: Mapped[float | None] = mapped_column(Numeric(12, 3))
    excess_trigger: Mapped[float | None] = mapped_column(Numeric(12, 3))
    excess_price: Mapped[float | None] = mapped_column(Numeric(12, 2))
    is_hazardous: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    final_disposal: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    used_on_site: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    default_waste_code_id: Mapped[int | None] = mapped_column(
        ForeignKey("waste_codes.id")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
