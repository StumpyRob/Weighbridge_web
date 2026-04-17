"""add accounting revenue account maps

Revision ID: e9a1b2c3d4f5
Revises: d6e7f8a9b0c1
Create Date: 2026-04-17 16:40:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "e9a1b2c3d4f5"
down_revision = "d6e7f8a9b0c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "accounting_revenue_account_maps",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("local_scope_type", sa.String(length=32), nullable=False),
        sa.Column("local_scope_id", sa.Integer(), nullable=True),
        sa.Column("local_nominal_code", sa.String(length=20), nullable=True),
        sa.Column("remote_account_id", sa.String(length=255), nullable=False),
        sa.Column("remote_account_code", sa.String(length=50), nullable=True),
        sa.Column("remote_account_name", sa.String(length=255), nullable=False),
        sa.Column("remote_account_type", sa.String(length=64), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
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
    )
    op.create_index(
        "ix_accounting_revenue_account_maps_tenant_id",
        "accounting_revenue_account_maps",
        ["tenant_id"],
    )
    op.create_index(
        "ix_accounting_revenue_account_maps_provider_scope",
        "accounting_revenue_account_maps",
        ["tenant_id", "provider", "local_scope_type", "is_active"],
    )
    op.create_index(
        "uq_accounting_revenue_account_maps_global_default",
        "accounting_revenue_account_maps",
        ["tenant_id", "provider", "local_scope_type"],
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
    )


def downgrade() -> None:
    op.drop_index(
        "uq_accounting_revenue_account_maps_global_default",
        table_name="accounting_revenue_account_maps",
    )
    op.drop_index(
        "ix_accounting_revenue_account_maps_provider_scope",
        table_name="accounting_revenue_account_maps",
    )
    op.drop_index(
        "ix_accounting_revenue_account_maps_tenant_id",
        table_name="accounting_revenue_account_maps",
    )
    op.drop_table("accounting_revenue_account_maps")
