from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_no: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), nullable=False)
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    payment_method_id: Mapped[int | None] = mapped_column(
        ForeignKey("payment_methods.id")
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)
    net_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    vat_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    gross_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
