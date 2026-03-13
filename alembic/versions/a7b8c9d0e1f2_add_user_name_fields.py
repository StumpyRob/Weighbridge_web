"""add user name fields

Revision ID: a7b8c9d0e1f2
Revises: d4f7a8b9c0d1
Create Date: 2026-03-13 15:20:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a7b8c9d0e1f2"
down_revision = "d4f7a8b9c0d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("first_name", sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column("last_name", sa.String(length=100), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("last_name")
        batch_op.drop_column("first_name")
