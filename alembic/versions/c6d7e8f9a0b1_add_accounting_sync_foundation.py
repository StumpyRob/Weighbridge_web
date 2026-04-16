"""add accounting sync foundation

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-04-16 15:10:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "c6d7e8f9a0b1"
down_revision = "b5c6d7e8f9a0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "accounting_connections",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("realm_id", sa.String(length=64), nullable=True),
        sa.Column("encrypted_access_token", sa.Text(), nullable=True),
        sa.Column("encrypted_refresh_token", sa.Text(), nullable=True),
        sa.Column("access_token_expires_at", sa.DateTime(), nullable=True),
        sa.Column("refresh_token_expires_at", sa.DateTime(), nullable=True),
        sa.Column("scopes", sa.Text(), nullable=True),
        sa.Column("connected_at", sa.DateTime(), nullable=True),
        sa.Column("disconnected_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            name="uq_accounting_connections_tenant_provider",
        ),
    )
    op.create_index(
        "ix_accounting_connections_tenant_id",
        "accounting_connections",
        ["tenant_id"],
    )
    op.create_index(
        "ix_accounting_connections_tenant_provider_status",
        "accounting_connections",
        ["tenant_id", "provider", "status"],
    )

    op.create_table(
        "accounting_customer_maps",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("sync_status", sa.String(length=32), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("payload_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
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
    )
    op.create_index(
        "ix_accounting_customer_maps_tenant_id",
        "accounting_customer_maps",
        ["tenant_id"],
    )
    op.create_index(
        "ix_accounting_customer_maps_customer_id",
        "accounting_customer_maps",
        ["customer_id"],
    )
    op.create_index(
        "ix_accounting_customer_maps_sync_status",
        "accounting_customer_maps",
        ["sync_status"],
    )

    op.create_table(
        "accounting_product_maps",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("sync_status", sa.String(length=32), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("payload_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
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
    )
    op.create_index(
        "ix_accounting_product_maps_tenant_id",
        "accounting_product_maps",
        ["tenant_id"],
    )
    op.create_index(
        "ix_accounting_product_maps_product_id",
        "accounting_product_maps",
        ["product_id"],
    )
    op.create_index(
        "ix_accounting_product_maps_sync_status",
        "accounting_product_maps",
        ["sync_status"],
    )

    op.create_table(
        "accounting_invoice_syncs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("invoice_id", sa.Integer(), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("external_doc_number", sa.String(length=255), nullable=True),
        sa.Column("sync_status", sa.String(length=32), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("payload_hash", sa.String(length=64), nullable=True),
        sa.Column("provider_response_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            "invoice_id",
            name="uq_accounting_invoice_syncs_tenant_provider_invoice_id",
        ),
    )
    op.create_index(
        "ix_accounting_invoice_syncs_tenant_id",
        "accounting_invoice_syncs",
        ["tenant_id"],
    )
    op.create_index(
        "ix_accounting_invoice_syncs_invoice_id",
        "accounting_invoice_syncs",
        ["invoice_id"],
    )
    op.create_index(
        "ix_accounting_invoice_syncs_sync_status",
        "accounting_invoice_syncs",
        ["sync_status"],
    )

    op.create_table(
        "accounting_sync_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("job_type", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("locked_at", sa.DateTime(), nullable=True),
        sa.Column("lock_token", sa.String(length=64), nullable=True),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_accounting_sync_jobs_tenant_id",
        "accounting_sync_jobs",
        ["tenant_id"],
    )
    op.create_index(
        "ix_accounting_sync_jobs_queue",
        "accounting_sync_jobs",
        ["tenant_id", "provider", "status", "available_at"],
    )
    op.create_index(
        "ix_accounting_sync_jobs_entity",
        "accounting_sync_jobs",
        ["tenant_id", "entity_type", "entity_id"],
    )
    op.create_index(
        "ix_accounting_sync_jobs_lock_token",
        "accounting_sync_jobs",
        ["lock_token"],
    )

    op.create_table(
        "accounting_sync_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=True),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("summary", sa.String(length=255), nullable=False),
        sa.Column("detail_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_accounting_sync_events_tenant_provider_created_at",
        "accounting_sync_events",
        ["tenant_id", "provider", "created_at"],
    )
    op.create_index(
        "ix_accounting_sync_events_entity",
        "accounting_sync_events",
        ["tenant_id", "entity_type", "entity_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_accounting_sync_events_entity",
        table_name="accounting_sync_events",
    )
    op.drop_index(
        "ix_accounting_sync_events_tenant_provider_created_at",
        table_name="accounting_sync_events",
    )
    op.drop_table("accounting_sync_events")

    op.drop_index(
        "ix_accounting_sync_jobs_lock_token",
        table_name="accounting_sync_jobs",
    )
    op.drop_index(
        "ix_accounting_sync_jobs_entity",
        table_name="accounting_sync_jobs",
    )
    op.drop_index(
        "ix_accounting_sync_jobs_queue",
        table_name="accounting_sync_jobs",
    )
    op.drop_index(
        "ix_accounting_sync_jobs_tenant_id",
        table_name="accounting_sync_jobs",
    )
    op.drop_table("accounting_sync_jobs")

    op.drop_index(
        "ix_accounting_invoice_syncs_sync_status",
        table_name="accounting_invoice_syncs",
    )
    op.drop_index(
        "ix_accounting_invoice_syncs_invoice_id",
        table_name="accounting_invoice_syncs",
    )
    op.drop_index(
        "ix_accounting_invoice_syncs_tenant_id",
        table_name="accounting_invoice_syncs",
    )
    op.drop_table("accounting_invoice_syncs")

    op.drop_index(
        "ix_accounting_product_maps_sync_status",
        table_name="accounting_product_maps",
    )
    op.drop_index(
        "ix_accounting_product_maps_product_id",
        table_name="accounting_product_maps",
    )
    op.drop_index(
        "ix_accounting_product_maps_tenant_id",
        table_name="accounting_product_maps",
    )
    op.drop_table("accounting_product_maps")

    op.drop_index(
        "ix_accounting_customer_maps_sync_status",
        table_name="accounting_customer_maps",
    )
    op.drop_index(
        "ix_accounting_customer_maps_customer_id",
        table_name="accounting_customer_maps",
    )
    op.drop_index(
        "ix_accounting_customer_maps_tenant_id",
        table_name="accounting_customer_maps",
    )
    op.drop_table("accounting_customer_maps")

    op.drop_index(
        "ix_accounting_connections_tenant_provider_status",
        table_name="accounting_connections",
    )
    op.drop_index(
        "ix_accounting_connections_tenant_id",
        table_name="accounting_connections",
    )
    op.drop_table("accounting_connections")
