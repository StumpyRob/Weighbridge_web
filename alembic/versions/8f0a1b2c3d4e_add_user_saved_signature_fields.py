"""add user saved signature fields

Revision ID: 8f0a1b2c3d4e
Revises: 7d8e9f0a1b2c
Create Date: 2026-03-23 20:20:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "8f0a1b2c3d4e"
down_revision = "7d8e9f0a1b2c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("saved_signature_data_uri", sa.Text(), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("saved_signature_signer_name", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("saved_signature_updated_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "saved_signature_updated_at")
    op.drop_column("users", "saved_signature_signer_name")
    op.drop_column("users", "saved_signature_data_uri")
