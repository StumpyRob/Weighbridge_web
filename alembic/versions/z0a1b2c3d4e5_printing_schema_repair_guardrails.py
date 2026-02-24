"""printing schema repair guardrails

Revision ID: z0a1b2c3d4e5
Revises: y5z6a7b8c9d0
Create Date: 2026-02-24 21:15:00.000000
"""

from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa


revision = "z0a1b2c3d4e5"
down_revision = "y5z6a7b8c9d0"
branch_labels = None
depends_on = None


def _table_exists(inspector: sa.Inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _column_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    return {str(col["name"]) for col in inspector.get_columns(table_name)}


def _index_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    return {str(idx["name"]) for idx in inspector.get_indexes(table_name)}


def _as_json_object(value: object) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if value is None:
        return {}
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _normalize_document_type(value: object) -> str:
    text = str(value or "").strip().upper()
    if text in {"TICKET", "INVOICE", "WTN"}:
        return text
    if text.startswith("INVOICE"):
        return "INVOICE"
    if text.startswith("WTN"):
        return "WTN"
    if text.startswith("TICKET"):
        return "TICKET"
    return "TICKET"


def _normalize_format(value: object) -> str:
    text = str(value or "").strip().upper()
    if text == "HTML":
        return "HTML"
    if text == "PDF":
        return "PDF"
    return "TEXT"


def _normalize_delivery_type(value: object) -> str:
    text = str(value or "").strip().upper()
    mapping = {
        "LOCAL_BROWSER": "PRINT_LOCAL_BROWSER",
        "NETWORK_RAW_9100": "PRINT_NETWORK_RAW_9100",
        "LOCAL_NODE_HTTP": "PRINT_NODE_HTTP",
        "EMAIL_PDF": "EMAIL_PDF",
        "PRINT_LOCAL_BROWSER": "PRINT_LOCAL_BROWSER",
        "PRINT_NETWORK_RAW_9100": "PRINT_NETWORK_RAW_9100",
        "PRINT_NODE_HTTP": "PRINT_NODE_HTTP",
        "CUPS": "PRINT_LOCAL_BROWSER",
    }
    return mapping.get(text, "PRINT_LOCAL_BROWSER")


def _create_print_destinations_if_missing(bind: sa.Connection, inspector: sa.Inspector) -> None:
    if _table_exists(inspector, "print_destinations"):
        return

    op.create_table(
        "print_destinations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("document_type", sa.String(length=16), nullable=False),
        sa.Column("template_id", sa.Integer(), nullable=False),
        sa.Column("delivery_type", sa.String(length=32), nullable=False),
        sa.Column(
            "delivery_config",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "is_default",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["template_id"], ["print_templates.id"]),
        sa.UniqueConstraint("name", name="uq_print_destinations_name"),
    )


def _ensure_templates_schema(bind: sa.Connection, inspector: sa.Inspector) -> None:
    if not _table_exists(inspector, "print_templates"):
        return

    columns = _column_names(inspector, "print_templates")
    if "document_type" not in columns:
        op.add_column(
            "print_templates",
            sa.Column("document_type", sa.String(length=16), nullable=True),
        )
    if "format" not in columns:
        op.add_column(
            "print_templates",
            sa.Column("format", sa.String(length=16), nullable=True),
        )

    inspector = sa.inspect(bind)
    columns = _column_names(inspector, "print_templates")
    purpose_expr = "purpose" if "purpose" in columns else "NULL"
    content_type_expr = "content_type" if "content_type" in columns else "NULL"
    rows = (
        bind.execute(
            sa.text(
                f"""
                SELECT
                    id,
                    document_type,
                    format,
                    {purpose_expr} AS purpose,
                    {content_type_expr} AS content_type
                FROM print_templates
                """
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        bind.execute(
            sa.text(
                """
                UPDATE print_templates
                SET document_type = :document_type, format = :template_format
                WHERE id = :id
                """
            ),
            {
                "id": int(row["id"]),
                "document_type": _normalize_document_type(
                    row.get("document_type") or row.get("purpose")
                ),
                "template_format": _normalize_format(
                    row.get("format") or row.get("content_type")
                ),
            },
        )

    inspector = sa.inspect(bind)
    columns_info = {str(col["name"]): col for col in inspector.get_columns("print_templates")}
    with op.batch_alter_table("print_templates") as batch_op:
        if "document_type" in columns_info and bool(columns_info["document_type"].get("nullable", True)):
            batch_op.alter_column(
                "document_type",
                existing_type=sa.String(length=16),
                nullable=False,
            )
        if "format" in columns_info and bool(columns_info["format"].get("nullable", True)):
            batch_op.alter_column(
                "format",
                existing_type=sa.String(length=16),
                nullable=False,
            )

    inspector = sa.inspect(bind)
    index_names = _index_names(inspector, "print_templates")
    if "ix_print_templates_document_type" not in index_names:
        op.create_index(
            "ix_print_templates_document_type",
            "print_templates",
            ["document_type"],
            unique=False,
        )


def _fallback_template_id_for_document(bind: sa.Connection, document_type: str) -> int:
    existing = bind.execute(
        sa.text(
            """
            SELECT id
            FROM print_templates
            WHERE document_type = :document_type
            ORDER BY is_active DESC, id ASC
            LIMIT 1
            """
        ),
        {"document_type": document_type},
    ).first()
    if existing:
        return int(existing[0])

    code = {
        "TICKET": "ticket_default",
        "INVOICE": "invoice_default",
        "WTN": "wtn_default",
    }.get(document_type, "ticket_default")
    fmt = "HTML" if document_type == "INVOICE" else "TEXT"
    content = (
        "<html><body>Invoice {{ invoice.invoice_no }}</body></html>"
        if document_type == "INVOICE"
        else "Ticket {{ payload.ticket_no }}"
    )
    bind.execute(
        sa.text(
            """
            INSERT INTO print_templates
                (code, description, document_type, format, content, is_active, created_at, updated_at)
            VALUES
                (:code, :description, :document_type, :template_format, :content, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """
        ),
        {
            "code": code,
            "description": f"{document_type.title()} default template",
            "document_type": document_type,
            "template_format": fmt,
            "content": content,
        },
    )
    return int(bind.execute(sa.text("SELECT last_insert_rowid()")).scalar_one())


def _backfill_destinations_from_profiles(bind: sa.Connection, inspector: sa.Inspector) -> None:
    if not _table_exists(inspector, "print_destinations"):
        return
    if not _table_exists(inspector, "print_profiles"):
        return

    existing_count = int(
        bind.execute(sa.text("SELECT COUNT(*) FROM print_destinations")).scalar_one()
    )
    if existing_count > 0:
        return

    rows = (
        bind.execute(
            sa.text(
                """
                SELECT
                    id, code, description, purpose, template_id,
                    transport_mode, transport_config, is_default, is_active,
                    created_at, updated_at
                FROM print_profiles
                ORDER BY id ASC
                """
            )
        )
        .mappings()
        .all()
    )
    template_ids = {
        int(row[0]) for row in bind.execute(sa.text("SELECT id FROM print_templates")).fetchall()
    }
    for row in rows:
        destination_id = int(row["id"])
        name = str(row.get("code") or "").strip() or f"Destination {destination_id}"
        document_type = _normalize_document_type(row.get("purpose"))
        template_id = row.get("template_id")
        try:
            resolved_template_id = int(template_id) if template_id is not None else None
        except (TypeError, ValueError):
            resolved_template_id = None
        if not resolved_template_id or resolved_template_id not in template_ids:
            resolved_template_id = _fallback_template_id_for_document(bind, document_type)
            template_ids.add(resolved_template_id)

        bind.execute(
            sa.text(
                """
                INSERT INTO print_destinations
                    (id, name, description, document_type, template_id, delivery_type, delivery_config, is_default, is_active, created_at, updated_at)
                VALUES
                    (:id, :name, :description, :document_type, :template_id, :delivery_type, :delivery_config, :is_default, :is_active, :created_at, :updated_at)
                """
            ),
            {
                "id": destination_id,
                "name": name,
                "description": str(row.get("description") or "").strip() or None,
                "document_type": document_type,
                "template_id": resolved_template_id,
                "delivery_type": _normalize_delivery_type(row.get("transport_mode")),
                "delivery_config": json.dumps(_as_json_object(row.get("transport_config"))),
                "is_default": 1 if bool(row.get("is_default")) else 0,
                "is_active": 1 if row.get("is_active") is None or bool(row.get("is_active")) else 0,
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            },
        )

    bind.execute(sa.text("UPDATE print_destinations SET is_default = 0 WHERE is_active = 0"))
    for document_type in ("TICKET", "INVOICE", "WTN"):
        keep_row = bind.execute(
            sa.text(
                """
                SELECT id
                FROM print_destinations
                WHERE document_type = :document_type AND is_active = 1
                ORDER BY is_default DESC, id ASC
                LIMIT 1
                """
            ),
            {"document_type": document_type},
        ).first()
        if keep_row:
            bind.execute(
                sa.text(
                    """
                    UPDATE print_destinations
                    SET is_default = CASE WHEN id = :keep_id THEN 1 ELSE 0 END
                    WHERE document_type = :document_type AND is_active = 1
                    """
                ),
                {"document_type": document_type, "keep_id": int(keep_row[0])},
            )


def _ensure_destinations_indexes(bind: sa.Connection, inspector: sa.Inspector) -> None:
    if not _table_exists(inspector, "print_destinations"):
        return
    index_names = _index_names(inspector, "print_destinations")
    if "ix_print_destinations_document_type" not in index_names:
        op.create_index(
            "ix_print_destinations_document_type",
            "print_destinations",
            ["document_type"],
            unique=False,
        )
    if "ix_print_destinations_delivery_type" not in index_names:
        op.create_index(
            "ix_print_destinations_delivery_type",
            "print_destinations",
            ["delivery_type"],
            unique=False,
        )
    if "ix_print_destinations_is_active" not in index_names:
        op.create_index(
            "ix_print_destinations_is_active",
            "print_destinations",
            ["is_active"],
            unique=False,
        )
    if "ix_print_destinations_template_id" not in index_names:
        op.create_index(
            "ix_print_destinations_template_id",
            "print_destinations",
            ["template_id"],
            unique=False,
        )
    if "uq_print_destinations_default_active_doc_type" not in index_names:
        op.create_index(
            "uq_print_destinations_default_active_doc_type",
            "print_destinations",
            ["document_type"],
            unique=True,
            sqlite_where=sa.text("is_default = 1 AND is_active = 1"),
        )


def _ensure_jobs_schema(bind: sa.Connection, inspector: sa.Inspector) -> None:
    if not _table_exists(inspector, "print_jobs"):
        return

    columns = _column_names(inspector, "print_jobs")
    if "document_type" not in columns:
        op.add_column(
            "print_jobs",
            sa.Column("document_type", sa.String(length=16), nullable=True),
        )
    if "destination_id" not in columns:
        op.add_column(
            "print_jobs",
            sa.Column("destination_id", sa.Integer(), nullable=True),
        )
    if "invoice_id" not in columns:
        op.add_column(
            "print_jobs",
            sa.Column("invoice_id", sa.Integer(), nullable=True),
        )
    if "delivery_type" not in columns:
        op.add_column(
            "print_jobs",
            sa.Column("delivery_type", sa.String(length=32), nullable=True),
        )
    if "delivery_config_json" not in columns:
        op.add_column(
            "print_jobs",
            sa.Column("delivery_config_json", sa.JSON(), nullable=True),
        )

    inspector = sa.inspect(bind)
    columns = _column_names(inspector, "print_jobs")
    purpose_expr = "purpose" if "purpose" in columns else "NULL"
    profile_id_expr = "profile_id" if "profile_id" in columns else "NULL"
    transport_mode_expr = "transport_mode" if "transport_mode" in columns else "NULL"
    transport_config_expr = (
        "transport_config_json" if "transport_config_json" in columns else "NULL"
    )
    rows = (
        bind.execute(
            sa.text(
                f"""
                SELECT
                    id,
                    document_type,
                    destination_id,
                    delivery_type,
                    delivery_config_json,
                    {purpose_expr} AS purpose,
                    {profile_id_expr} AS profile_id,
                    {transport_mode_expr} AS transport_mode,
                    {transport_config_expr} AS transport_config_json
                FROM print_jobs
                """
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        destination_id = row.get("destination_id")
        if destination_id is None and row.get("profile_id") is not None:
            try:
                destination_id = int(row.get("profile_id"))
            except (TypeError, ValueError):
                destination_id = None
        payload_config = _as_json_object(row.get("delivery_config_json"))
        if not payload_config:
            payload_config = _as_json_object(row.get("transport_config_json"))
        bind.execute(
            sa.text(
                """
                UPDATE print_jobs
                SET
                    document_type = :document_type,
                    destination_id = :destination_id,
                    delivery_type = :delivery_type,
                    delivery_config_json = :delivery_config_json
                WHERE id = :id
                """
            ),
            {
                "id": int(row["id"]),
                "document_type": _normalize_document_type(
                    row.get("document_type") or row.get("purpose")
                ),
                "destination_id": destination_id,
                "delivery_type": _normalize_delivery_type(
                    row.get("delivery_type") or row.get("transport_mode")
                ),
                "delivery_config_json": json.dumps(payload_config),
            },
        )

    inspector = sa.inspect(bind)
    col_info = {str(col["name"]): col for col in inspector.get_columns("print_jobs")}
    with op.batch_alter_table("print_jobs") as batch_op:
        if "document_type" in col_info and bool(col_info["document_type"].get("nullable", True)):
            batch_op.alter_column(
                "document_type",
                existing_type=sa.String(length=16),
                nullable=False,
            )
        if "delivery_type" in col_info and bool(col_info["delivery_type"].get("nullable", True)):
            batch_op.alter_column(
                "delivery_type",
                existing_type=sa.String(length=32),
                nullable=False,
            )
        if "delivery_config_json" in col_info and bool(col_info["delivery_config_json"].get("nullable", True)):
            batch_op.alter_column(
                "delivery_config_json",
                existing_type=sa.JSON(),
                nullable=False,
            )

    inspector = sa.inspect(bind)
    index_names = _index_names(inspector, "print_jobs")
    if "ix_print_jobs_document_type" not in index_names:
        op.create_index(
            "ix_print_jobs_document_type",
            "print_jobs",
            ["document_type"],
            unique=False,
        )
    if "ix_print_jobs_destination_id" not in index_names:
        op.create_index(
            "ix_print_jobs_destination_id",
            "print_jobs",
            ["destination_id"],
            unique=False,
        )
    if "ix_print_jobs_invoice_id" not in index_names:
        op.create_index(
            "ix_print_jobs_invoice_id",
            "print_jobs",
            ["invoice_id"],
            unique=False,
        )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    _ensure_templates_schema(bind, inspector)
    inspector = sa.inspect(bind)

    _create_print_destinations_if_missing(bind, inspector)
    inspector = sa.inspect(bind)

    _backfill_destinations_from_profiles(bind, inspector)
    inspector = sa.inspect(bind)

    _ensure_destinations_indexes(bind, inspector)
    inspector = sa.inspect(bind)

    _ensure_jobs_schema(bind, inspector)


def downgrade() -> None:
    # Intentional no-op: corrective migration only.
    pass

