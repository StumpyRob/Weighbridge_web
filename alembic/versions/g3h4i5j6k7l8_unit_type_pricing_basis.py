"""add unit type and ticket pricing basis

Revision ID: g3h4i5j6k7l8
Revises: f2a3b4c5d6e7
Create Date: 2026-01-29 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "g3h4i5j6k7l8"
down_revision = "f2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "units" in table_names:
        columns = {col["name"] for col in inspector.get_columns("units")}
        unit_type_exists = "unit_type" in columns
        with op.batch_alter_table("units") as batch_op:
            if not unit_type_exists:
                batch_op.add_column(
                    sa.Column(
                        "unit_type",
                        sa.String(length=10),
                        nullable=False,
                        server_default="COUNT",
                    )
                )
                unit_type_exists = True
        if unit_type_exists:
            bind.execute(
                sa.text(
                    "UPDATE units SET unit_type = 'COUNT' "
                    "WHERE unit_type IS NULL OR unit_type = ''"
                )
            )

    if "tickets" in table_names:
        columns = {col["name"] for col in inspector.get_columns("tickets")}
        with op.batch_alter_table("tickets") as batch_op:
            if "pricing_basis" not in columns:
                batch_op.add_column(
                    sa.Column("pricing_basis", sa.String(length=10), nullable=True)
                )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "tickets" in table_names:
        columns = {col["name"] for col in inspector.get_columns("tickets")}
        with op.batch_alter_table("tickets") as batch_op:
            if "pricing_basis" in columns:
                batch_op.drop_column("pricing_basis")

    if "units" in table_names:
        columns = {col["name"] for col in inspector.get_columns("units")}
        with op.batch_alter_table("units") as batch_op:
            if "unit_type" in columns:
                batch_op.drop_column("unit_type")
