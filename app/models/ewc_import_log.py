from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class EwcImportLog(Base):
    __tablename__ = "ewc_import_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_file: Mapped[str] = mapped_column(String(255), nullable=False)
    replace_mode: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    total_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    inserted_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unchanged_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    deactivated_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    errors_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    imported_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
