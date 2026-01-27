"""product unit required

Revision ID: 3c9a7b2e1f04
Revises: 0a9b3d5c7e21
Create Date: 2026-01-22 14:30:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "3c9a7b2e1f04"
down_revision = "0a9b3d5c7e21"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    tonne_row = conn.execute(
        sa.text("SELECT id FROM units WHERE name = 'Tonne' LIMIT 1")
    ).fetchone()
    null_count = conn.execute(
        sa.text("SELECT COUNT(*) FROM products WHERE unit_id IS NULL")
    ).scalar()

    if null_count and null_count > 0:
        if tonne_row is None:
            raise RuntimeError(
                "Products have null unit_id but Tonne unit is missing. "
                "Run `python -m app.seed` before applying this migration."
            )
        conn.execute(
            sa.text("UPDATE products SET unit_id = :unit_id WHERE unit_id IS NULL"),
            {"unit_id": tonne_row[0]},
        )

    with op.batch_alter_table("products") as batch_op:
        batch_op.alter_column(
            "unit_id", existing_type=sa.Integer(), nullable=False
        )


def downgrade() -> None:
    with op.batch_alter_table("products") as batch_op:
        batch_op.alter_column("unit_id", existing_type=sa.Integer(), nullable=True)
