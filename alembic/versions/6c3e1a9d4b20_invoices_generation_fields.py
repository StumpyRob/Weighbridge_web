"""invoices generation fields

Revision ID: 6c3e1a9d4b20
Revises: 8f2b6d1c3a90
Create Date: 2026-01-21 21:05:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "6c3e1a9d4b20"
down_revision = "8f2b6d1c3a90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tax_rates") as batch_op:
        batch_op.add_column(sa.Column("rate_percent", sa.Numeric(6, 3), nullable=True))

    with op.batch_alter_table("tickets") as batch_op:
        batch_op.add_column(sa.Column("invoice_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_tickets_invoice_id", "invoices", ["invoice_id"], ["id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("tickets") as batch_op:
        batch_op.drop_constraint("fk_tickets_invoice_id", type_="foreignkey")
        batch_op.drop_column("invoice_id")

    with op.batch_alter_table("tax_rates") as batch_op:
        batch_op.drop_column("rate_percent")
