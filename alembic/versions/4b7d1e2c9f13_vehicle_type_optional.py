"""vehicle type optional

Revision ID: 4b7d1e2c9f13
Revises: 2e6f8a1c9b44
Create Date: 2026-01-21 22:05:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "4b7d1e2c9f13"
down_revision = "2e6f8a1c9b44"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("vehicles") as batch_op:
        batch_op.alter_column(
            "vehicle_type_id", existing_type=sa.Integer(), nullable=True
        )


def downgrade() -> None:
    with op.batch_alter_table("vehicles") as batch_op:
        batch_op.alter_column(
            "vehicle_type_id", existing_type=sa.Integer(), nullable=False
        )
