"""add customer adjustments table for auditable credit overrides

Revision ID: d3e4f5a6b7c8
Revises: veh_def_driver_fix
Create Date: 2026-02-26 23:15:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "d3e4f5a6b7c8"
down_revision = "veh_def_driver_fix"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    if "customer_adjustments" in table_names:
        return

    op.create_table(
        "customer_adjustments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("amount_decimal", sa.Numeric(12, 2), nullable=False),
        sa.Column("reason", sa.String(length=50), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_customer_adjustments_customer_id",
        "customer_adjustments",
        ["customer_id"],
        unique=False,
    )
    op.create_index(
        "ix_customer_adjustments_created_at",
        "customer_adjustments",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    if "customer_adjustments" not in table_names:
        return

    existing_indexes = {
        index["name"] for index in inspector.get_indexes("customer_adjustments")
    }
    if "ix_customer_adjustments_created_at" in existing_indexes:
        op.drop_index(
            "ix_customer_adjustments_created_at",
            table_name="customer_adjustments",
        )
    if "ix_customer_adjustments_customer_id" in existing_indexes:
        op.drop_index(
            "ix_customer_adjustments_customer_id",
            table_name="customer_adjustments",
        )
    op.drop_table("customer_adjustments")
