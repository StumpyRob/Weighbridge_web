from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..constants import DESC_MAX, NAME_MAX
from .base import Base, utcnow


class UserFeedback(Base):
    __tablename__ = "user_feedback"
    __table_args__ = (
        sa.Index("ix_user_feedback_tenant_created", "tenant_id", "created_at"),
        sa.Index("ix_user_feedback_tenant_status", "tenant_id", "status"),
        sa.Index("ix_user_feedback_tenant_kind", "tenant_id", "kind"),
        sa.Index(
            "ix_user_feedback_tenant_email_status",
            "tenant_id",
            "email_delivery_status",
        ),
        sa.Index("ix_user_feedback_submitted_by", "submitted_by_user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, default=1)
    submitted_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"),
        nullable=True,
    )
    reviewed_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"),
        nullable=True,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="new")
    title: Mapped[str | None] = mapped_column(String(NAME_MAX), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    source_path: Mapped[str | None] = mapped_column(String(DESC_MAX), nullable=True)
    submitted_by_display_name: Mapped[str | None] = mapped_column(
        String(NAME_MAX),
        nullable=True,
    )
    submitted_by_email: Mapped[str | None] = mapped_column(String(DESC_MAX), nullable=True)
    host_name: Mapped[str | None] = mapped_column(String(DESC_MAX), nullable=True)
    recipient_email: Mapped[str | None] = mapped_column(String(DESC_MAX), nullable=True)
    email_delivery_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
    )
    email_delivery_error: Mapped[str | None] = mapped_column(
        String(DESC_MAX),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
