from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Index
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        Index("ix_tickets_datetime", "datetime"),
        Index("ix_tickets_status", "status"),
        Index("ix_tickets_customer_id", "customer_id"),
        Index("ix_tickets_vehicle_id", "vehicle_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_no: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    datetime: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    direction: Mapped[str] = mapped_column(String(50), nullable=False)
    transaction_type: Mapped[str] = mapped_column(String(50), nullable=False)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    vehicle_id: Mapped[int | None] = mapped_column(ForeignKey("vehicles.id"))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"))
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"))
    haulier_id: Mapped[int | None] = mapped_column(ForeignKey("hauliers.id"))
    driver_id: Mapped[int | None] = mapped_column(ForeignKey("drivers.id"))
    container_id: Mapped[int | None] = mapped_column(ForeignKey("containers.id"))
    destination_id: Mapped[int | None] = mapped_column(
        ForeignKey("destinations.id")
    )
    yard_id: Mapped[int | None] = mapped_column(ForeignKey("yards.id"))
    area_id: Mapped[int | None] = mapped_column(ForeignKey("areas.id"))
    waste_code_id: Mapped[int | None] = mapped_column(
        ForeignKey("waste_codes.id")
    )
    waste_producer_id: Mapped[int | None] = mapped_column(
        ForeignKey("waste_producers.id")
    )
    licence_id: Mapped[int | None] = mapped_column(ForeignKey("licences.id"))
    gross_kg: Mapped[float | None] = mapped_column(Numeric(12, 3))
    tare_kg: Mapped[float | None] = mapped_column(Numeric(12, 3))
    net_kg: Mapped[float | None] = mapped_column(Numeric(12, 3))
    qty: Mapped[float | None] = mapped_column(Numeric(12, 3))
    unit_id: Mapped[int | None] = mapped_column(Integer)
    unit_price: Mapped[float | None] = mapped_column(Numeric(12, 2))
    total: Mapped[float | None] = mapped_column(Numeric(12, 2))
    dont_invoice: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    paid: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    payment_method_id: Mapped[int | None] = mapped_column(Integer)
