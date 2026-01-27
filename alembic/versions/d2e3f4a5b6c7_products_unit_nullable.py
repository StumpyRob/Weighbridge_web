"""products unit nullable

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-01-26 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "d2e3f4a5b6c7"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("products") as batch_op:
        batch_op.alter_column(
            "unit_id",
            existing_type=sa.Integer(),
            nullable=True,
        )
    op.create_index("ix_products_unit_id", "products", ["unit_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_products_unit_id", table_name="products")
    with op.batch_alter_table("products") as batch_op:
        batch_op.alter_column(
            "unit_id",
            existing_type=sa.Integer(),
            nullable=False,
        )
