from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Vehicle(Base):
    __tablename__ = "vehicles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    registration: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    owner_customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    vehicle_type_id: Mapped[int | None] = mapped_column(
        ForeignKey("vehicle_types.id")
    )
    default_tare_kg: Mapped[float | None] = mapped_column(Numeric(12, 3))
    overweight_threshold_kg: Mapped[float | None] = mapped_column(Numeric(12, 3))
    haulier_id: Mapped[int | None] = mapped_column(ForeignKey("hauliers.id"))
    driver_id: Mapped[int | None] = mapped_column(ForeignKey("drivers.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
