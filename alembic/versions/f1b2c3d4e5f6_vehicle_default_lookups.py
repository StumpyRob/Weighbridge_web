"""vehicle default lookups

Revision ID: f1b2c3d4e5f6
Revises: e4f5a6b7c8d9
Create Date: 2026-01-27 14:30:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "f1b2c3d4e5f6"
down_revision = "e4f5a6b7c8d9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("vehicles") as batch_op:
        batch_op.add_column(
            sa.Column("default_customer_id", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("default_haulier_id", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_vehicles_default_customer_id",
            "customers",
            ["default_customer_id"],
            ["id"],
        )
        batch_op.create_foreign_key(
            "fk_vehicles_default_haulier_id",
            "hauliers",
            ["default_haulier_id"],
            ["id"],
        )
        batch_op.create_index(
            "ix_vehicles_default_customer_id", ["default_customer_id"]
        )
        batch_op.create_index(
            "ix_vehicles_default_haulier_id", ["default_haulier_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("vehicles") as batch_op:
        batch_op.drop_index("ix_vehicles_default_haulier_id")
        batch_op.drop_index("ix_vehicles_default_customer_id")
        batch_op.drop_constraint(
            "fk_vehicles_default_haulier_id", type_="foreignkey"
        )
        batch_op.drop_constraint(
            "fk_vehicles_default_customer_id", type_="foreignkey"
        )
        batch_op.drop_column("default_haulier_id")
        batch_op.drop_column("default_customer_id")
