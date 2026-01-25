from decimal import Decimal

from sqlalchemy import ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), nullable=False)
    ticket_id: Mapped[int | None] = mapped_column(ForeignKey("tickets.id"))
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    net: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    vat: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    gross: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
