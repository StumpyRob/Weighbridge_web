from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..constants import DESC_MAX, NAME_MAX
from .base import Base, utcnow


class WorkstationPrinterProfile(Base):
    __tablename__ = "workstation_printer_profiles"
    __table_args__ = (
        sa.UniqueConstraint(
            "tenant_id",
            "workstation_key",
            "document_type",
            name="uq_workstation_printer_profiles_tenant_key_document",
        ),
        sa.Index("ix_workstation_printer_profiles_tenant_id", "tenant_id"),
        sa.Index(
            "ix_workstation_printer_profiles_tenant_workstation_key",
            "tenant_id",
            "workstation_key",
        ),
        sa.Index(
            "ix_workstation_printer_profiles_document_type",
            "document_type",
        ),
        sa.Index(
            "ix_workstation_printer_profiles_is_active",
            "is_active",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, default=1
    )
    workstation_key: Mapped[str] = mapped_column(String(64), nullable=False)
    workstation_label: Mapped[str | None] = mapped_column(String(NAME_MAX), nullable=True)
    document_type: Mapped[str] = mapped_column(String(16), nullable=False)
    printer_name: Mapped[str | None] = mapped_column(String(DESC_MAX), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
        onupdate=utcnow,
    )
