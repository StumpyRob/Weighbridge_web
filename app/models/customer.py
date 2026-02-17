from datetime import datetime

from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from ..constants import (
    ADDRESS_LINE_MAX,
    CODE_MAX,
    DESC_MAX,
    NAME_MAX,
    POSTCODE_MAX,
)
from .base import Base, utcnow


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_code: Mapped[str] = mapped_column(String(CODE_MAX), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(NAME_MAX), nullable=False)
    invoice_email: Mapped[str | None] = mapped_column(String(DESC_MAX))
    phone: Mapped[str | None] = mapped_column(String(CODE_MAX))
    address_line1: Mapped[str | None] = mapped_column(String(ADDRESS_LINE_MAX))
    address_line2: Mapped[str | None] = mapped_column(String(ADDRESS_LINE_MAX))
    city: Mapped[str | None] = mapped_column(String(NAME_MAX))
    postcode: Mapped[str | None] = mapped_column(String(POSTCODE_MAX))
    country: Mapped[str | None] = mapped_column(String(NAME_MAX))
    vat_number: Mapped[str | None] = mapped_column(String(CODE_MAX))
    invoice_frequency_id: Mapped[int | None] = mapped_column(
        ForeignKey("invoice_frequencies.id")
    )
    invoice_frequency: Mapped[str | None] = mapped_column(String(20))
    payment_terms: Mapped[str | None] = mapped_column(String(NAME_MAX))
    payment_terms_days: Mapped[int | None] = mapped_column(Integer)
    credit_limit: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    on_stop: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cash_account: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    do_not_invoice: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    must_have_po: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )
