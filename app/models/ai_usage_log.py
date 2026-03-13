from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class AIUsageLog(Base):
    __tablename__ = "ai_usage_logs"
    __table_args__ = (
        sa.Index("ix_ai_usage_logs_tenant_request_occurred", "tenant_id", "request_type", "occurred_at"),
        sa.Index("ix_ai_usage_logs_user_request_occurred", "user_id", "request_type", "occurred_at"),
        sa.Index("ix_ai_usage_logs_request_occurred", "request_type", "occurred_at"),
        sa.Index("ix_ai_usage_logs_success_occurred", "success", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    request_type: Mapped[str] = mapped_column(String(32), nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa.false())
    error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    counted_toward_limit: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=sa.false(),
    )
