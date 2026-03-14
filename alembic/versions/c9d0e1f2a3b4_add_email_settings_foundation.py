"""add email settings foundation

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-03-14 22:15:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "c9d0e1f2a3b4"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("platform_settings") as batch_op:
        batch_op.add_column(sa.Column("smtp_host", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("smtp_port", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("smtp_username", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("smtp_password", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("smtp_from_email", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("smtp_from_display_name", sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column("smtp_reply_to", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("smtp_security", sa.String(length=16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("platform_settings") as batch_op:
        batch_op.drop_column("smtp_security")
        batch_op.drop_column("smtp_reply_to")
        batch_op.drop_column("smtp_from_display_name")
        batch_op.drop_column("smtp_from_email")
        batch_op.drop_column("smtp_password")
        batch_op.drop_column("smtp_username")
        batch_op.drop_column("smtp_port")
        batch_op.drop_column("smtp_host")
