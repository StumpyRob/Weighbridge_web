"""add platform qz credentials

Revision ID: e8f9a0b1c2d3
Revises: d2e3f4a5b6c7
Create Date: 2026-03-27 00:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "e8f9a0b1c2d3"
down_revision = "d2e3f4a5b6c7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("platform_settings") as batch_op:
        batch_op.add_column(sa.Column("qz_certificate_encrypted", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("qz_private_key_encrypted", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("qz_certificate_updated_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("qz_private_key_updated_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("platform_settings") as batch_op:
        batch_op.drop_column("qz_private_key_updated_at")
        batch_op.drop_column("qz_certificate_updated_at")
        batch_op.drop_column("qz_private_key_encrypted")
        batch_op.drop_column("qz_certificate_encrypted")
