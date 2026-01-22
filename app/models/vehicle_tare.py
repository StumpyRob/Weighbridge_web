from sqlalchemy import ForeignKey, Integer, Numeric
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class VehicleTare(Base):
    __tablename__ = "vehicle_tares"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicles.id"), nullable=False)
    container_id: Mapped[int] = mapped_column(ForeignKey("containers.id"), nullable=False)
    tare_kg: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)
