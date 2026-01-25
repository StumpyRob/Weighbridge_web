"""ticket sequences

Revision ID: 7d1b3e5f9a02
Revises: 3c9a7b2e1f04
Create Date: 2026-01-22 18:20:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "7d1b3e5f9a02"
down_revision = "3c9a7b2e1f04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ticket_sequences",
        sa.Column("year", sa.Integer(), primary_key=True),
        sa.Column("last_number", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("ticket_sequences")
