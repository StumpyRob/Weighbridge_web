"""void reasons: composite uniqueness + normalized invoice codes

Revision ID: m3n4o5p6q7r8
Revises: l2m3n4o5p6q7
Create Date: 2026-02-13 00:00:01.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "m3n4o5p6q7r8"
down_revision = "l2m3n4o5p6q7"
branch_labels = None
depends_on = None


def _normalize_reason_types(conn) -> None:
    conn.execute(
        sa.text(
            "UPDATE void_reasons "
            "SET reason_type = 'TICKET' "
            "WHERE reason_type IS NULL OR trim(reason_type) = ''"
        )
    )
    conn.execute(
        sa.text(
            "UPDATE void_reasons "
            "SET reason_type = upper(trim(reason_type)) "
            "WHERE reason_type IS NOT NULL"
        )
    )


def _upgrade_unique_constraint(conn) -> None:
    if conn.dialect.name == "sqlite":
        sqlite_copy_from = sa.Table(
            "void_reasons",
            sa.MetaData(),
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("code", sa.String(length=50), nullable=False),
            sa.Column("reason_type", sa.String(length=20), nullable=False),
            sa.Column("description", sa.String(length=255), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
        with op.batch_alter_table(
            "void_reasons",
            recreate="always",
            copy_from=sqlite_copy_from,
        ) as batch_op:
            batch_op.create_unique_constraint(
                "uq_void_reasons_code_reason_type",
                ["code", "reason_type"],
            )
        return

    inspector = sa.inspect(conn)
    for constraint in inspector.get_unique_constraints("void_reasons"):
        columns = [col.lower() for col in (constraint.get("column_names") or [])]
        name = constraint.get("name")
        if columns == ["code"] and name:
            op.drop_constraint(name, "void_reasons", type_="unique")
    for index in inspector.get_indexes("void_reasons"):
        columns = [col.lower() for col in (index.get("column_names") or [])]
        if index.get("unique") and columns == ["code"]:
            op.drop_index(index["name"], table_name="void_reasons")
    op.create_unique_constraint(
        "uq_void_reasons_code_reason_type",
        "void_reasons",
        ["code", "reason_type"],
    )


def _downgrade_unique_constraint(conn) -> None:
    if conn.dialect.name == "sqlite":
        sqlite_copy_from = sa.Table(
            "void_reasons",
            sa.MetaData(),
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("code", sa.String(length=50), nullable=False),
            sa.Column("reason_type", sa.String(length=20), nullable=False),
            sa.Column("description", sa.String(length=255), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
        with op.batch_alter_table(
            "void_reasons",
            recreate="always",
            copy_from=sqlite_copy_from,
        ) as batch_op:
            batch_op.create_unique_constraint("uq_void_reasons_code", ["code"])
        return

    inspector = sa.inspect(conn)
    for constraint in inspector.get_unique_constraints("void_reasons"):
        columns = [col.lower() for col in (constraint.get("column_names") or [])]
        name = constraint.get("name")
        if columns == ["code", "reason_type"] and name:
            op.drop_constraint(name, "void_reasons", type_="unique")
    for index in inspector.get_indexes("void_reasons"):
        columns = [col.lower() for col in (index.get("column_names") or [])]
        if index.get("unique") and columns == ["code", "reason_type"]:
            op.drop_index(index["name"], table_name="void_reasons")
    op.create_unique_constraint("uq_void_reasons_code", "void_reasons", ["code"])


def _upgrade_invoice_code_labels(conn) -> None:
    legacy_to_normalized = [
        ("Invoice entered in error", "Entered in error"),
        ("Invoice customer cancelled", "Customer cancelled"),
    ]
    for old_code, new_code in legacy_to_normalized:
        conn.execute(
            sa.text(
                "UPDATE void_reasons "
                "SET code = :new_code, description = :new_code, reason_type = 'INVOICE' "
                "WHERE lower(trim(code)) = :old_code "
                "AND upper(trim(reason_type)) = 'INVOICE'"
            ),
            {"new_code": new_code, "old_code": old_code.lower()},
        )


def _downgrade_invoice_code_labels(conn) -> None:
    normalized_to_legacy = [
        ("Entered in error", "Invoice entered in error"),
        ("Customer cancelled", "Invoice customer cancelled"),
    ]
    for old_code, new_code in normalized_to_legacy:
        conn.execute(
            sa.text(
                "UPDATE void_reasons "
                "SET code = :new_code, description = :old_code, reason_type = 'INVOICE' "
                "WHERE lower(trim(code)) = :old_code_match "
                "AND upper(trim(reason_type)) = 'INVOICE'"
            ),
            {
                "new_code": new_code,
                "old_code": old_code,
                "old_code_match": old_code.lower(),
            },
        )


def upgrade() -> None:
    conn = op.get_bind()
    _normalize_reason_types(conn)
    _upgrade_unique_constraint(conn)
    _upgrade_invoice_code_labels(conn)


def downgrade() -> None:
    conn = op.get_bind()
    _normalize_reason_types(conn)
    _downgrade_invoice_code_labels(conn)
    _downgrade_unique_constraint(conn)
