from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..constants import ADDRESS_LINE_MAX, NAME_MAX, POSTCODE_MAX
from .base import Base, utcnow


class CompanySetting(Base):
    __tablename__ = "company_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(NAME_MAX), nullable=True)
    address_line1: Mapped[str | None] = mapped_column(String(ADDRESS_LINE_MAX), nullable=True)
    address_line2: Mapped[str | None] = mapped_column(String(ADDRESS_LINE_MAX), nullable=True)
    city: Mapped[str | None] = mapped_column(String(ADDRESS_LINE_MAX), nullable=True)
    postcode: Mapped[str | None] = mapped_column(String(POSTCODE_MAX), nullable=True)
    country: Mapped[str | None] = mapped_column(String(ADDRESS_LINE_MAX), nullable=True)
    company_logo_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    company_logo_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Legacy logo fields retained for backward compatibility with existing DB rows.
    logo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    logo_file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
        onupdate=utcnow,
    )
