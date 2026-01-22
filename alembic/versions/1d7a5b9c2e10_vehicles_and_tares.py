"""vehicles and tares

Revision ID: 1d7a5b9c2e10
Revises: 5b4e2a1f6c39
Create Date: 2026-01-21 19:45:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "1d7a5b9c2e10"
down_revision = "5b4e2a1f6c39"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("vehicles") as batch_op:
        batch_op.add_column(sa.Column("owner_customer_id", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("overweight_threshold_kg", sa.Numeric(12, 3), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_vehicles_owner_customer_id",
            "customers",
            ["owner_customer_id"],
            ["id"],
        )

    op.create_table(
        "vehicle_tares",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("vehicle_id", sa.Integer(), sa.ForeignKey("vehicles.id"), nullable=False),
        sa.Column("container_id", sa.Integer(), sa.ForeignKey("containers.id"), nullable=False),
        sa.Column("tare_kg", sa.Numeric(12, 3), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("vehicle_tares")
    with op.batch_alter_table("vehicles") as batch_op:
        batch_op.drop_constraint("fk_vehicles_owner_customer_id", type_="foreignkey")
        batch_op.drop_column("overweight_threshold_kg")
        batch_op.drop_column("owner_customer_id")
