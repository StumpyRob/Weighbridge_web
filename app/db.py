from collections.abc import Generator

import sqlalchemy as sa
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker, with_loader_criteria

from .config import settings
from .models import (
    AIUsageLog,
    Area,
    CompanySetting,
    Container,
    Customer,
    CustomerAdjustment,
    CustomerProductPrice,
    Destination,
    Driver,
    Haulier,
    Invoice,
    InvoiceLine,
    InvoiceVoid,
    PrintDestination,
    PrintJob,
    PrintTemplate,
    PrintTemplateVersion,
    Product,
    ProductGroup,
    Ticket,
    TicketVoid,
    Unit,
    User,
    Vehicle,
    VehicleTare,
    Yard,
)
from .tenancy import current_platform_mode, current_tenant_id, is_tenant_scoped_entity

TENANT_FILTER_MODELS = (
    AIUsageLog,
    Area,
    CompanySetting,
    Container,
    Customer,
    CustomerAdjustment,
    CustomerProductPrice,
    Destination,
    Driver,
    Haulier,
    Invoice,
    InvoiceLine,
    InvoiceVoid,
    PrintDestination,
    PrintJob,
    PrintTemplate,
    PrintTemplateVersion,
    Product,
    ProductGroup,
    Ticket,
    TicketVoid,
    Unit,
    User,
    Vehicle,
    VehicleTare,
    Yard,
)


class TenantSession(Session):
    def get(self, entity, ident, **kwargs):  # type: ignore[override]
        platform_mode = bool(self.info.get("platform_mode", False))
        tenant_id = self.info.get("tenant_id")
        if platform_mode or tenant_id is None or not is_tenant_scoped_entity(entity):
            return super().get(entity, ident, **kwargs)

        mapper = sa.inspect(entity)
        primary_keys = mapper.primary_key
        if len(primary_keys) != 1:
            return super().get(entity, ident, **kwargs)

        identity_value = ident[0] if isinstance(ident, tuple) else ident
        statement = sa.select(entity).where(
            primary_keys[0] == identity_value,
            entity.tenant_id == int(tenant_id),
        )

        options = kwargs.get("options")
        if options:
            statement = statement.options(*options)

        return self.execute(statement).scalars().first()


engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
    class_=TenantSession,
)


@event.listens_for(TenantSession, "do_orm_execute")
def _add_tenant_filter(execute_state) -> None:
    if not execute_state.is_select:
        return
    if execute_state.execution_options.get("skip_tenant_scope"):
        return

    session = execute_state.session
    platform_mode = bool(session.info.get("platform_mode", False))
    tenant_id = session.info.get("tenant_id")
    if platform_mode or tenant_id is None:
        return
    tenant_id_value = int(tenant_id)

    statement = execute_state.statement
    for model in TENANT_FILTER_MODELS:
        statement = statement.options(
            with_loader_criteria(
                model,
                lambda cls: cls.tenant_id == tenant_id_value,
                include_aliases=True,
            )
        )
    execute_state.statement = statement


@event.listens_for(TenantSession, "before_flush")
def _stamp_tenant_id(session: TenantSession, _flush_context, _instances) -> None:
    platform_mode = bool(session.info.get("platform_mode", False))
    tenant_id = session.info.get("tenant_id")

    for instance in session.new:
        if not is_tenant_scoped_entity(type(instance)):
            continue

        value = getattr(instance, "tenant_id", None)
        if value is None:
            if platform_mode or tenant_id is None:
                continue
            setattr(instance, "tenant_id", int(tenant_id))
            continue

        if not platform_mode and tenant_id is not None and int(value) != int(tenant_id):
            raise ValueError("Cross-tenant write blocked.")


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    db.info["tenant_id"] = current_tenant_id()
    db.info["platform_mode"] = current_platform_mode()
    try:
        yield db
    finally:
        db.close()
