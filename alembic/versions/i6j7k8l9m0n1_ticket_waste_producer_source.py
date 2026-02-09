"""add ticket waste producer source

Revision ID: i6j7k8l9m0n1
Revises: h4i5j6k7l8m9
Create Date: 2026-02-09 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "i6j7k8l9m0n1"
down_revision = "h4i5j6k7l8m9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tickets") as batch_op:
        batch_op.add_column(
            sa.Column("waste_producer_source", sa.String(length=20), nullable=True)
        )

    op.execute(
        "UPDATE tickets SET waste_producer_source = 'CUSTOMER' "
        "WHERE waste_producer_customer_id IS NOT NULL"
    )
    op.execute(
        "UPDATE tickets SET waste_producer_source = 'MANUAL' "
        "WHERE waste_producer_source IS NULL "
        "AND (COALESCE(waste_producer_name, '') <> '' "
        "OR COALESCE(waste_producer_address, '') <> '')"
    )


def downgrade() -> None:
    with op.batch_alter_table("tickets") as batch_op:
        batch_op.drop_column("waste_producer_source")
