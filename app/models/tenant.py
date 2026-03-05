from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class Tenant(Base):
    __tablename__ = "tenants"
    __table_args__ = (
        sa.UniqueConstraint("subdomain", name="uq_tenants_subdomain"),
        sa.Index("ix_tenants_is_active", "is_active"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    subdomain: Mapped[str] = mapped_column(String(63), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
