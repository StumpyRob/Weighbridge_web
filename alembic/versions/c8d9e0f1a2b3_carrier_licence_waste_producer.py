"""carrier licence + waste producer customer

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
Create Date: 2026-01-29 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "c8d9e0f1a2b3"
down_revision = "b7c8d9e0f1a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("hauliers") as batch_op:
        batch_op.add_column(
            sa.Column("carrier_licence_number", sa.String(length=100))
        )

    with op.batch_alter_table("tickets") as batch_op:
        batch_op.add_column(
            sa.Column("carrier_licence_number", sa.String(length=100))
        )
        batch_op.add_column(sa.Column("waste_producer_customer_id", sa.Integer()))
        batch_op.add_column(sa.Column("waste_producer_name", sa.String(length=255)))
        batch_op.add_column(sa.Column("waste_producer_address", sa.Text()))
        batch_op.create_foreign_key(
            "fk_tickets_waste_producer_customer_id",
            "customers",
            ["waste_producer_customer_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("tickets") as batch_op:
        batch_op.drop_constraint(
            "fk_tickets_waste_producer_customer_id", type_="foreignkey"
        )
        batch_op.drop_column("waste_producer_address")
        batch_op.drop_column("waste_producer_name")
        batch_op.drop_column("waste_producer_customer_id")
        batch_op.drop_column("carrier_licence_number")

    with op.batch_alter_table("hauliers") as batch_op:
        batch_op.drop_column("carrier_licence_number")
