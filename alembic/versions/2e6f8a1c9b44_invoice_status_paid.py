"""invoice status paid

Revision ID: 2e6f8a1c9b44
Revises: 6c3e1a9d4b20
Create Date: 2026-01-21 21:35:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "2e6f8a1c9b44"
down_revision = "6c3e1a9d4b20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("invoices") as batch_op:
        batch_op.add_column(sa.Column("payment_method_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("paid_at", sa.DateTime(), nullable=True))
        batch_op.create_foreign_key(
            "fk_invoices_payment_method_id",
            "payment_methods",
            ["payment_method_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("invoices") as batch_op:
        batch_op.drop_constraint("fk_invoices_payment_method_id", type_="foreignkey")
        batch_op.drop_column("paid_at")
        batch_op.drop_column("payment_method_id")
