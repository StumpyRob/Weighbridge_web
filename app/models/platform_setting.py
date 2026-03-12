from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Boolean, DateTime, Float, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class PlatformSetting(Base):
    __tablename__ = "platform_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    default_ai_model: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    ai_temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ai_dashboard_insights_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    ai_dashboard_cache_ttl_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ai_default_response_style: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)
    ai_default_focus: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)
    ai_extra_global_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
        onupdate=utcnow,
    )
