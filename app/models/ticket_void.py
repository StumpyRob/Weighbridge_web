from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..constants import NOTES_MAX
from .base import Base


class TicketVoid(Base):
    __tablename__ = "ticket_voids"
    __table_args__ = (sa.Index("ix_ticket_voids_tenant_id", "tenant_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, default=1)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), nullable=False)
    reason_id: Mapped[int] = mapped_column(ForeignKey("void_reasons.id"), nullable=False)
    note: Mapped[str] = mapped_column(String(NOTES_MAX), nullable=False)
    voided_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    voided_by: Mapped[str] = mapped_column(String(150), nullable=False)
