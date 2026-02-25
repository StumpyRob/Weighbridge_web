"""vehicle default driver lookup

Revision ID: 1a2b3c4d5e6f
Revises: e7f8a9b0c1d2
Create Date: 2026-02-25 15:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "1a2b3c4d5e6f"
down_revision = "e7f8a9b0c1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("vehicles") as batch_op:
        batch_op.add_column(sa.Column("default_driver_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_vehicles_default_driver_id",
            "drivers",
            ["default_driver_id"],
            ["id"],
        )
        batch_op.create_index("ix_vehicles_default_driver_id", ["default_driver_id"])


def downgrade() -> None:
    with op.batch_alter_table("vehicles") as batch_op:
        batch_op.drop_index("ix_vehicles_default_driver_id")
        batch_op.drop_constraint("fk_vehicles_default_driver_id", type_="foreignkey")
        batch_op.drop_column("default_driver_id")
