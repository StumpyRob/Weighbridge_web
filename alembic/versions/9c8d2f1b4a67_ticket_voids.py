"""ticket voids

Revision ID: 9c8d2f1b4a67
Revises: 7a1c4f2d8e02
Create Date: 2026-01-21 18:05:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "9c8d2f1b4a67"
down_revision = "7a1c4f2d8e02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ticket_voids",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ticket_id", sa.Integer(), sa.ForeignKey("tickets.id"), nullable=False),
        sa.Column("reason_id", sa.Integer(), sa.ForeignKey("void_reasons.id"), nullable=False),
        sa.Column("note", sa.String(length=255), nullable=False),
        sa.Column("voided_at", sa.DateTime(), nullable=False),
        sa.Column("voided_by", sa.String(length=150), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("ticket_voids")
