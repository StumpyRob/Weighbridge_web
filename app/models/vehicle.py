from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from ..constants import REG_MAX
from .base import Base, utcnow


class Vehicle(Base):
    __tablename__ = "vehicles"
    __table_args__ = (
        sa.UniqueConstraint(
            "tenant_id",
            "registration",
            name="uq_vehicles_tenant_registration",
        ),
        sa.Index("ix_vehicles_tenant_id", "tenant_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, default=1)
    registration: Mapped[str] = mapped_column(String(REG_MAX), nullable=False)
    owner_customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    default_customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    vehicle_type_id: Mapped[int | None] = mapped_column(
        ForeignKey("vehicle_types.id")
    )
    default_tare_kg: Mapped[float | None] = mapped_column(Numeric(12, 3))
    overweight_threshold_kg: Mapped[float | None] = mapped_column(Numeric(12, 3))
    haulier_id: Mapped[int | None] = mapped_column(ForeignKey("hauliers.id"))
    default_haulier_id: Mapped[int | None] = mapped_column(ForeignKey("hauliers.id"))
    driver_id: Mapped[int | None] = mapped_column(ForeignKey("drivers.id"))
    default_driver_id: Mapped[int | None] = mapped_column(ForeignKey("drivers.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )
