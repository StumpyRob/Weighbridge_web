from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class EwcCode(Base):
    __tablename__ = "ewc_codes"
    __table_args__ = (
        Index("idx_ewc_codes_code_6", "code_6"),
        Index("idx_ewc_codes_active", "active"),
        Index("idx_ewc_codes_hazardous", "hazardous"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code_6: Mapped[str] = mapped_column(String(6), unique=True, nullable=False)
    code_display: Mapped[str] = mapped_column(String(10), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    hazardous: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    source_file: Mapped[str] = mapped_column(String(255), nullable=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
