"""global field limits and deterministic truncation

NOTE:
This migration is lossy for overlength values. It deterministically truncates
data with `substr(..., 1, max_len)` before narrowing column lengths.

Revision ID: q7r8s9t0u1v2
Revises: p6q7r8s9t0u1
Create Date: 2026-02-17 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "q7r8s9t0u1v2"
down_revision = "p6q7r8s9t0u1"
branch_labels = None
depends_on = None


def _truncate_column(table: str, column: str, max_len: int) -> None:
    # Lossy by design: enforce deterministic left-truncation before narrowing.
    op.execute(
        sa.text(
            f"""
            UPDATE {table}
            SET {column} = substr({column}, 1, :max_len)
            WHERE {column} IS NOT NULL
              AND length({column}) > :max_len
            """
        ).bindparams(max_len=max_len)
    )


def upgrade() -> None:
    _truncate_column("customers", "name", 120)
    _truncate_column("customers", "address_line1", 120)
    _truncate_column("customers", "address_line2", 120)
    _truncate_column("customers", "postcode", 16)
    _truncate_column("products", "nominal_code", 20)
    _truncate_column("product_groups", "nominal_code_default", 20)
    _truncate_column("vehicles", "registration", 15)
    _truncate_column("hauliers", "carrier_licence_number", 50)
    _truncate_column("tickets", "vehicle_reg_text", 15)
    _truncate_column("tickets", "carrier_licence_number", 50)
    _truncate_column("tickets", "waste_producer_name", 120)
    _truncate_column("tickets", "waste_producer_address", 1000)
    _truncate_column("tickets", "po_number", 50)
    _truncate_column("items", "name", 120)
    _truncate_column("waste_producers", "name", 120)
    _truncate_column("recyclers", "name", 120)
    _truncate_column("suppliers", "name", 120)
    _truncate_column("contractors", "name", 120)

    with op.batch_alter_table("customers") as batch_op:
        batch_op.alter_column(
            "name",
            existing_type=sa.String(length=255),
            type_=sa.String(length=120),
        )
        batch_op.alter_column(
            "address_line1",
            existing_type=sa.String(length=255),
            type_=sa.String(length=120),
        )
        batch_op.alter_column(
            "address_line2",
            existing_type=sa.String(length=255),
            type_=sa.String(length=120),
        )
        batch_op.alter_column(
            "city",
            existing_type=sa.String(length=100),
            type_=sa.String(length=120),
        )
        batch_op.alter_column(
            "postcode",
            existing_type=sa.String(length=50),
            type_=sa.String(length=16),
        )
        batch_op.alter_column(
            "country",
            existing_type=sa.String(length=100),
            type_=sa.String(length=120),
        )
        batch_op.alter_column(
            "payment_terms",
            existing_type=sa.String(length=100),
            type_=sa.String(length=120),
        )

    with op.batch_alter_table("products") as batch_op:
        batch_op.alter_column(
            "nominal_code",
            existing_type=sa.String(length=50),
            type_=sa.String(length=20),
        )

    with op.batch_alter_table("product_groups") as batch_op:
        batch_op.alter_column(
            "nominal_code_default",
            existing_type=sa.String(length=50),
            type_=sa.String(length=20),
        )

    with op.batch_alter_table("vehicles") as batch_op:
        batch_op.alter_column(
            "registration",
            existing_type=sa.String(length=50),
            type_=sa.String(length=15),
        )

    with op.batch_alter_table("hauliers") as batch_op:
        batch_op.alter_column(
            "carrier_licence_number",
            existing_type=sa.String(length=100),
            type_=sa.String(length=50),
        )

    with op.batch_alter_table("units") as batch_op:
        batch_op.alter_column(
            "name",
            existing_type=sa.String(length=50),
            type_=sa.String(length=120),
        )

    with op.batch_alter_table("waste_producers") as batch_op:
        batch_op.alter_column(
            "name",
            existing_type=sa.String(length=150),
            type_=sa.String(length=120),
        )

    with op.batch_alter_table("recyclers") as batch_op:
        batch_op.alter_column(
            "name",
            existing_type=sa.String(length=150),
            type_=sa.String(length=120),
        )

    with op.batch_alter_table("suppliers") as batch_op:
        batch_op.alter_column(
            "name",
            existing_type=sa.String(length=150),
            type_=sa.String(length=120),
        )

    with op.batch_alter_table("contractors") as batch_op:
        batch_op.alter_column(
            "name",
            existing_type=sa.String(length=150),
            type_=sa.String(length=120),
        )

    with op.batch_alter_table("items") as batch_op:
        batch_op.alter_column(
            "name",
            existing_type=sa.String(length=255),
            type_=sa.String(length=120),
        )

    with op.batch_alter_table("tickets") as batch_op:
        batch_op.alter_column(
            "vehicle_reg_text",
            existing_type=sa.String(length=50),
            type_=sa.String(length=15),
        )
        batch_op.alter_column(
            "carrier_licence_number",
            existing_type=sa.String(length=100),
            type_=sa.String(length=50),
        )
        batch_op.alter_column(
            "waste_producer_name",
            existing_type=sa.String(length=255),
            type_=sa.String(length=120),
        )
        batch_op.alter_column(
            "waste_producer_address",
            existing_type=sa.Text(),
            type_=sa.String(length=1000),
        )
        batch_op.alter_column(
            "pricing_unit_name",
            existing_type=sa.String(length=50),
            type_=sa.String(length=120),
        )
        batch_op.alter_column(
            "po_number",
            existing_type=sa.String(length=100),
            type_=sa.String(length=50),
        )

    with op.batch_alter_table("ticket_voids") as batch_op:
        batch_op.alter_column(
            "note",
            existing_type=sa.String(length=255),
            type_=sa.String(length=1000),
        )

    with op.batch_alter_table("invoice_voids") as batch_op:
        batch_op.alter_column(
            "note",
            existing_type=sa.String(length=255),
            type_=sa.String(length=1000),
        )


def downgrade() -> None:
    with op.batch_alter_table("invoice_voids") as batch_op:
        batch_op.alter_column(
            "note",
            existing_type=sa.String(length=1000),
            type_=sa.String(length=255),
        )

    with op.batch_alter_table("ticket_voids") as batch_op:
        batch_op.alter_column(
            "note",
            existing_type=sa.String(length=1000),
            type_=sa.String(length=255),
        )

    with op.batch_alter_table("tickets") as batch_op:
        batch_op.alter_column(
            "po_number",
            existing_type=sa.String(length=50),
            type_=sa.String(length=100),
        )
        batch_op.alter_column(
            "pricing_unit_name",
            existing_type=sa.String(length=120),
            type_=sa.String(length=50),
        )
        batch_op.alter_column(
            "waste_producer_address",
            existing_type=sa.String(length=1000),
            type_=sa.Text(),
        )
        batch_op.alter_column(
            "waste_producer_name",
            existing_type=sa.String(length=120),
            type_=sa.String(length=255),
        )
        batch_op.alter_column(
            "carrier_licence_number",
            existing_type=sa.String(length=50),
            type_=sa.String(length=100),
        )
        batch_op.alter_column(
            "vehicle_reg_text",
            existing_type=sa.String(length=15),
            type_=sa.String(length=50),
        )

    with op.batch_alter_table("contractors") as batch_op:
        batch_op.alter_column(
            "name",
            existing_type=sa.String(length=120),
            type_=sa.String(length=150),
        )

    with op.batch_alter_table("items") as batch_op:
        batch_op.alter_column(
            "name",
            existing_type=sa.String(length=120),
            type_=sa.String(length=255),
        )

    with op.batch_alter_table("suppliers") as batch_op:
        batch_op.alter_column(
            "name",
            existing_type=sa.String(length=120),
            type_=sa.String(length=150),
        )

    with op.batch_alter_table("recyclers") as batch_op:
        batch_op.alter_column(
            "name",
            existing_type=sa.String(length=120),
            type_=sa.String(length=150),
        )

    with op.batch_alter_table("waste_producers") as batch_op:
        batch_op.alter_column(
            "name",
            existing_type=sa.String(length=120),
            type_=sa.String(length=150),
        )

    with op.batch_alter_table("units") as batch_op:
        batch_op.alter_column(
            "name",
            existing_type=sa.String(length=120),
            type_=sa.String(length=50),
        )

    with op.batch_alter_table("hauliers") as batch_op:
        batch_op.alter_column(
            "carrier_licence_number",
            existing_type=sa.String(length=50),
            type_=sa.String(length=100),
        )

    with op.batch_alter_table("vehicles") as batch_op:
        batch_op.alter_column(
            "registration",
            existing_type=sa.String(length=15),
            type_=sa.String(length=50),
        )

    with op.batch_alter_table("product_groups") as batch_op:
        batch_op.alter_column(
            "nominal_code_default",
            existing_type=sa.String(length=20),
            type_=sa.String(length=50),
        )

    with op.batch_alter_table("products") as batch_op:
        batch_op.alter_column(
            "nominal_code",
            existing_type=sa.String(length=20),
            type_=sa.String(length=50),
        )

    with op.batch_alter_table("customers") as batch_op:
        batch_op.alter_column(
            "payment_terms",
            existing_type=sa.String(length=120),
            type_=sa.String(length=100),
        )
        batch_op.alter_column(
            "country",
            existing_type=sa.String(length=120),
            type_=sa.String(length=100),
        )
        batch_op.alter_column(
            "postcode",
            existing_type=sa.String(length=16),
            type_=sa.String(length=50),
        )
        batch_op.alter_column(
            "city",
            existing_type=sa.String(length=120),
            type_=sa.String(length=100),
        )
        batch_op.alter_column(
            "address_line2",
            existing_type=sa.String(length=120),
            type_=sa.String(length=255),
        )
        batch_op.alter_column(
            "address_line1",
            existing_type=sa.String(length=120),
            type_=sa.String(length=255),
        )
        batch_op.alter_column(
            "name",
            existing_type=sa.String(length=120),
            type_=sa.String(length=255),
        )
