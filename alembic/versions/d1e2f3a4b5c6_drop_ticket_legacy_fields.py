"""drop legacy ticket fields

Revision ID: d1e2f3a4b5c6
Revises: c8d9e0f1a2b3
Create Date: 2026-01-29 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "d1e2f3a4b5c6"
down_revision = "c8d9e0f1a2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    if "tickets" not in table_names:
        return

    columns = {col["name"] for col in inspector.get_columns("tickets")}
    fk_names = {}
    for fk in inspector.get_foreign_keys("tickets"):
        name = fk.get("name")
        cols = fk.get("constrained_columns") or []
        for col in cols:
            fk_names[col] = name

    with op.batch_alter_table("tickets") as batch_op:
        for col in ("waste_producer_id", "licence_id"):
            if col in columns:
                fk_name = fk_names.get(col)
                if fk_name:
                    batch_op.drop_constraint(fk_name, type_="foreignkey")
                batch_op.drop_column(col)


def downgrade() -> None:
    with op.batch_alter_table("tickets") as batch_op:
        batch_op.add_column(
            sa.Column("licence_id", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("waste_producer_id", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_tickets_licence_id",
            "licences",
            ["licence_id"],
            ["id"],
            ondelete=None,
        )
        batch_op.create_foreign_key(
            "fk_tickets_waste_producer_id",
            "waste_producers",
            ["waste_producer_id"],
            ["id"],
            ondelete=None,
        )
