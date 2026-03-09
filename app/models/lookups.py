from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from ..constants import CODE_MAX, NAME_MAX
from .base import Base


class Haulier(Base):
    __tablename__ = "hauliers"
    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "name", name="uq_hauliers_tenant_name"),
        sa.Index("ix_hauliers_tenant_id", "tenant_id"),
        sa.Index("ix_hauliers_name", "name"),
        sa.Index("ix_hauliers_is_active", "is_active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, default=1
    )
    name: Mapped[str] = mapped_column(String(NAME_MAX), nullable=False)
    carrier_licence_number: Mapped[str | None] = mapped_column(String(CODE_MAX))
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.true()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Driver(Base):
    __tablename__ = "drivers"
    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "name", name="uq_drivers_tenant_name"),
        sa.Index("ix_drivers_tenant_id", "tenant_id"),
        sa.Index("ix_drivers_name", "name"),
        sa.Index("ix_drivers_is_active", "is_active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, default=1
    )
    name: Mapped[str] = mapped_column(String(NAME_MAX), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.true()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Container(Base):
    __tablename__ = "containers"
    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "name", name="uq_containers_tenant_name"),
        sa.Index("ix_containers_tenant_id", "tenant_id"),
        sa.Index("ix_containers_name", "name"),
        sa.Index("ix_containers_is_active", "is_active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, default=1
    )
    name: Mapped[str] = mapped_column(String(NAME_MAX), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.true()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Destination(Base):
    __tablename__ = "destinations"
    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "name", name="uq_destinations_tenant_name"),
        sa.Index("ix_destinations_tenant_id", "tenant_id"),
        sa.Index("ix_destinations_name", "name"),
        sa.Index("ix_destinations_is_active", "is_active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, default=1
    )
    name: Mapped[str] = mapped_column(String(NAME_MAX), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.true()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
