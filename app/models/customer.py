from datetime import datetime

from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    invoice_email: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(50))
    address_line1: Mapped[str | None] = mapped_column(String(255))
    address_line2: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(100))
    postcode: Mapped[str | None] = mapped_column(String(50))
    country: Mapped[str | None] = mapped_column(String(100))
    vat_number: Mapped[str | None] = mapped_column(String(50))
    invoice_frequency_id: Mapped[int | None] = mapped_column(
        ForeignKey("invoice_frequencies.id")
    )
    payment_terms: Mapped[str | None] = mapped_column(String(100))
    credit_limit: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    on_stop: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cash_account: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    do_not_invoice: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    must_have_po: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
