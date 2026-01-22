"""customer fields

Revision ID: 5b4e2a1f6c39
Revises: 9c8d2f1b4a67
Create Date: 2026-01-21 19:10:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "5b4e2a1f6c39"
down_revision = "9c8d2f1b4a67"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("customers") as batch_op:
        batch_op.add_column(sa.Column("phone", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("address_line1", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("address_line2", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("city", sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column("postcode", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("country", sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column("vat_number", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("invoice_frequency_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("payment_terms", sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column("credit_limit", sa.Numeric(12, 2), nullable=True))
        batch_op.add_column(
            sa.Column("on_stop", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(
            sa.Column(
                "cash_account", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
        batch_op.add_column(
            sa.Column(
                "do_not_invoice", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
        batch_op.add_column(
            sa.Column(
                "must_have_po", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
        batch_op.create_foreign_key(
            "fk_customers_invoice_frequency_id",
            "invoice_frequencies",
            ["invoice_frequency_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("customers") as batch_op:
        batch_op.drop_constraint(
            "fk_customers_invoice_frequency_id", type_="foreignkey"
        )
        batch_op.drop_column("must_have_po")
        batch_op.drop_column("do_not_invoice")
        batch_op.drop_column("cash_account")
        batch_op.drop_column("on_stop")
        batch_op.drop_column("credit_limit")
        batch_op.drop_column("payment_terms")
        batch_op.drop_column("invoice_frequency_id")
        batch_op.drop_column("vat_number")
        batch_op.drop_column("country")
        batch_op.drop_column("postcode")
        batch_op.drop_column("city")
        batch_op.drop_column("address_line2")
        batch_op.drop_column("address_line1")
        batch_op.drop_column("phone")
