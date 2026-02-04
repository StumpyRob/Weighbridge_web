"""add product default destination

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-01-29 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "e2f3a4b5c6d7"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    if "products" not in table_names:
        return

    columns = {col["name"] for col in inspector.get_columns("products")}

    with op.batch_alter_table("products") as batch_op:
        if "default_destination_id" not in columns:
            batch_op.add_column(
                sa.Column("default_destination_id", sa.Integer(), nullable=True)
            )
            batch_op.create_foreign_key(
                "fk_products_default_destination_id",
                "destinations",
                ["default_destination_id"],
                ["id"],
                ondelete="SET NULL",
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    if "products" not in table_names:
        return

    columns = {col["name"] for col in inspector.get_columns("products")}
    fk_names = {}
    for fk in inspector.get_foreign_keys("products"):
        name = fk.get("name")
        cols = fk.get("constrained_columns") or []
        for col in cols:
            fk_names[col] = name

    with op.batch_alter_table("products") as batch_op:
        if "default_destination_id" in columns:
            fk_name = fk_names.get("default_destination_id")
            if fk_name:
                batch_op.drop_constraint(fk_name, type_="foreignkey")
            batch_op.drop_column("default_destination_id")
