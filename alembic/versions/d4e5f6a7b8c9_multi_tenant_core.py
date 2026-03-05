"""multi tenant core

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-03-05 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


DEFAULT_TENANT_NAME = "Default"
DEFAULT_TENANT_SUBDOMAIN = "default"

TENANT_BACKFILL_TABLES: tuple[str, ...] = (
    "company_settings",
    "customers",
    "vehicles",
    "products",
    "tickets",
    "invoices",
    "customer_adjustments",
    "customer_product_prices",
    "invoice_lines",
    "invoice_voids",
    "ticket_voids",
    "vehicle_tares",
    "print_templates",
    "print_destinations",
    "print_template_versions",
    "print_jobs",
)

TENANT_NOT_NULL_TABLES: tuple[str, ...] = TENANT_BACKFILL_TABLES


def _create_tenants_table() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("subdomain", sa.String(length=63), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("subdomain", name="uq_tenants_subdomain"),
    )
    op.create_index("ix_tenants_is_active", "tenants", ["is_active"], unique=False)


def _ensure_default_tenant() -> int:
    conn = op.get_bind()
    existing = conn.execute(
        sa.text(
            "SELECT id FROM tenants WHERE lower(subdomain)=:subdomain ORDER BY id ASC LIMIT 1"
        ),
        {"subdomain": DEFAULT_TENANT_SUBDOMAIN},
    ).scalar_one_or_none()
    if existing is not None:
        return int(existing)

    conn.execute(
        sa.text(
            "INSERT INTO tenants (name, subdomain, is_active, created_at) "
            "VALUES (:name, :subdomain, :is_active, :created_at)"
        ),
        {
            "name": DEFAULT_TENANT_NAME,
            "subdomain": DEFAULT_TENANT_SUBDOMAIN,
            "is_active": True,
            "created_at": datetime.now(timezone.utc).replace(tzinfo=None),
        },
    )
    created = conn.execute(
        sa.text(
            "SELECT id FROM tenants WHERE lower(subdomain)=:subdomain ORDER BY id ASC LIMIT 1"
        ),
        {"subdomain": DEFAULT_TENANT_SUBDOMAIN},
    ).scalar_one()
    return int(created)


def _add_tenant_column(table_name: str, *, nullable: bool) -> None:
    with op.batch_alter_table(table_name, schema=None) as batch_op:
        batch_op.add_column(sa.Column("tenant_id", sa.Integer(), nullable=nullable))
        batch_op.create_foreign_key(
            f"fk_{table_name}_tenant_id_tenants",
            "tenants",
            ["tenant_id"],
            ["id"],
        )
        batch_op.create_index(f"ix_{table_name}_tenant_id", ["tenant_id"], unique=False)


def _backfill_tenant_id(table_names: Iterable[str], tenant_id: int) -> None:
    conn = op.get_bind()
    for table_name in table_names:
        conn.execute(
            sa.text(
                f"UPDATE {table_name} SET tenant_id=:tenant_id WHERE tenant_id IS NULL"
            ),
            {"tenant_id": tenant_id},
        )


def _enforce_not_null(table_names: Iterable[str]) -> None:
    for table_name in table_names:
        with op.batch_alter_table(table_name, schema=None) as batch_op:
            batch_op.alter_column("tenant_id", existing_type=sa.Integer(), nullable=False)


def _update_uniqueness_constraints() -> None:
    with op.batch_alter_table("customers", schema=None) as batch_op:
        batch_op.create_unique_constraint(
            "uq_customers_tenant_account_code",
            ["tenant_id", "account_code"],
        )

    with op.batch_alter_table("vehicles", schema=None) as batch_op:
        batch_op.create_unique_constraint(
            "uq_vehicles_tenant_registration",
            ["tenant_id", "registration"],
        )

    with op.batch_alter_table("products", schema=None) as batch_op:
        batch_op.create_unique_constraint(
            "uq_products_tenant_code",
            ["tenant_id", "code"],
        )

    with op.batch_alter_table("tickets", schema=None) as batch_op:
        batch_op.create_unique_constraint(
            "uq_tickets_tenant_ticket_no",
            ["tenant_id", "ticket_no"],
        )

    with op.batch_alter_table("invoices", schema=None) as batch_op:
        batch_op.create_unique_constraint(
            "uq_invoices_tenant_invoice_no",
            ["tenant_id", "invoice_no"],
        )

    with op.batch_alter_table("company_settings", schema=None) as batch_op:
        batch_op.create_unique_constraint(
            "uq_company_settings_tenant_id",
            ["tenant_id"],
        )

    with op.batch_alter_table("print_templates", schema=None) as batch_op:
        batch_op.drop_constraint("uq_print_templates_code", type_="unique")
        batch_op.create_unique_constraint(
            "uq_print_templates_tenant_code",
            ["tenant_id", "code"],
        )

    with op.batch_alter_table("print_destinations", schema=None) as batch_op:
        batch_op.drop_index("uq_print_destinations_default_active_doc_type")
        batch_op.drop_constraint("uq_print_destinations_name", type_="unique")
        batch_op.create_unique_constraint(
            "uq_print_destinations_tenant_name",
            ["tenant_id", "name"],
        )
        batch_op.create_index(
            "uq_print_destinations_default_active_doc_type",
            ["tenant_id", "document_type"],
            unique=True,
            sqlite_where=sa.text("is_default = 1 AND is_active = 1"),
            postgresql_where=sa.text("is_default AND is_active"),
        )


def _add_user_tenant_role_columns(default_tenant_id: int) -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("tenant_id", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "role",
                sa.String(length=20),
                nullable=False,
                server_default="tenant_admin",
            )
        )
        batch_op.create_foreign_key(
            "fk_users_tenant_id_tenants",
            "tenants",
            ["tenant_id"],
            ["id"],
        )
        batch_op.create_index("ix_users_tenant_id", ["tenant_id"], unique=False)
        batch_op.create_index("ix_users_role", ["role"], unique=False)

    conn = op.get_bind()
    conn.execute(
        sa.text("UPDATE users SET tenant_id=:tenant_id WHERE tenant_id IS NULL"),
        {"tenant_id": default_tenant_id},
    )
    conn.execute(
        sa.text("UPDATE users SET role='tenant_admin' WHERE role IS NULL OR trim(role)=''")
    )

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.alter_column(
            "role",
            existing_type=sa.String(length=20),
            nullable=False,
            server_default=None,
        )


def upgrade() -> None:
    _create_tenants_table()
    default_tenant_id = _ensure_default_tenant()

    for table_name in TENANT_BACKFILL_TABLES:
        _add_tenant_column(table_name, nullable=True)

    _add_user_tenant_role_columns(default_tenant_id)
    _backfill_tenant_id(TENANT_BACKFILL_TABLES, default_tenant_id)
    _enforce_not_null(TENANT_NOT_NULL_TABLES)
    _update_uniqueness_constraints()


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_index("ix_users_role")
        batch_op.drop_index("ix_users_tenant_id")
        batch_op.drop_constraint("fk_users_tenant_id_tenants", type_="foreignkey")
        batch_op.drop_column("role")
        batch_op.drop_column("tenant_id")

    with op.batch_alter_table("print_destinations", schema=None) as batch_op:
        batch_op.drop_index("uq_print_destinations_default_active_doc_type")
        batch_op.drop_constraint("uq_print_destinations_tenant_name", type_="unique")
        batch_op.create_unique_constraint("uq_print_destinations_name", ["name"])
        batch_op.create_index(
            "uq_print_destinations_default_active_doc_type",
            ["document_type"],
            unique=True,
            sqlite_where=sa.text("is_default = 1 AND is_active = 1"),
            postgresql_where=sa.text("is_default AND is_active"),
        )

    with op.batch_alter_table("print_templates", schema=None) as batch_op:
        batch_op.drop_constraint("uq_print_templates_tenant_code", type_="unique")
        batch_op.create_unique_constraint("uq_print_templates_code", ["code"])

    with op.batch_alter_table("company_settings", schema=None) as batch_op:
        batch_op.drop_constraint("uq_company_settings_tenant_id", type_="unique")

    with op.batch_alter_table("invoices", schema=None) as batch_op:
        batch_op.drop_constraint("uq_invoices_tenant_invoice_no", type_="unique")

    with op.batch_alter_table("tickets", schema=None) as batch_op:
        batch_op.drop_constraint("uq_tickets_tenant_ticket_no", type_="unique")

    with op.batch_alter_table("products", schema=None) as batch_op:
        batch_op.drop_constraint("uq_products_tenant_code", type_="unique")

    with op.batch_alter_table("vehicles", schema=None) as batch_op:
        batch_op.drop_constraint("uq_vehicles_tenant_registration", type_="unique")

    with op.batch_alter_table("customers", schema=None) as batch_op:
        batch_op.drop_constraint("uq_customers_tenant_account_code", type_="unique")

    for table_name in reversed(TENANT_BACKFILL_TABLES):
        with op.batch_alter_table(table_name, schema=None) as batch_op:
            batch_op.drop_index(f"ix_{table_name}_tenant_id")
            batch_op.drop_constraint(
                f"fk_{table_name}_tenant_id_tenants",
                type_="foreignkey",
            )
            batch_op.drop_column("tenant_id")

    op.drop_index("ix_tenants_is_active", table_name="tenants")
    op.drop_table("tenants")
