"""mvp models

Revision ID: 3f2a9b7c4d11
Revises: 96f1234044ae
Create Date: 2026-01-21 17:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "3f2a9b7c4d11"
down_revision = "96f1234044ae"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(length=150), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "customers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_code", sa.String(length=50), nullable=False, unique=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("invoice_email", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "vehicles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("registration", sa.String(length=50), nullable=False, unique=True),
        sa.Column("default_tare_kg", sa.Numeric(12, 3), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=50), nullable=False, unique=True),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column("unit_id", sa.Integer(), nullable=True),
        sa.Column("tax_rate_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "tickets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ticket_no", sa.String(length=50), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("datetime", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("direction", sa.String(length=50), nullable=False),
        sa.Column("transaction_type", sa.String(length=50), nullable=False),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id")),
        sa.Column("vehicle_id", sa.Integer(), sa.ForeignKey("vehicles.id")),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id")),
        sa.Column("gross_kg", sa.Numeric(12, 3), nullable=True),
        sa.Column("tare_kg", sa.Numeric(12, 3), nullable=True),
        sa.Column("net_kg", sa.Numeric(12, 3), nullable=True),
        sa.Column("qty", sa.Numeric(12, 3), nullable=True),
        sa.Column("unit_id", sa.Integer(), nullable=True),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("total", sa.Numeric(12, 2), nullable=True),
        sa.Column("dont_invoice", sa.Boolean(), nullable=False),
        sa.Column("paid", sa.Boolean(), nullable=False),
        sa.Column("payment_method_id", sa.Integer(), nullable=True),
    )
    op.create_index("ix_tickets_datetime", "tickets", ["datetime"])
    op.create_index("ix_tickets_status", "tickets", ["status"])
    op.create_index("ix_tickets_customer_id", "tickets", ["customer_id"])
    op.create_index("ix_tickets_vehicle_id", "tickets", ["vehicle_id"])
    op.create_table(
        "invoices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("invoice_no", sa.String(length=50), nullable=False, unique=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("invoice_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("net_total", sa.Numeric(12, 2), nullable=False),
        sa.Column("vat_total", sa.Numeric(12, 2), nullable=False),
        sa.Column("gross_total", sa.Numeric(12, 2), nullable=False),
    )
    op.create_table(
        "invoice_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("invoice_id", sa.Integer(), sa.ForeignKey("invoices.id"), nullable=False),
        sa.Column("ticket_id", sa.Integer(), sa.ForeignKey("tickets.id")),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column("quantity", sa.Numeric(12, 3), nullable=False),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("net", sa.Numeric(12, 2), nullable=False),
        sa.Column("vat", sa.Numeric(12, 2), nullable=False),
        sa.Column("gross", sa.Numeric(12, 2), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("invoice_lines")
    op.drop_table("invoices")
    op.drop_index("ix_tickets_vehicle_id", table_name="tickets")
    op.drop_index("ix_tickets_customer_id", table_name="tickets")
    op.drop_index("ix_tickets_status", table_name="tickets")
    op.drop_index("ix_tickets_datetime", table_name="tickets")
    op.drop_table("tickets")
    op.drop_table("products")
    op.drop_table("vehicles")
    op.drop_table("customers")
    op.drop_table("users")
