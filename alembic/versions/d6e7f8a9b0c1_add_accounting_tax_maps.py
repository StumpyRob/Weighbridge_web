"""add accounting tax maps

Revision ID: d6e7f8a9b0c1
Revises: c6d7e8f9a0b1
Create Date: 2026-04-16 21:05:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "d6e7f8a9b0c1"
down_revision = "c6d7e8f9a0b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "accounting_tax_maps",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("tax_rate_id", sa.Integer(), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("external_code", sa.String(length=50), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tax_rate_id"], ["tax_rates.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            "tax_rate_id",
            name="uq_accounting_tax_maps_tenant_provider_tax_rate_id",
        ),
    )
    op.create_index(
        "ix_accounting_tax_maps_tenant_id",
        "accounting_tax_maps",
        ["tenant_id"],
    )
    op.create_index(
        "ix_accounting_tax_maps_tax_rate_id",
        "accounting_tax_maps",
        ["tax_rate_id"],
    )
    op.create_index(
        "ix_accounting_tax_maps_tenant_provider_active",
        "accounting_tax_maps",
        ["tenant_id", "provider", "is_active"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_accounting_tax_maps_tenant_provider_active",
        table_name="accounting_tax_maps",
    )
    op.drop_index(
        "ix_accounting_tax_maps_tax_rate_id",
        table_name="accounting_tax_maps",
    )
    op.drop_index(
        "ix_accounting_tax_maps_tenant_id",
        table_name="accounting_tax_maps",
    )
    op.drop_table("accounting_tax_maps")
