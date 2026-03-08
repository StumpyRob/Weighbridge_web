from __future__ import annotations

from datetime import datetime
import re

from alembic import op
import sqlalchemy as sa


revision = "07b8c9d0e1f2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None

_TICKET_NUMBER_RE = re.compile(r"^(?P<yy>\d{2})-(?P<number>\d{5})$")


def _coerce_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if value is None:
        return None
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _ticket_sequence_rows(bind) -> list[dict[str, object]]:
    rows = bind.execute(
        sa.text(
            "SELECT tenant_id, datetime, ticket_no, updated_at, created_at "
            "FROM tickets WHERE tenant_id IS NOT NULL AND ticket_no IS NOT NULL"
        )
    ).mappings()

    grouped: dict[tuple[int, int], dict[str, object]] = {}
    for row in rows:
        tenant_id = row.get("tenant_id")
        ticket_datetime = _coerce_datetime(row.get("datetime"))
        ticket_no = str(row.get("ticket_no") or "").strip()
        if tenant_id is None or ticket_datetime is None or not ticket_no:
            continue

        match = _TICKET_NUMBER_RE.fullmatch(ticket_no)
        if match is None:
            continue

        year = int(ticket_datetime.year)
        if match.group("yy") != f"{year % 100:02d}":
            continue

        sequence_number = int(match.group("number"))
        updated_at = (
            _coerce_datetime(row.get("updated_at"))
            or _coerce_datetime(row.get("created_at"))
            or ticket_datetime
            or datetime.utcnow()
        )
        key = (int(tenant_id), year)
        existing = grouped.get(key)
        if existing is None or sequence_number > int(existing["last_number"]):
            grouped[key] = {
                "tenant_id": int(tenant_id),
                "year": year,
                "last_number": sequence_number,
                "updated_at": updated_at,
            }

    return list(grouped.values())


def upgrade() -> None:
    bind = op.get_bind()
    ticket_sequence_rows = _ticket_sequence_rows(bind)

    op.drop_table("ticket_sequences")
    op.create_table(
        "ticket_sequences",
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("last_number", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "year"),
    )

    for row in ticket_sequence_rows:
        bind.execute(
            sa.text(
                "INSERT INTO ticket_sequences (tenant_id, year, last_number, updated_at) "
                "VALUES (:tenant_id, :year, :last_number, :updated_at)"
            ),
            row,
        )


def downgrade() -> None:
    bind = op.get_bind()
    grouped_rows = bind.execute(
        sa.text(
            "SELECT year, MAX(last_number) AS last_number, MAX(updated_at) AS updated_at "
            "FROM ticket_sequences GROUP BY year"
        )
    ).mappings()

    op.drop_table("ticket_sequences")
    op.create_table(
        "ticket_sequences",
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("last_number", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("year"),
    )

    for row in grouped_rows:
        bind.execute(
            sa.text(
                "INSERT INTO ticket_sequences (year, last_number, updated_at) "
                "VALUES (:year, :last_number, :updated_at)"
            ),
            {
                "year": int(row["year"]),
                "last_number": int(row["last_number"] or 0),
                "updated_at": _coerce_datetime(row["updated_at"]) or datetime.utcnow(),
            },
        )
