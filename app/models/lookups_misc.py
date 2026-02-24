from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..constants import CODE_MAX, DESC_MAX, NAME_MAX, NOMINAL_CODE_MAX
from .base import Base, utcnow


class Yard(Base):
    __tablename__ = "yards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(CODE_MAX), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(DESC_MAX))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class Area(Base):
    __tablename__ = "areas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(CODE_MAX), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(DESC_MAX))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class WasteCode(Base):
    __tablename__ = "waste_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(CODE_MAX), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(DESC_MAX))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class HazCode(Base):
    __tablename__ = "haz_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(CODE_MAX), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(DESC_MAX))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class SICCode(Base):
    __tablename__ = "sic_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(CODE_MAX), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(DESC_MAX))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class Licence(Base):
    __tablename__ = "licences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(CODE_MAX), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(DESC_MAX))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class WasteProducer(Base):
    __tablename__ = "waste_producers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(NAME_MAX), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(DESC_MAX))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class Recycler(Base):
    __tablename__ = "recyclers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(NAME_MAX), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(DESC_MAX))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class Supplier(Base):
    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(NAME_MAX), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(DESC_MAX))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class Contractor(Base):
    __tablename__ = "contractors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(NAME_MAX), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(DESC_MAX))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class Unit(Base):
    __tablename__ = "units"
    __table_args__ = (
        sa.UniqueConstraint("name", name="uq_units_name"),
        sa.Index("ix_units_name", "name"),
        sa.Index("ix_units_is_active", "is_active"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(NAME_MAX), nullable=False)
    unit_type: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default="COUNT"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa.true()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class TaxRate(Base):
    __tablename__ = "tax_rates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(CODE_MAX), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(DESC_MAX))
    rate_percent: Mapped[float | None] = mapped_column(Numeric(6, 3))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class PaymentMethod(Base):
    __tablename__ = "payment_methods"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(CODE_MAX), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(DESC_MAX))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class NominalCode(Base):
    __tablename__ = "nominal_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(CODE_MAX), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(DESC_MAX))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class CostCenter(Base):
    __tablename__ = "cost_centers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(CODE_MAX), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(DESC_MAX))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class InvoiceFrequency(Base):
    __tablename__ = "invoice_frequencies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(CODE_MAX), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(DESC_MAX))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class VoidReason(Base):
    __tablename__ = "void_reasons"
    __table_args__ = (
        sa.UniqueConstraint(
            "code",
            "reason_type",
            name="uq_void_reasons_code_reason_type",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(CODE_MAX), nullable=False)
    reason_type: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="TICKET"
    )
    description: Mapped[str | None] = mapped_column(String(DESC_MAX))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class VehicleType(Base):
    __tablename__ = "vehicle_types"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(CODE_MAX), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(DESC_MAX))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class ProductGroup(Base):
    __tablename__ = "product_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(CODE_MAX), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(NAME_MAX), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(DESC_MAX))
    nominal_code_default: Mapped[str | None] = mapped_column(String(NOMINAL_CODE_MAX))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class PrintDestination(Base):
    __tablename__ = "print_destinations"
    __table_args__ = (
        sa.UniqueConstraint("name", name="uq_print_destinations_name"),
        sa.Index("ix_print_destinations_document_type", "document_type"),
        sa.Index("ix_print_destinations_delivery_type", "delivery_type"),
        sa.Index("ix_print_destinations_is_active", "is_active"),
        sa.Index("ix_print_destinations_template_id", "template_id"),
        sa.Index(
            "uq_print_destinations_default_active_doc_type",
            "document_type",
            unique=True,
            sqlite_where=sa.text("is_default = 1 AND is_active = 1"),
            postgresql_where=sa.text("is_default AND is_active"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(CODE_MAX), nullable=False)
    description: Mapped[str | None] = mapped_column(String(DESC_MAX))
    document_type: Mapped[str] = mapped_column(String(16), nullable=False)
    template_id: Mapped[int] = mapped_column(
        ForeignKey("print_templates.id"), nullable=False
    )
    delivery_type: Mapped[str] = mapped_column(String(32), nullable=False)
    delivery_config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

class PrintTemplate(Base):
    __tablename__ = "print_templates"
    __table_args__ = (
        sa.UniqueConstraint("code", name="uq_print_templates_code"),
        sa.Index("ix_print_templates_document_type", "document_type"),
        sa.Index("ix_print_templates_is_active", "is_active"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str | None] = mapped_column(String(CODE_MAX), nullable=True)
    description: Mapped[str | None] = mapped_column(String(DESC_MAX))
    document_type: Mapped[str] = mapped_column(String(16), nullable=False)
    format: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

class PrintTemplateVersion(Base):
    __tablename__ = "print_template_versions"
    __table_args__ = (sa.Index("ix_print_template_versions_template_id", "template_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_id: Mapped[int] = mapped_column(
        ForeignKey("print_templates.id"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PrintJob(Base):
    __tablename__ = "print_jobs"
    __table_args__ = (
        sa.Index("ix_print_jobs_status", "status"),
        sa.Index("ix_print_jobs_document_type", "document_type"),
        sa.Index("ix_print_jobs_destination_id", "destination_id"),
        sa.Index("ix_print_jobs_template_id", "template_id"),
        sa.Index("ix_print_jobs_ticket_id", "ticket_id"),
        sa.Index("ix_print_jobs_invoice_id", "invoice_id"),
        sa.Index("ix_print_jobs_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    document_type: Mapped[str] = mapped_column(String(16), nullable=False)
    destination_id: Mapped[int | None] = mapped_column(
        ForeignKey("print_destinations.id"), nullable=True
    )
    template_id: Mapped[int | None] = mapped_column(
        ForeignKey("print_templates.id"), nullable=True
    )
    ticket_id: Mapped[int | None] = mapped_column(ForeignKey("tickets.id"), nullable=True)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"), nullable=True)
    delivery_type: Mapped[str] = mapped_column(String(32), nullable=False)
    delivery_config_json: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )
    rendered_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    rendered_bytes_base64: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
