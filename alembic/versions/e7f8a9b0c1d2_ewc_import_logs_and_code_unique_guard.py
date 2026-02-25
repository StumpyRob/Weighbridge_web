"""ewc import logs + code unique guard

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-02-25 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "e7f8a9b0c1d2"
down_revision = "d6e7f8a9b0c1"
branch_labels = None
depends_on = None


def _has_unique_on_code_6(inspector: sa.Inspector) -> bool:
    unique_constraints = inspector.get_unique_constraints("ewc_codes")
    for constraint in unique_constraints:
        columns = constraint.get("column_names") or []
        if columns == ["code_6"] or set(columns) == {"code_6"}:
            return True

    indexes = inspector.get_indexes("ewc_codes")
    for index in indexes:
        if not bool(index.get("unique")):
            continue
        columns = index.get("column_names") or []
        if columns == ["code_6"] or set(columns) == {"code_6"}:
            return True

    return False


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "ewc_codes" in table_names and not _has_unique_on_code_6(inspector):
        op.create_index(
            "ux_ewc_codes_code_6",
            "ewc_codes",
            ["code_6"],
            unique=True,
        )

    if "ewc_import_logs" not in table_names:
        op.create_table(
            "ewc_import_logs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("source_file", sa.String(length=255), nullable=False),
            sa.Column("replace_mode", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("total_rows", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("inserted_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("updated_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("unchanged_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("deactivated_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("errors_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("imported_by", sa.String(length=120), nullable=True),
            sa.Column("imported_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_ewc_import_logs_imported_at",
            "ewc_import_logs",
            ["imported_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "ewc_import_logs" in table_names:
        index_names = {idx.get("name") for idx in inspector.get_indexes("ewc_import_logs")}
        if "ix_ewc_import_logs_imported_at" in index_names:
            op.drop_index("ix_ewc_import_logs_imported_at", table_name="ewc_import_logs")
        op.drop_table("ewc_import_logs")

    if "ewc_codes" in table_names:
        index_names = {idx.get("name") for idx in inspector.get_indexes("ewc_codes")}
        if "ux_ewc_codes_code_6" in index_names:
            op.drop_index("ux_ewc_codes_code_6", table_name="ewc_codes")
