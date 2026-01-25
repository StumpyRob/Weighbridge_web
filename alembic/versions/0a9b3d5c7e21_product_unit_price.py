"""product unit price

Revision ID: 0a9b3d5c7e21
Revises: 4b7d1e2c9f13
Create Date: 2026-01-22 13:45:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "0a9b3d5c7e21"
down_revision = "4b7d1e2c9f13"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("products") as batch_op:
        batch_op.add_column(sa.Column("unit_price", sa.Numeric(12, 2), nullable=True))

    op.execute("UPDATE products SET unit_price = 0.00 WHERE unit_price IS NULL")

    with op.batch_alter_table("products") as batch_op:
        batch_op.alter_column(
            "unit_price", existing_type=sa.Numeric(12, 2), nullable=False
        )


def downgrade() -> None:
    with op.batch_alter_table("products") as batch_op:
        batch_op.drop_column("unit_price")
