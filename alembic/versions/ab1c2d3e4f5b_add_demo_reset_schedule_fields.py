"""add demo reset schedule fields to tenants

Revision ID: ab1c2d3e4f5b
Revises: 8f0a1b2c3d4e
Create Date: 2026-03-24 16:40:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "ab1c2d3e4f5b"
down_revision = "8f0a1b2c3d4e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("demo_reset_interval_days", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("demo_last_reset_at", sa.DateTime(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.drop_column("demo_last_reset_at")
        batch_op.drop_column("demo_reset_interval_days")
