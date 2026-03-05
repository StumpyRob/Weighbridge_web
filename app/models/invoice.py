from datetime import date, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy import Date, DateTime, ForeignKey, Integer, JSON, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from ..constants import CODE_MAX
from .base import Base


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "invoice_no", name="uq_invoices_tenant_invoice_no"),
        sa.Index("ix_invoices_tenant_id", "tenant_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, default=1)
    invoice_no: Mapped[str] = mapped_column(String(CODE_MAX), nullable=False)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), nullable=False)
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(CODE_MAX), nullable=False)
    payment_method_id: Mapped[int | None] = mapped_column(
        ForeignKey("payment_methods.id")
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)
    net_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    vat_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    gross_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    customer_snapshot_json: Mapped[dict | None] = mapped_column(JSON)
