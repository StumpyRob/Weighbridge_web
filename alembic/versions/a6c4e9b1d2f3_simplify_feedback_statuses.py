"""simplify feedback statuses

Revision ID: a6c4e9b1d2f3
Revises: f7b8c9d0e1f1
Create Date: 2026-04-10 00:25:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a6c4e9b1d2f3"
down_revision = "f7b8c9d0e1f1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE user_feedback "
        "SET status = 'read' "
        "WHERE lower(trim(status)) IN ('reviewed', 'closed')"
    )
    with op.batch_alter_table("user_feedback", schema=None) as batch_op:
        batch_op.drop_column("source_title")


def downgrade() -> None:
    with op.batch_alter_table("user_feedback", schema=None) as batch_op:
        batch_op.add_column(sa.Column("source_title", sa.String(length=255), nullable=True))
