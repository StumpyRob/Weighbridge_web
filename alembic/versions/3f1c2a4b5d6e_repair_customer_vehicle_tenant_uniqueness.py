"""repair customer and vehicle tenant uniqueness

Revision ID: 3f1c2a4b5d6e
Revises: 29d0e1f2a3b4
Create Date: 2026-03-10 12:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "3f1c2a4b5d6e"
down_revision = "29d0e1f2a3b4"
branch_labels = None
depends_on = None

_CUSTOMERS_TABLE = "customers"
_VEHICLES_TABLE = "vehicles"
_CUSTOMERS_COMPOSITE_UNIQUE = "uq_customers_tenant_account_code"
_VEHICLES_COMPOSITE_UNIQUE = "uq_vehicles_tenant_registration"
_CUSTOMERS_GLOBAL_UNIQUE = "uq_customers_account_code"
_VEHICLES_GLOBAL_UNIQUE = "uq_vehicles_registration"
_LEGACY_CUSTOMER_CONSTRAINTS = (
    "customers_account_code_key",
    _CUSTOMERS_GLOBAL_UNIQUE,
)
_LEGACY_VEHICLE_CONSTRAINTS = (
    "vehicles_registration_key",
    _VEHICLES_GLOBAL_UNIQUE,
)

_CUSTOMER_COPY_COLUMNS = (
    "id",
    "account_code",
    "name",
    "invoice_email",
    "phone",
    "address_line1",
    "address_line2",
    "city",
    "postcode",
    "country",
    "vat_number",
    "credit_limit_pence",
    "is_cash_account",
    "invoice_frequency_id",
    "invoice_frequency",
    "payment_terms",
    "payment_terms_days",
    "credit_limit",
    "on_stop",
    "cash_account",
    "do_not_invoice",
    "must_have_po",
    "created_at",
    "updated_at",
    "tenant_id",
)

_VEHICLE_COPY_COLUMNS = (
    "id",
    "registration",
    "owner_customer_id",
    "default_customer_id",
    "vehicle_type_id",
    "default_tare_kg",
    "overweight_threshold_kg",
    "haulier_id",
    "default_haulier_id",
    "driver_id",
    "default_driver_id",
    "created_at",
    "updated_at",
    "tenant_id",
)


def _normalized_columns(columns: object) -> list[str]:
    return [str(column or "").strip().lower() for column in list(columns or [])]


def _uniqueness_state(
    table_name: str,
    *,
    global_column: str,
    composite_columns: tuple[str, ...],
) -> tuple[set[str], set[str], bool, bool]:
    inspector = sa.inspect(op.get_bind())
    global_constraint_names: set[str] = set()
    global_index_names: set[str] = set()
    composite_exists = False
    global_exists = False

    for constraint in inspector.get_unique_constraints(table_name):
        columns = _normalized_columns(constraint.get("column_names"))
        name = str(constraint.get("name") or "").strip()
        if columns == list(composite_columns):
            composite_exists = True
        elif columns == [global_column]:
            global_exists = True
            if name:
                global_constraint_names.add(name)

    for index in inspector.get_indexes(table_name):
        if not bool(index.get("unique", False)):
            continue
        columns = _normalized_columns(index.get("column_names"))
        name = str(index.get("name") or "").strip()
        if columns == list(composite_columns):
            composite_exists = True
        elif columns == [global_column]:
            global_exists = True
            if name:
                global_index_names.add(name)

    return global_constraint_names, global_index_names, composite_exists, global_exists


def _copy_into_tmp_table(source_table: str, tmp_table: str, columns: tuple[str, ...]) -> None:
    column_list = ", ".join(columns)
    op.get_bind().execute(
        sa.text(
            f"INSERT INTO {tmp_table} ({column_list}) "
            f"SELECT {column_list} FROM {source_table}"
        )
    )


def _sqlite_rebuild_customers(*, include_global_unique: bool) -> None:
    conn = op.get_bind()
    conn.execute(sa.text("PRAGMA foreign_keys=OFF"))
    try:
        op.create_table(
            "customers__tmp",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("account_code", sa.String(length=50), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("invoice_email", sa.String(length=255), nullable=True),
            sa.Column("phone", sa.String(length=50), nullable=True),
            sa.Column("address_line1", sa.String(length=120), nullable=True),
            sa.Column("address_line2", sa.String(length=120), nullable=True),
            sa.Column("city", sa.String(length=120), nullable=True),
            sa.Column("postcode", sa.String(length=16), nullable=True),
            sa.Column("country", sa.String(length=120), nullable=True),
            sa.Column("vat_number", sa.String(length=50), nullable=True),
            sa.Column("credit_limit_pence", sa.Integer(), nullable=True),
            sa.Column("is_cash_account", sa.Boolean(), nullable=False),
            sa.Column("invoice_frequency_id", sa.Integer(), nullable=True),
            sa.Column("invoice_frequency", sa.String(length=20), nullable=True),
            sa.Column("payment_terms", sa.String(length=120), nullable=True),
            sa.Column("payment_terms_days", sa.Integer(), nullable=True),
            sa.Column("credit_limit", sa.Numeric(precision=12, scale=2), nullable=True),
            sa.Column("on_stop", sa.Boolean(), nullable=False),
            sa.Column("cash_account", sa.Boolean(), nullable=False),
            sa.Column("do_not_invoice", sa.Boolean(), nullable=False),
            sa.Column("must_have_po", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("tenant_id", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["invoice_frequency_id"], ["invoice_frequencies.id"]),
            sa.ForeignKeyConstraint(
                ["tenant_id"],
                ["tenants.id"],
                name="fk_customers_tenant_id_tenants",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "tenant_id",
                "account_code",
                name=_CUSTOMERS_COMPOSITE_UNIQUE,
            ),
            *([sa.UniqueConstraint("account_code")] if include_global_unique else []),
        )
        _copy_into_tmp_table(
            _CUSTOMERS_TABLE,
            "customers__tmp",
            _CUSTOMER_COPY_COLUMNS,
        )
        op.drop_table(_CUSTOMERS_TABLE)
        op.rename_table("customers__tmp", _CUSTOMERS_TABLE)
        op.create_index("ix_customers_tenant_id", _CUSTOMERS_TABLE, ["tenant_id"], unique=False)
    finally:
        conn.execute(sa.text("PRAGMA foreign_keys=ON"))


def _sqlite_rebuild_vehicles(*, include_global_unique: bool) -> None:
    conn = op.get_bind()
    conn.execute(sa.text("PRAGMA foreign_keys=OFF"))
    try:
        op.create_table(
            "vehicles__tmp",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("registration", sa.String(length=15), nullable=False),
            sa.Column("owner_customer_id", sa.Integer(), nullable=True),
            sa.Column("default_customer_id", sa.Integer(), nullable=True),
            sa.Column("vehicle_type_id", sa.Integer(), nullable=True),
            sa.Column("default_tare_kg", sa.Numeric(precision=12, scale=3), nullable=True),
            sa.Column(
                "overweight_threshold_kg",
                sa.Numeric(precision=12, scale=3),
                nullable=True,
            ),
            sa.Column("haulier_id", sa.Integer(), nullable=True),
            sa.Column("default_haulier_id", sa.Integer(), nullable=True),
            sa.Column("driver_id", sa.Integer(), nullable=True),
            sa.Column("default_driver_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("tenant_id", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(
                ["tenant_id"],
                ["tenants.id"],
                name="fk_vehicles_tenant_id_tenants",
            ),
            sa.ForeignKeyConstraint(["owner_customer_id"], ["customers.id"]),
            sa.ForeignKeyConstraint(["default_customer_id"], ["customers.id"]),
            sa.ForeignKeyConstraint(["vehicle_type_id"], ["vehicle_types.id"]),
            sa.ForeignKeyConstraint(["haulier_id"], ["hauliers.id"]),
            sa.ForeignKeyConstraint(["default_haulier_id"], ["hauliers.id"]),
            sa.ForeignKeyConstraint(["driver_id"], ["drivers.id"]),
            sa.ForeignKeyConstraint(["default_driver_id"], ["drivers.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "tenant_id",
                "registration",
                name=_VEHICLES_COMPOSITE_UNIQUE,
            ),
            *([sa.UniqueConstraint("registration")] if include_global_unique else []),
        )
        _copy_into_tmp_table(
            _VEHICLES_TABLE,
            "vehicles__tmp",
            _VEHICLE_COPY_COLUMNS,
        )
        op.drop_table(_VEHICLES_TABLE)
        op.rename_table("vehicles__tmp", _VEHICLES_TABLE)
        op.create_index("ix_vehicles_tenant_id", _VEHICLES_TABLE, ["tenant_id"], unique=False)
    finally:
        conn.execute(sa.text("PRAGMA foreign_keys=ON"))


def _ensure_no_global_duplicates(table_name: str, column_name: str) -> None:
    duplicate_value = op.get_bind().execute(
        sa.text(
            f"SELECT {column_name} "
            f"FROM {table_name} "
            f"WHERE {column_name} IS NOT NULL "
            f"GROUP BY {column_name} "
            f"HAVING COUNT(*) > 1 "
            f"LIMIT 1"
        )
    ).scalar_one_or_none()
    if duplicate_value is not None:
        raise RuntimeError(
            f"Cannot restore global uniqueness for {table_name}.{column_name}; "
            f"tenant-scoped duplicates already exist for value {duplicate_value!r}."
        )


def _upgrade_postgresql() -> None:
    customer_constraints, customer_indexes, customer_composite_exists, _ = _uniqueness_state(
        _CUSTOMERS_TABLE,
        global_column="account_code",
        composite_columns=("tenant_id", "account_code"),
    )
    for name in sorted(set(customer_constraints).union(_LEGACY_CUSTOMER_CONSTRAINTS)):
        op.execute(
            sa.text(f'ALTER TABLE {_CUSTOMERS_TABLE} DROP CONSTRAINT IF EXISTS "{name}"')
        )
    for name in sorted(customer_indexes):
        op.execute(sa.text(f'DROP INDEX IF EXISTS "{name}"'))
    if not customer_composite_exists:
        op.create_unique_constraint(
            _CUSTOMERS_COMPOSITE_UNIQUE,
            _CUSTOMERS_TABLE,
            ["tenant_id", "account_code"],
        )

    vehicle_constraints, vehicle_indexes, vehicle_composite_exists, _ = _uniqueness_state(
        _VEHICLES_TABLE,
        global_column="registration",
        composite_columns=("tenant_id", "registration"),
    )
    for name in sorted(set(vehicle_constraints).union(_LEGACY_VEHICLE_CONSTRAINTS)):
        op.execute(
            sa.text(f'ALTER TABLE {_VEHICLES_TABLE} DROP CONSTRAINT IF EXISTS "{name}"')
        )
    for name in sorted(vehicle_indexes):
        op.execute(sa.text(f'DROP INDEX IF EXISTS "{name}"'))
    if not vehicle_composite_exists:
        op.create_unique_constraint(
            _VEHICLES_COMPOSITE_UNIQUE,
            _VEHICLES_TABLE,
            ["tenant_id", "registration"],
        )


def _downgrade_postgresql() -> None:
    _ensure_no_global_duplicates(_CUSTOMERS_TABLE, "account_code")
    _, _, _, customer_global_exists = _uniqueness_state(
        _CUSTOMERS_TABLE,
        global_column="account_code",
        composite_columns=("tenant_id", "account_code"),
    )
    if not customer_global_exists:
        op.create_unique_constraint(
            _CUSTOMERS_GLOBAL_UNIQUE,
            _CUSTOMERS_TABLE,
            ["account_code"],
        )

    _ensure_no_global_duplicates(_VEHICLES_TABLE, "registration")
    _, _, _, vehicle_global_exists = _uniqueness_state(
        _VEHICLES_TABLE,
        global_column="registration",
        composite_columns=("tenant_id", "registration"),
    )
    if not vehicle_global_exists:
        op.create_unique_constraint(
            _VEHICLES_GLOBAL_UNIQUE,
            _VEHICLES_TABLE,
            ["registration"],
        )


def upgrade() -> None:
    dialect = str(op.get_bind().dialect.name or "").strip().lower()
    if dialect == "sqlite":
        _sqlite_rebuild_customers(include_global_unique=False)
        _sqlite_rebuild_vehicles(include_global_unique=False)
        return
    _upgrade_postgresql()


def downgrade() -> None:
    _ensure_no_global_duplicates(_CUSTOMERS_TABLE, "account_code")
    _ensure_no_global_duplicates(_VEHICLES_TABLE, "registration")

    dialect = str(op.get_bind().dialect.name or "").strip().lower()
    if dialect == "sqlite":
        _sqlite_rebuild_customers(include_global_unique=True)
        _sqlite_rebuild_vehicles(include_global_unique=True)
        return
    _downgrade_postgresql()
