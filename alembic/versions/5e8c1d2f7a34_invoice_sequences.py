"""invoice sequences

Revision ID: 5e8c1d2f7a34
Revises: 7d1b3e5f9a02
Create Date: 2026-01-22 19:10:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "5e8c1d2f7a34"
down_revision = "7d1b3e5f9a02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "invoice_sequences",
        sa.Column("year", sa.Integer(), primary_key=True),
        sa.Column("last_number", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("invoice_sequences")
