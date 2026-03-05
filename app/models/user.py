from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, event
from sqlalchemy.orm import Mapped, Session, mapped_column

from .base import Base, utcnow
from ..user_roles import ROLE_SUPERADMIN, ROLE_TENANT_ADMIN, ROLE_USER, normalize_role


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        sa.Index("ix_users_tenant_id", "tenant_id"),
        sa.Index("ix_users_role", "role"),
        sa.Index(
            "uq_users_tenant_username",
            "tenant_id",
            "username",
            unique=True,
            sqlite_where=sa.text("tenant_id IS NOT NULL"),
            postgresql_where=sa.text("tenant_id IS NOT NULL"),
        ),
        sa.Index(
            "uq_users_platform_username",
            "username",
            unique=True,
            sqlite_where=sa.text("tenant_id IS NULL"),
            postgresql_where=sa.text("tenant_id IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(150), nullable=False)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenants.id"), nullable=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default=ROLE_USER)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

def _session_platform_user_count(session: Session) -> int:
    return int(
        session.execute(
            sa.select(sa.func.count(User.id))
            .execution_options(skip_tenant_scope=True)
            .where(User.tenant_id.is_(None))
        ).scalar_one_or_none()
        or 0
    )


def _session_platform_superadmin_count(session: Session) -> int:
    return int(
        session.execute(
            sa.select(sa.func.count(User.id))
            .execution_options(skip_tenant_scope=True)
            .where(
                User.tenant_id.is_(None),
                sa.func.lower(sa.func.trim(User.role)) == ROLE_SUPERADMIN,
            )
        ).scalar_one_or_none()
        or 0
    )


def _normalize_user_for_write(target: User) -> None:
    tenant_id = getattr(target, "tenant_id", None)
    normalized = normalize_role(getattr(target, "role", None), default=None)

    if normalized is None:
        normalized = ROLE_USER if tenant_id is None else ROLE_TENANT_ADMIN

    if normalized == ROLE_SUPERADMIN:
        target.tenant_id = None
    target.role = normalized


@event.listens_for(Session, "before_flush")
def _assign_new_user_roles_before_flush(session: Session, flush_context, instances) -> None:
    new_users = [item for item in session.new if isinstance(item, User)]
    if not new_users:
        return

    ordered_users = sorted(
        new_users,
        key=lambda item: int(getattr(sa.inspect(item), "insert_order", 0) or 0),
    )
    bootstrap_available = (
        _session_platform_superadmin_count(session) == 0
        and _session_platform_user_count(session) == 0
    )
    bootstrap_assigned = False

    for user in ordered_users:
        tenant_id = getattr(user, "tenant_id", None)
        normalized = normalize_role(getattr(user, "role", None), default=None)
        if bootstrap_available and (not bootstrap_assigned) and tenant_id is None:
            normalized = ROLE_SUPERADMIN
            bootstrap_assigned = True
        elif normalized is None:
            normalized = ROLE_USER if tenant_id is None else ROLE_TENANT_ADMIN

        if normalized == ROLE_SUPERADMIN:
            user.tenant_id = None
        user.role = normalized


@event.listens_for(User, "before_insert")
def _normalize_user_before_insert(mapper, connection, target: User) -> None:
    _normalize_user_for_write(target)


@event.listens_for(User, "before_update")
def _normalize_user_before_update(mapper, connection, target: User) -> None:
    _normalize_user_for_write(target)
