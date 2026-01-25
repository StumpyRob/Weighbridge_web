"""invoice voids

Revision ID: 1f3c9a2b7d50
Revises: 5e8c1d2f7a34
Create Date: 2026-01-22 20:05:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "1f3c9a2b7d50"
down_revision = "5e8c1d2f7a34"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "invoice_voids",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("invoice_id", sa.Integer(), sa.ForeignKey("invoices.id"), nullable=False),
        sa.Column("reason_id", sa.Integer(), sa.ForeignKey("void_reasons.id"), nullable=False),
        sa.Column("note", sa.String(length=255), nullable=False),
        sa.Column("voided_at", sa.DateTime(), nullable=False),
        sa.Column("voided_by", sa.String(length=150), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("invoice_voids")
