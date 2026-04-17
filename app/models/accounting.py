from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..constants import CODE_MAX, DESC_MAX, NOMINAL_CODE_MAX
from .base import Base, utcnow


class AccountingConnection(Base):
    __tablename__ = "accounting_connections"
    __table_args__ = (
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            name="uq_accounting_connections_tenant_provider",
        ),
        sa.Index("ix_accounting_connections_tenant_id", "tenant_id"),
        sa.Index(
            "ix_accounting_connections_tenant_provider_status",
            "tenant_id",
            "provider",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, default=1
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    realm_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    encrypted_access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    encrypted_refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    refresh_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    scopes: Mapped[str | None] = mapped_column(Text, nullable=True)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class AccountingCustomerMap(Base):
    __tablename__ = "accounting_customer_maps"
    __table_args__ = (
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            "customer_id",
            name="uq_accounting_customer_maps_tenant_provider_customer_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            "external_id",
            name="uq_accounting_customer_maps_tenant_provider_external_id",
        ),
        sa.Index("ix_accounting_customer_maps_tenant_id", "tenant_id"),
        sa.Index("ix_accounting_customer_maps_customer_id", "customer_id"),
        sa.Index("ix_accounting_customer_maps_sync_status", "sync_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, default=1
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id"), nullable=False
    )
    external_id: Mapped[str] = mapped_column(String(DESC_MAX), nullable=False)
    sync_status: Mapped[str] = mapped_column(String(32), nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class AccountingProductMap(Base):
    __tablename__ = "accounting_product_maps"
    __table_args__ = (
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            "product_id",
            name="uq_accounting_product_maps_tenant_provider_product_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            "external_id",
            name="uq_accounting_product_maps_tenant_provider_external_id",
        ),
        sa.Index("ix_accounting_product_maps_tenant_id", "tenant_id"),
        sa.Index("ix_accounting_product_maps_product_id", "product_id"),
        sa.Index("ix_accounting_product_maps_sync_status", "sync_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, default=1
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    external_id: Mapped[str] = mapped_column(String(DESC_MAX), nullable=False)
    sync_status: Mapped[str] = mapped_column(String(32), nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class AccountingTaxMap(Base):
    __tablename__ = "accounting_tax_maps"
    __table_args__ = (
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            "tax_rate_id",
            name="uq_accounting_tax_maps_tenant_provider_tax_rate_id",
        ),
        sa.Index("ix_accounting_tax_maps_tenant_id", "tenant_id"),
        sa.Index("ix_accounting_tax_maps_tax_rate_id", "tax_rate_id"),
        sa.Index(
            "ix_accounting_tax_maps_tenant_provider_active",
            "tenant_id",
            "provider",
            "is_active",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, default=1
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    tax_rate_id: Mapped[int] = mapped_column(ForeignKey("tax_rates.id"), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(DESC_MAX), nullable=True)
    external_code: Mapped[str | None] = mapped_column(String(CODE_MAX), nullable=True)
    name: Mapped[str | None] = mapped_column(String(DESC_MAX), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class AccountingRevenueAccountMap(Base):
    __tablename__ = "accounting_revenue_account_maps"
    __table_args__ = (
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            "local_scope_type",
            "local_scope_id",
            name="uq_accounting_revenue_account_maps_scope_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            "local_scope_type",
            "local_nominal_code",
            name="uq_accounting_revenue_account_maps_nominal_code",
        ),
        sa.Index("ix_accounting_revenue_account_maps_tenant_id", "tenant_id"),
        sa.Index(
            "ix_accounting_revenue_account_maps_provider_scope",
            "tenant_id",
            "provider",
            "local_scope_type",
            "is_active",
        ),
        sa.Index(
            "uq_accounting_revenue_account_maps_global_default",
            "tenant_id",
            "provider",
            "local_scope_type",
            unique=True,
            sqlite_where=sa.text(
                "local_scope_type = 'global_default' "
                "AND local_scope_id IS NULL "
                "AND local_nominal_code IS NULL"
            ),
            postgresql_where=sa.text(
                "local_scope_type = 'global_default' "
                "AND local_scope_id IS NULL "
                "AND local_nominal_code IS NULL"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, default=1
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    local_scope_type: Mapped[str] = mapped_column(String(32), nullable=False)
    local_scope_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    local_nominal_code: Mapped[str | None] = mapped_column(
        String(NOMINAL_CODE_MAX), nullable=True
    )
    remote_account_id: Mapped[str] = mapped_column(String(DESC_MAX), nullable=False)
    remote_account_code: Mapped[str | None] = mapped_column(String(CODE_MAX), nullable=True)
    remote_account_name: Mapped[str] = mapped_column(String(DESC_MAX), nullable=False)
    remote_account_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class AccountingInvoiceSync(Base):
    __tablename__ = "accounting_invoice_syncs"
    __table_args__ = (
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            "invoice_id",
            name="uq_accounting_invoice_syncs_tenant_provider_invoice_id",
        ),
        sa.Index("ix_accounting_invoice_syncs_tenant_id", "tenant_id"),
        sa.Index("ix_accounting_invoice_syncs_invoice_id", "invoice_id"),
        sa.Index("ix_accounting_invoice_syncs_sync_status", "sync_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, default=1
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(DESC_MAX), nullable=True)
    external_doc_number: Mapped[str | None] = mapped_column(
        String(DESC_MAX), nullable=True
    )
    sync_status: Mapped[str] = mapped_column(String(32), nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_response_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class AccountingSyncJob(Base):
    __tablename__ = "accounting_sync_jobs"
    __table_args__ = (
        sa.Index("ix_accounting_sync_jobs_tenant_id", "tenant_id"),
        sa.Index(
            "ix_accounting_sync_jobs_queue",
            "tenant_id",
            "provider",
            "status",
            "available_at",
        ),
        sa.Index(
            "ix_accounting_sync_jobs_entity",
            "tenant_id",
            "entity_type",
            "entity_id",
        ),
        sa.Index("ix_accounting_sync_jobs_lock_token", "lock_token"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, default=1
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lock_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class AccountingSyncEvent(Base):
    __tablename__ = "accounting_sync_events"
    __table_args__ = (
        sa.Index(
            "ix_accounting_sync_events_tenant_provider_created_at",
            "tenant_id",
            "provider",
            "created_at",
        ),
        sa.Index(
            "ix_accounting_sync_events_entity",
            "tenant_id",
            "entity_type",
            "entity_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, default=1
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    summary: Mapped[str] = mapped_column(String(DESC_MAX), nullable=False)
    detail_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
