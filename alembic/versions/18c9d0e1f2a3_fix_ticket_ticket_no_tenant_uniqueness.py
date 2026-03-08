from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "18c9d0e1f2a3"
down_revision = "07b8c9d0e1f2"
branch_labels = None
depends_on = None

_TICKETS_TABLE = "tickets"
_LEGACY_GLOBAL_TICKET_CONSTRAINTS = (
    "tickets_ticket_no_key",
    "uq_tickets_ticket_no",
)
_COMPOSITE_TICKET_CONSTRAINT = "uq_tickets_tenant_ticket_no"
_GLOBAL_DOWNGRADE_CONSTRAINT = "uq_tickets_ticket_no"


def _normalized_columns(columns: object) -> list[str]:
    return [str(column or "").strip().lower() for column in list(columns or [])]


def _ticket_uniqueness_state(bind) -> tuple[set[str], set[str], bool]:
    inspector = sa.inspect(bind)
    legacy_constraint_names: set[str] = set()
    legacy_index_names: set[str] = set()
    composite_exists = False

    for constraint in inspector.get_unique_constraints(_TICKETS_TABLE):
        columns = _normalized_columns(constraint.get("column_names"))
        name = str(constraint.get("name") or "").strip()
        if columns == ["tenant_id", "ticket_no"]:
            composite_exists = True
        elif columns == ["ticket_no"] and name:
            legacy_constraint_names.add(name)

    for index in inspector.get_indexes(_TICKETS_TABLE):
        if not bool(index.get("unique", False)):
            continue
        columns = _normalized_columns(index.get("column_names"))
        name = str(index.get("name") or "").strip()
        if columns == ["tenant_id", "ticket_no"]:
            composite_exists = True
        elif columns == ["ticket_no"] and name:
            legacy_index_names.add(name)

    return legacy_constraint_names, legacy_index_names, composite_exists


def upgrade() -> None:
    bind = op.get_bind()
    dialect = str(bind.dialect.name or "").strip().lower()
    legacy_constraint_names, legacy_index_names, composite_exists = _ticket_uniqueness_state(
        bind
    )

    if dialect == "postgresql":
        for name in sorted(set(legacy_constraint_names).union(_LEGACY_GLOBAL_TICKET_CONSTRAINTS)):
            op.execute(sa.text(f'ALTER TABLE {_TICKETS_TABLE} DROP CONSTRAINT IF EXISTS "{name}"'))
        for name in sorted(legacy_index_names):
            op.execute(sa.text(f'DROP INDEX IF EXISTS "{name}"'))
    else:
        with op.batch_alter_table(_TICKETS_TABLE, schema=None) as batch_op:
            for name in sorted(legacy_constraint_names):
                batch_op.drop_constraint(name, type_="unique")
            for name in sorted(legacy_index_names):
                batch_op.drop_index(name)

    if not composite_exists:
        op.create_unique_constraint(
            _COMPOSITE_TICKET_CONSTRAINT,
            _TICKETS_TABLE,
            ["tenant_id", "ticket_no"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    duplicate_ticket_no = bind.execute(
        sa.text(
            "SELECT ticket_no "
            "FROM tickets "
            "WHERE ticket_no IS NOT NULL "
            "GROUP BY ticket_no "
            "HAVING COUNT(*) > 1 "
            "LIMIT 1"
        )
    ).scalar_one_or_none()
    if duplicate_ticket_no is not None:
        raise RuntimeError(
            "Cannot downgrade ticket ticket_no uniqueness because tenant-scoped duplicates already exist."
        )

    legacy_constraint_names, legacy_index_names, composite_exists = _ticket_uniqueness_state(bind)
    dialect = str(bind.dialect.name or "").strip().lower()

    if composite_exists:
        if dialect == "postgresql":
            op.execute(
                sa.text(
                    f'ALTER TABLE {_TICKETS_TABLE} DROP CONSTRAINT IF EXISTS "{_COMPOSITE_TICKET_CONSTRAINT}"'
                )
            )
        else:
            with op.batch_alter_table(_TICKETS_TABLE, schema=None) as batch_op:
                batch_op.drop_constraint(_COMPOSITE_TICKET_CONSTRAINT, type_="unique")

    if dialect == "postgresql":
        for name in sorted(legacy_constraint_names):
            op.execute(sa.text(f'ALTER TABLE {_TICKETS_TABLE} DROP CONSTRAINT IF EXISTS "{name}"'))
        for name in sorted(legacy_index_names):
            op.execute(sa.text(f'DROP INDEX IF EXISTS "{name}"'))
    else:
        with op.batch_alter_table(_TICKETS_TABLE, schema=None) as batch_op:
            for name in sorted(legacy_constraint_names):
                batch_op.drop_constraint(name, type_="unique")
            for name in sorted(legacy_index_names):
                batch_op.drop_index(name)

    op.create_unique_constraint(
        _GLOBAL_DOWNGRADE_CONSTRAINT,
        _TICKETS_TABLE,
        ["ticket_no"],
    )
