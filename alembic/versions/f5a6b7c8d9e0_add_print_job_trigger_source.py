"""add print job trigger source

Revision ID: f5a6b7c8d9e0
Revises: f4e5f6a7b8c0
Create Date: 2026-03-31 17:05:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "f5a6b7c8d9e0"
down_revision = "f4e5f6a7b8c0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("print_jobs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "trigger_source",
                sa.String(length=32),
                nullable=False,
                server_default="MANUAL",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("print_jobs") as batch_op:
        batch_op.drop_column("trigger_source")
