"""add ticket wtn signature fields

Revision ID: 3c4d5e6f7a8b
Revises: 2a3b4c5d6e7f
Create Date: 2026-03-22 21:05:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "3c4d5e6f7a8b"
down_revision = "2a3b4c5d6e7f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tickets", sa.Column("wtn_signature_data_uri", sa.Text(), nullable=True))
    op.add_column("tickets", sa.Column("wtn_signature_signed_at", sa.DateTime(), nullable=True))
    op.add_column(
        "tickets",
        sa.Column("wtn_signature_signer_name", sa.String(length=120), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tickets", "wtn_signature_signer_name")
    op.drop_column("tickets", "wtn_signature_signed_at")
    op.drop_column("tickets", "wtn_signature_data_uri")
