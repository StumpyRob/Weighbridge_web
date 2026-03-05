from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..constants import ADDRESS_LINE_MAX, NAME_MAX, POSTCODE_MAX
from .base import Base, utcnow


class CompanySetting(Base):
    __tablename__ = "company_settings"
    __table_args__ = (
        sa.UniqueConstraint("tenant_id", name="uq_company_settings_tenant_id"),
        sa.Index("ix_company_settings_tenant_id", "tenant_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(sa.ForeignKey("tenants.id"), nullable=False, default=1)
    name: Mapped[str | None] = mapped_column(String(NAME_MAX), nullable=True)
    address_line1: Mapped[str | None] = mapped_column(String(ADDRESS_LINE_MAX), nullable=True)
    address_line2: Mapped[str | None] = mapped_column(String(ADDRESS_LINE_MAX), nullable=True)
    city: Mapped[str | None] = mapped_column(String(ADDRESS_LINE_MAX), nullable=True)
    postcode: Mapped[str | None] = mapped_column(String(POSTCODE_MAX), nullable=True)
    country: Mapped[str | None] = mapped_column(String(ADDRESS_LINE_MAX), nullable=True)
    company_logo_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    company_logo_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    nav_logo_height_px: Mapped[int | None] = mapped_column(Integer, nullable=True)
    navbar_color_hex: Mapped[str | None] = mapped_column(String(7), nullable=True)
    primary_color_hex: Mapped[str | None] = mapped_column(String(7), nullable=True)
    show_nav_logo: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    show_nav_title: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_initialized: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
        onupdate=utcnow,
    )
