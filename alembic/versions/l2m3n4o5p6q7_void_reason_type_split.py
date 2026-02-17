"""separate ticket and invoice void reasons by type

Revision ID: l2m3n4o5p6q7
Revises: k1l2m3n4o5p6
Create Date: 2026-02-13 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "l2m3n4o5p6q7"
down_revision = "k1l2m3n4o5p6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("void_reasons") as batch_op:
        batch_op.add_column(
            sa.Column(
                "reason_type",
                sa.String(length=20),
                nullable=False,
                server_default="TICKET",
            )
        )

    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE void_reasons "
            "SET reason_type = 'INVOICE' "
            "WHERE lower(trim(code)) = 'duplicate invoice'"
        )
    )

    with op.batch_alter_table("void_reasons") as batch_op:
        batch_op.alter_column("reason_type", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("void_reasons") as batch_op:
        batch_op.drop_column("reason_type")
