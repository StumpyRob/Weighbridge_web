"""add print agent printer inventory snapshot

Revision ID: f4e5f6a7b8c0
Revises: f3d4e5f6a7b9
Create Date: 2026-03-31 16:10:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "f4e5f6a7b8c0"
down_revision = "f3d4e5f6a7b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("print_agents") as batch_op:
        batch_op.add_column(sa.Column("printers_json", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("printers_synced_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("print_agents") as batch_op:
        batch_op.drop_column("printers_synced_at")
        batch_op.drop_column("printers_json")
