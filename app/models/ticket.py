from datetime import datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, utcnow


class DirectionEnum(str, Enum):
    INWARD = "INWARD"
    OUTWARD = "OUTWARD"


class TransactionTypeEnum(str, Enum):
    WASTEIN = "WASTEIN"
    WASTEOUT = "WASTEOUT"
    SALE = "SALE"


class TicketStatusEnum(str, Enum):
    OPEN = "OPEN"
    COMPLETE = "COMPLETE"
    VOID = "VOID"


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        Index("ix_tickets_datetime", "datetime"),
        Index("ix_tickets_status", "status"),
        Index("ix_tickets_customer_id", "customer_id"),
        Index("ix_tickets_vehicle_id", "vehicle_id"),
        Index("ix_tickets_haulier_id", "haulier_id"),
        Index("ix_tickets_driver_id", "driver_id"),
        Index("ix_tickets_container_id", "container_id"),
        Index("ix_tickets_destination_id", "destination_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_no: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )
    datetime: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[TicketStatusEnum] = mapped_column(
        SAEnum(TicketStatusEnum, native_enum=False, create_constraint=False),
        nullable=False,
    )
    direction: Mapped[DirectionEnum] = mapped_column(
        SAEnum(DirectionEnum, native_enum=False, create_constraint=False),
        nullable=False,
    )
    transaction_type: Mapped[TransactionTypeEnum] = mapped_column(
        SAEnum(TransactionTypeEnum, native_enum=False, create_constraint=False),
        nullable=False,
    )
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
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    total: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    dont_invoice: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    paid: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    payment_method_id: Mapped[int | None] = mapped_column(Integer)
    product: Mapped["Product | None"] = relationship("Product")
    haulier: Mapped["Haulier | None"] = relationship("Haulier")
    driver: Mapped["Driver | None"] = relationship("Driver")
    container: Mapped["Container | None"] = relationship("Container")
    destination: Mapped["Destination | None"] = relationship("Destination")
