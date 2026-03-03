from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        sa.Index("ix_audit_events_occurred_at", "occurred_at"),
        sa.Index("ix_audit_events_action", "action"),
        sa.Index("ix_audit_events_entity_type", "entity_type"),
        sa.Index("ix_audit_events_entity", "entity_type", "entity_id"),
        sa.Index("ix_audit_events_user_occurred", "user_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary: Mapped[str | None] = mapped_column(String(255), nullable=True)
    details_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
