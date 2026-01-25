from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class InvoiceVoid(Base):
    __tablename__ = "invoice_voids"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), nullable=False)
    reason_id: Mapped[int] = mapped_column(ForeignKey("void_reasons.id"), nullable=False)
    note: Mapped[str] = mapped_column(String(255), nullable=False)
    voided_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    voided_by: Mapped[str] = mapped_column(String(150), nullable=False)
