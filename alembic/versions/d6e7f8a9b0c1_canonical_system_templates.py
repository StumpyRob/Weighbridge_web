"""canonical system template names and A4 ticket system template

Revision ID: d6e7f8a9b0c1
Revises: c4d5e6f7a8b9
Create Date: 2026-02-25 11:05:00.000000
"""

from __future__ import annotations

from datetime import datetime

from alembic import op
import sqlalchemy as sa


revision = "d6e7f8a9b0c1"
down_revision = "c4d5e6f7a8b9"
branch_labels = None
depends_on = None


def _table_exists(inspector: sa.Inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _column_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    return {str(col["name"]) for col in inspector.get_columns(table_name)}


def _find_template_id_by_code(bind: sa.Connection, code: str) -> int | None:
    row = bind.execute(
        sa.text(
            """
            SELECT id
            FROM print_templates
            WHERE lower(code) = :code
            ORDER BY id ASC
            LIMIT 1
            """
        ),
        {"code": code.strip().lower()},
    ).first()
    if row is None:
        return None
    return int(row[0])


def _ensure_system_template(
    bind: sa.Connection,
    *,
    code: str,
    description: str,
    document_type: str,
    template_format: str,
    fallback_content: str,
    legacy_codes: tuple[str, ...] = (),
) -> None:
    target_id = _find_template_id_by_code(bind, code)

    if target_id is None:
        for legacy_code in legacy_codes:
            legacy_id = _find_template_id_by_code(bind, legacy_code)
            if legacy_id is not None:
                has_conflict = _find_template_id_by_code(bind, code)
                if has_conflict is None:
                    bind.execute(
                        sa.text(
                            """
                            UPDATE print_templates
                            SET code = :new_code
                            WHERE id = :template_id
                            """
                        ),
                        {
                            "new_code": code,
                            "template_id": legacy_id,
                        },
                    )
                    target_id = legacy_id
                break

    if target_id is None:
        now = datetime.utcnow()
        bind.execute(
            sa.text(
                """
                INSERT INTO print_templates
                    (code, description, document_type, format, content, is_system, is_active, created_at, updated_at)
                VALUES
                    (:code, :description, :document_type, :template_format, :content, 1, 1, :created_at, :updated_at)
                """
            ),
            {
                "code": code,
                "description": description,
                "document_type": document_type,
                "template_format": template_format,
                "content": fallback_content,
                "created_at": now,
                "updated_at": now,
            },
        )
        target_id = _find_template_id_by_code(bind, code)

    if target_id is None:
        return

    bind.execute(
        sa.text(
            """
            UPDATE print_templates
            SET
                description = :description,
                document_type = :document_type,
                format = :template_format,
                is_system = 1,
                is_active = 1,
                content = CASE
                    WHEN content IS NULL OR trim(content) = '' THEN :fallback_content
                    ELSE content
                END
            WHERE id = :template_id
            """
        ),
        {
            "description": description,
            "document_type": document_type,
            "template_format": template_format,
            "fallback_content": fallback_content,
            "template_id": target_id,
        },
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _table_exists(inspector, "print_templates"):
        return
    columns = _column_names(inspector, "print_templates")
    required = {
        "code",
        "description",
        "document_type",
        "format",
        "content",
        "is_system",
        "is_active",
    }
    if not required.issubset(columns):
        return

    _ensure_system_template(
        bind,
        code="TICKET_THERMAL_SYSTEM",
        description="Thermal Ticket (System)",
        document_type="TICKET",
        template_format="TEXT",
        fallback_content="Ticket: {{ payload.ticket_no }}",
        legacy_codes=("TICKET_DEFAULT",),
    )
    _ensure_system_template(
        bind,
        code="TICKET_A4_SYSTEM",
        description="A4 Ticket (System)",
        document_type="TICKET",
        template_format="HTML",
        fallback_content="<html><body><h1>Ticket {{ payload.ticket_no }}</h1></body></html>",
    )
    _ensure_system_template(
        bind,
        code="INVOICE_SYSTEM",
        description="Invoice (System)",
        document_type="INVOICE",
        template_format="HTML",
        fallback_content="<html><body><h1>Invoice {{ invoice.invoice_no }}</h1></body></html>",
        legacy_codes=("INVOICE_DEFAULT", "INVOICE_A4_DEFAULT", "INV_A4_STANDARD"),
    )
    _ensure_system_template(
        bind,
        code="WTN_SYSTEM",
        description="Waste Transfer Note (System)",
        document_type="WTN",
        template_format="HTML",
        fallback_content="<html><body><h1>Waste Transfer Note</h1><p>Reference: {{ payload.wtn_no or '-' }}</p></body></html>",
        legacy_codes=("WTN_DEFAULT",),
    )

    # Keep legacy-named templates available for backward compatibility, but they
    # are no longer marked as system templates once canonical system rows exist.
    bind.execute(
        sa.text(
            """
            UPDATE print_templates
            SET is_system = 0
            WHERE lower(code) IN ('ticket_default', 'invoice_default', 'invoice_a4_default', 'inv_a4_standard', 'wtn_default')
              AND lower(code) NOT IN ('ticket_thermal_system', 'invoice_system', 'wtn_system', 'ticket_a4_system')
            """
        )
    )


def downgrade() -> None:
    # Data-shape migration only; no destructive downgrade.
    pass
