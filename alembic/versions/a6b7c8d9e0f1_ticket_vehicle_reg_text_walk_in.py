"""ticket vehicle reg text + walk in

Revision ID: a6b7c8d9e0f1
Revises: f1b2c3d4e5f6
Create Date: 2026-01-28 10:10:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "a6b7c8d9e0f1"
down_revision = "f1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tickets") as batch_op:
        batch_op.add_column(
            sa.Column("vehicle_reg_text", sa.String(length=50), nullable=True)
        )
        batch_op.add_column(
            sa.Column("walk_in", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.create_index(
            "ix_tickets_vehicle_reg_text", ["vehicle_reg_text"]
        )


def downgrade() -> None:
    with op.batch_alter_table("tickets") as batch_op:
        batch_op.drop_index("ix_tickets_vehicle_reg_text")
        batch_op.drop_column("walk_in")
        batch_op.drop_column("vehicle_reg_text")
