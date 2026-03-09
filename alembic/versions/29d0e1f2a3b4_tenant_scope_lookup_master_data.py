from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "29d0e1f2a3b4"
down_revision = "18c9d0e1f2a3"
branch_labels = None
depends_on = None


_SCOPED_TABLES: dict[str, dict[str, object]] = {
    "hauliers": {
        "copy_columns": (
            "name",
            "carrier_licence_number",
            "is_active",
            "created_at",
            "updated_at",
        ),
        "legacy_unique_names": ("uq_hauliers_name",),
        "legacy_unique_columns": (("name",),),
        "new_uniques": (("uq_hauliers_tenant_name", ("tenant_id", "name")),),
        "tenant_index": "ix_hauliers_tenant_id",
        "children": (
            ("vehicles", "haulier_id"),
            ("vehicles", "default_haulier_id"),
            ("tickets", "haulier_id"),
        ),
    },
    "drivers": {
        "copy_columns": (
            "name",
            "is_active",
            "created_at",
            "updated_at",
        ),
        "legacy_unique_names": ("uq_drivers_name",),
        "legacy_unique_columns": (("name",),),
        "new_uniques": (("uq_drivers_tenant_name", ("tenant_id", "name")),),
        "tenant_index": "ix_drivers_tenant_id",
        "children": (
            ("vehicles", "driver_id"),
            ("vehicles", "default_driver_id"),
            ("tickets", "driver_id"),
        ),
    },
    "containers": {
        "copy_columns": (
            "name",
            "is_active",
            "created_at",
            "updated_at",
        ),
        "legacy_unique_names": ("uq_containers_name",),
        "legacy_unique_columns": (("name",),),
        "new_uniques": (("uq_containers_tenant_name", ("tenant_id", "name")),),
        "tenant_index": "ix_containers_tenant_id",
        "children": (
            ("tickets", "container_id"),
            ("vehicle_tares", "container_id"),
        ),
    },
    "destinations": {
        "copy_columns": (
            "name",
            "is_active",
            "created_at",
            "updated_at",
        ),
        "legacy_unique_names": ("uq_destinations_name",),
        "legacy_unique_columns": (("name",),),
        "new_uniques": (("uq_destinations_tenant_name", ("tenant_id", "name")),),
        "tenant_index": "ix_destinations_tenant_id",
        "children": (
            ("products", "default_destination_id"),
            ("tickets", "destination_id"),
        ),
    },
    "yards": {
        "copy_columns": (
            "code",
            "description",
            "is_active",
            "created_at",
            "updated_at",
        ),
        "legacy_unique_names": (),
        "legacy_unique_columns": (("code",),),
        "new_uniques": (("uq_yards_tenant_code", ("tenant_id", "code")),),
        "tenant_index": "ix_yards_tenant_id",
        "children": (("tickets", "yard_id"),),
    },
    "areas": {
        "copy_columns": (
            "code",
            "description",
            "is_active",
            "created_at",
            "updated_at",
        ),
        "legacy_unique_names": (),
        "legacy_unique_columns": (("code",),),
        "new_uniques": (("uq_areas_tenant_code", ("tenant_id", "code")),),
        "tenant_index": "ix_areas_tenant_id",
        "children": (("tickets", "area_id"),),
    },
    "units": {
        "copy_columns": (
            "name",
            "unit_type",
            "is_active",
            "created_at",
            "updated_at",
        ),
        "legacy_unique_names": ("uq_units_name",),
        "legacy_unique_columns": (("name",),),
        "new_uniques": (("uq_units_tenant_name", ("tenant_id", "name")),),
        "tenant_index": "ix_units_tenant_id",
        "children": (
            ("products", "unit_id"),
            ("tickets", "unit_id"),
        ),
    },
    "product_groups": {
        "copy_columns": (
            "code",
            "name",
            "description",
            "nominal_code_default",
            "is_active",
            "created_at",
            "updated_at",
        ),
        "legacy_unique_names": (),
        "legacy_unique_columns": (("code",), ("name",)),
        "new_uniques": (
            ("uq_product_groups_tenant_code", ("tenant_id", "code")),
            ("uq_product_groups_tenant_name", ("tenant_id", "name")),
        ),
        "tenant_index": "ix_product_groups_tenant_id",
        "children": (("products", "group_id"),),
    },
}


_SQLITE_RECREATE_TABLES = {"yards", "areas", "product_groups"}


def _normalized_columns(columns: object) -> list[str]:
    return [str(column or "").strip().lower() for column in list(columns or [])]


def _existing_unique_constraint_names(bind, table_name: str) -> set[str]:
    inspector = sa.inspect(bind)
    names: set[str] = set()
    for constraint in inspector.get_unique_constraints(table_name):
        name = str(constraint.get("name") or "").strip()
        if name:
            names.add(name)
    return names


def _matching_unique_constraints(
    bind,
    table_name: str,
    expected_column_sets: tuple[tuple[str, ...], ...],
) -> set[str]:
    inspector = sa.inspect(bind)
    expected = {tuple(column.lower() for column in columns) for columns in expected_column_sets}
    names: set[str] = set()
    for constraint in inspector.get_unique_constraints(table_name):
        columns = tuple(_normalized_columns(constraint.get("column_names")))
        name = str(constraint.get("name") or "").strip()
        if columns in expected and name:
            names.add(name)
    return names


def _sqlite_scoped_table_columns(table_name: str) -> list[sa.Column]:
    if table_name == "yards":
        return [
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("tenant_id", sa.Integer(), nullable=True),
            sa.Column("code", sa.String(length=50), nullable=False),
            sa.Column("description", sa.String(length=255), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        ]
    if table_name == "areas":
        return [
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("tenant_id", sa.Integer(), nullable=True),
            sa.Column("code", sa.String(length=50), nullable=False),
            sa.Column("description", sa.String(length=255), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        ]
    if table_name == "product_groups":
        return [
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("tenant_id", sa.Integer(), nullable=True),
            sa.Column("code", sa.String(length=50), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("description", sa.String(length=255), nullable=True),
            sa.Column("nominal_code_default", sa.String(length=20), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        ]
    raise ValueError(f"Unsupported SQLite table recreation target: {table_name}")


def _sqlite_recreate_scoped_table(table_name: str, config: dict[str, object]) -> None:
    temp_table_name = f"_alembic_{table_name}_scoped"
    copy_columns = tuple(config["copy_columns"])
    insert_column_list = ", ".join(["id", *copy_columns, "tenant_id"])
    select_column_list = ", ".join(["id", *copy_columns, "NULL AS tenant_id"])

    op.execute(sa.text("PRAGMA foreign_keys=OFF"))
    try:
        op.create_table(
            temp_table_name,
            *_sqlite_scoped_table_columns(table_name),
            sa.ForeignKeyConstraint(
                ["tenant_id"],
                ["tenants.id"],
                name=f"fk_{table_name}_tenant_id_tenants",
            ),
            sa.PrimaryKeyConstraint("id"),
            *[
                sa.UniqueConstraint(*columns, name=constraint_name)
                for constraint_name, columns in config["new_uniques"]
            ],
        )
        op.execute(
            sa.text(
                f"INSERT INTO {temp_table_name} ({insert_column_list}) "
                f"SELECT {select_column_list} FROM {table_name}"
            )
        )
        op.drop_table(table_name)
        op.rename_table(temp_table_name, table_name)
        op.create_index(
            str(config["tenant_index"]),
            table_name,
            ["tenant_id"],
            unique=False,
        )
    finally:
        op.execute(sa.text("PRAGMA foreign_keys=ON"))


def _alter_scoped_table(table_name: str, config: dict[str, object]) -> None:
    bind = op.get_bind()
    dialect = str(bind.dialect.name or "").strip().lower()
    if dialect == "sqlite" and table_name in _SQLITE_RECREATE_TABLES:
        _sqlite_recreate_scoped_table(table_name, config)
        return

    legacy_unique_names = {
        name
        for name in config.get("legacy_unique_names", ())
        if name in _existing_unique_constraint_names(bind, table_name)
    }
    legacy_unique_names.update(
        _matching_unique_constraints(
            bind,
            table_name,
            tuple(config.get("legacy_unique_columns", ())),
        )
    )

    recreate_mode = "always" if dialect == "sqlite" else "auto"
    with op.batch_alter_table(table_name, schema=None, recreate=recreate_mode) as batch_op:
        batch_op.add_column(sa.Column("tenant_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            f"fk_{table_name}_tenant_id_tenants",
            "tenants",
            ["tenant_id"],
            ["id"],
        )
        for constraint_name in sorted(legacy_unique_names):
            batch_op.drop_constraint(constraint_name, type_="unique")
        batch_op.create_index(str(config["tenant_index"]), ["tenant_id"], unique=False)
        for constraint_name, columns in config["new_uniques"]:
            batch_op.create_unique_constraint(str(constraint_name), list(columns))


def _tenant_ids(bind) -> list[int]:
    tenant_rows = bind.execute(sa.text("SELECT id FROM tenants ORDER BY id ASC")).all()
    tenant_ids = [int(row[0]) for row in tenant_rows if row and row[0] is not None]
    if not tenant_ids:
        raise RuntimeError("Cannot scope lookup data without at least one tenant row.")
    return tenant_ids


def _duplicate_rows_per_tenant(
    bind,
    table_name: str,
    copy_columns: tuple[str, ...],
    tenant_ids: list[int],
) -> dict[tuple[int, int], int]:
    metadata = sa.MetaData()
    table = sa.Table(table_name, metadata, autoload_with=bind)
    primary_tenant_id = int(tenant_ids[0])
    selected_columns = [table.c.id]
    selected_columns.extend(table.c[column_name] for column_name in copy_columns)
    rows = bind.execute(sa.select(*selected_columns).order_by(table.c.id.asc())).mappings().all()

    id_map: dict[tuple[int, int], int] = {}
    for row in rows:
        source_id = int(row["id"])
        bind.execute(
            table.update().where(table.c.id == source_id).values(tenant_id=primary_tenant_id)
        )
        id_map[(source_id, primary_tenant_id)] = source_id

        payload = {column_name: row[column_name] for column_name in copy_columns}
        for tenant_id in tenant_ids[1:]:
            insert_payload = dict(payload)
            insert_payload["tenant_id"] = int(tenant_id)
            result = bind.execute(table.insert().values(**insert_payload))
            inserted_id = result.inserted_primary_key[0]
            if inserted_id is None:
                raise RuntimeError(
                    f"Failed to duplicate {table_name} row {source_id} for tenant {tenant_id}."
                )
            id_map[(source_id, int(tenant_id))] = int(inserted_id)

    return id_map


def _remap_child_foreign_keys(
    bind,
    child_table_name: str,
    fk_column: str,
    source_table_name: str,
    id_map: dict[tuple[int, int], int],
) -> None:
    rows = bind.execute(
        sa.text(
            f"SELECT id, tenant_id, {fk_column} "
            f"FROM {child_table_name} "
            f"WHERE {fk_column} IS NOT NULL"
        )
    ).mappings().all()
    updates: list[dict[str, int]] = []
    for row in rows:
        child_id = row.get("id")
        tenant_id = row.get("tenant_id")
        source_id = row.get(fk_column)
        if child_id is None or tenant_id is None or source_id is None:
            continue
        mapped_id = id_map.get((int(source_id), int(tenant_id)))
        if mapped_id is None:
            raise RuntimeError(
                f"Missing {source_table_name} mapping for {child_table_name}.{fk_column} "
                f"row {child_id} (source {source_id}, tenant {tenant_id})."
            )
        if int(mapped_id) != int(source_id):
            updates.append({"id": int(child_id), "new_fk": int(mapped_id)})

    if updates:
        bind.execute(
            sa.text(
                f"UPDATE {child_table_name} SET {fk_column} = :new_fk WHERE id = :id"
            ),
            updates,
        )


def _set_tenant_not_null(table_name: str) -> None:
    bind = op.get_bind()
    dialect = str(bind.dialect.name or "").strip().lower()
    recreate_mode = "always" if dialect == "sqlite" else "auto"
    with op.batch_alter_table(table_name, schema=None, recreate=recreate_mode) as batch_op:
        batch_op.alter_column(
            "tenant_id",
            existing_type=sa.Integer(),
            nullable=False,
        )


def upgrade() -> None:
    bind = op.get_bind()
    tenant_ids = _tenant_ids(bind)

    for table_name, config in _SCOPED_TABLES.items():
        _alter_scoped_table(table_name, config)

    for table_name, config in _SCOPED_TABLES.items():
        id_map = _duplicate_rows_per_tenant(
            bind,
            table_name,
            tuple(config["copy_columns"]),
            tenant_ids,
        )
        for child_table_name, fk_column in tuple(config.get("children", ())):
            _remap_child_foreign_keys(
                bind,
                str(child_table_name),
                str(fk_column),
                table_name,
                id_map,
            )

    for table_name in _SCOPED_TABLES:
        _set_tenant_not_null(table_name)


def downgrade() -> None:
    raise RuntimeError(
        "Downgrade is not supported automatically because tenant-scoped lookup rows "
        "cannot be collapsed back into global rows without manual data reconciliation."
    )
