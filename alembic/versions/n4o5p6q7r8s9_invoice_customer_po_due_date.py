"""customer invoiceability fields and invoice due date

Revision ID: n4o5p6q7r8s9
Revises: m3n4o5p6q7r8
Create Date: 2026-02-13 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "n4o5p6q7r8s9"
down_revision = "m3n4o5p6q7r8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("customers") as batch_op:
        batch_op.add_column(sa.Column("payment_terms_days", sa.Integer(), nullable=True))

    with op.batch_alter_table("tickets") as batch_op:
        batch_op.add_column(sa.Column("po_number", sa.String(length=100), nullable=True))

    with op.batch_alter_table("invoices") as batch_op:
        batch_op.add_column(sa.Column("due_date", sa.Date(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("invoices") as batch_op:
        batch_op.drop_column("due_date")

    with op.batch_alter_table("tickets") as batch_op:
        batch_op.drop_column("po_number")

    with op.batch_alter_table("customers") as batch_op:
        batch_op.drop_column("payment_terms_days")
