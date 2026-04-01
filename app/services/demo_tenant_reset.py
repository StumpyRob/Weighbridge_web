from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, time, timedelta
import logging
from pathlib import Path
import shutil

from fastapi import Request
from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session

from ..audit import log as audit_log
from ..auth import hash_password, user_identity_kwargs
from ..models import (
    AIUsageLog,
    Area,
    AuditEvent,
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
    PrintAgent,
    PrintAgentPairing,
    PrintDestination,
    PrintJob,
    PrintTemplate,
    PrintTemplateVersion,
    Product,
    ProductGroup,
    Tenant,
    Ticket,
    TicketSequence,
    TicketVoid,
    Unit,
    User,
    Vehicle,
    VehicleTare,
    Yard,
)
from ..models.base import utcnow
from ..seed import force_refresh_system_print_templates, seed_print_destinations, seed_units
from ..user_roles import ROLE_TENANT_ADMIN
from .demo_dataset import DEMO_SIGNATURE_DATA_URI, seed_demo_dataset
from .system_setup import (
    DEFAULT_YARD_NAME,
    ensure_company_settings_row_exists,
    seed_required_reference_data,
    upsert_default_yard,
)
from .tenants import is_reserved_demo_tenant
from .uploads import company_logo_upload_dir

logger = logging.getLogger(__name__)

DEMO_DEFAULT_EMAIL = "demo@demo.com"
DEMO_DEFAULT_PASSWORD = "password"
DEMO_DEFAULT_FIRST_NAME = "Demo"
DEMO_DEFAULT_LAST_NAME = "Admin"
DEMO_DEFAULT_SIGNATURE_NAME = f"{DEMO_DEFAULT_FIRST_NAME} {DEMO_DEFAULT_LAST_NAME}".strip()
DEMO_RESET_INTERVAL_DAYS_MIN = 1
DEMO_RESET_INTERVAL_DAYS_MAX = 365
DEMO_RESET_DEFAULT_TIME_MINUTES = 180
DEMO_COMPANY_NAME = "Demo Ltd."
DEMO_COMPANY_ADDRESS_LINE_1 = "1 Chapter House Street"
DEMO_COMPANY_CITY = "York"
DEMO_COMPANY_POSTCODE = "YO1 7JH"
DEMO_COMPANY_COUNTRY = "United Kingdom"
DEMO_PRIMARY_COLOR_HEX = "#2596BE"
DEMO_NAVBAR_COLOR_HEX = "#242B3B"
DEMO_LOGO_FILENAME = "demo-logo.png"
DEMO_LOGO_WEB_PATH = f"/static/uploads/company/{DEMO_LOGO_FILENAME}"
DEMO_LOGO_SOURCE = (
    Path(__file__).resolve().parents[1] / "static" / "uploads" / "company" / DEMO_LOGO_FILENAME
)

_DELETE_CASCADE_MODELS = (
    PrintJob,
    PrintAgentPairing,
    PrintAgent,
    CustomerProductPrice,
    CustomerAdjustment,
    VehicleTare,
    InvoiceVoid,
    TicketVoid,
    InvoiceLine,
    Ticket,
    TicketSequence,
    Invoice,
    Vehicle,
    Product,
    ProductGroup,
    Unit,
    Customer,
    Haulier,
    Driver,
    Container,
    Destination,
    Area,
    Yard,
    PrintDestination,
    PrintTemplateVersion,
    PrintTemplate,
    CompanySetting,
)


@contextmanager
def _tenant_scope(db: Session, tenant_id: int):
    previous_tenant_id = db.info.get("tenant_id")
    previous_platform_mode = db.info.get("platform_mode")
    db.info["tenant_id"] = int(tenant_id)
    db.info["platform_mode"] = False
    try:
        yield
    finally:
        db.info["tenant_id"] = previous_tenant_id
        db.info["platform_mode"] = previous_platform_mode


def _seed_number_sequences(db: Session, tenant_id: int) -> None:
    now = utcnow()
    year = int(now.year)
    dialect = str(getattr(getattr(db.get_bind(), "dialect", None), "name", "") or "").lower()
    if dialect == "postgresql":
        db.execute(
            text(
                "INSERT INTO ticket_sequences (tenant_id, year, last_number, updated_at) "
                "VALUES (:tenant_id, :year, 0, :updated_at) "
                "ON CONFLICT (tenant_id, year) DO NOTHING"
            ),
            {"tenant_id": int(tenant_id), "year": year, "updated_at": now},
        )
        db.execute(
            text(
                "INSERT INTO invoice_sequences (year, last_number, updated_at) "
                "VALUES (:year, 0, :updated_at) ON CONFLICT (year) DO NOTHING"
            ),
            {"year": year, "updated_at": now},
        )
        return

    db.execute(
        text(
            "INSERT OR IGNORE INTO ticket_sequences (tenant_id, year, last_number, updated_at) "
            "VALUES (:tenant_id, :year, 0, :updated_at)"
        ),
        {"tenant_id": int(tenant_id), "year": year, "updated_at": now},
    )
    db.execute(
        text(
            "INSERT OR IGNORE INTO invoice_sequences (year, last_number, updated_at) "
            "VALUES (:year, 0, :updated_at)"
        ),
        {"year": year, "updated_at": now},
    )


def _seed_tenant_baseline(
    db: Session,
    tenant_id: int,
    *,
    company_name: str | None = None,
    include_shared_reference_data: bool = False,
) -> None:
    with _tenant_scope(db, tenant_id):
        company = ensure_company_settings_row_exists(db)
        resolved_company_name = str(company_name or "").strip()
        if resolved_company_name:
            company.name = resolved_company_name
        elif not str(company.name or "").strip():
            company.name = "Your Company Name"
        upsert_default_yard(db, yard_name=DEFAULT_YARD_NAME)
        if include_shared_reference_data:
            seed_required_reference_data(db)
        else:
            seed_units(db)
        force_refresh_system_print_templates(db)
        seed_print_destinations(db)
        company.is_initialized = True
    _seed_number_sequences(db, tenant_id)


def _apply_demo_company_branding(db: Session, tenant_id: int) -> None:
    with _tenant_scope(db, tenant_id):
        company = ensure_company_settings_row_exists(db)
        company.name = DEMO_COMPANY_NAME
        company.address_line1 = DEMO_COMPANY_ADDRESS_LINE_1
        company.address_line2 = None
        company.city = DEMO_COMPANY_CITY
        company.postcode = DEMO_COMPANY_POSTCODE
        company.country = DEMO_COMPANY_COUNTRY
        company.navbar_color_hex = DEMO_NAVBAR_COLOR_HEX
        company.primary_color_hex = DEMO_PRIMARY_COLOR_HEX
        company.nav_logo_height_px = 34
        company.show_nav_logo = True
        company.show_nav_title = True

        if DEMO_LOGO_SOURCE.is_file():
            logo_dir = company_logo_upload_dir(tenant_id, create=True)
            logo_target = logo_dir / DEMO_LOGO_FILENAME
            shutil.copyfile(DEMO_LOGO_SOURCE, logo_target)
            company.company_logo_path = DEMO_LOGO_WEB_PATH
            company.company_logo_updated_at = utcnow()


def parse_demo_reset_interval_days(raw_value: object) -> int | None:
    candidate = str(raw_value or "").strip()
    if candidate in {"", "0"}:
        return None
    try:
        days = int(candidate)
    except (TypeError, ValueError) as exc:
        raise ValueError("Enter a whole number of days between 1 and 365, or leave blank to disable.") from exc
    if not (DEMO_RESET_INTERVAL_DAYS_MIN <= days <= DEMO_RESET_INTERVAL_DAYS_MAX):
        raise ValueError("Enter a whole number of days between 1 and 365, or leave blank to disable.")
    return days


def parse_demo_reset_time_minutes(raw_value: object) -> int | None:
    candidate = str(raw_value or "").strip()
    if not candidate:
        return None
    parts = candidate.split(":", 1)
    if len(parts) != 2:
        raise ValueError("Enter a valid reset time in HH:MM format.")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except (TypeError, ValueError) as exc:
        raise ValueError("Enter a valid reset time in HH:MM format.") from exc
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError("Enter a valid reset time in HH:MM format.")
    return (hour * 60) + minute


def demo_reset_interval_days_value(tenant: Tenant | None) -> int | None:
    if tenant is None:
        return None
    raw_value = getattr(tenant, "demo_reset_interval_days", None)
    if raw_value is None:
        return None
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return None
    if value < DEMO_RESET_INTERVAL_DAYS_MIN:
        return None
    return value


def demo_reset_time_minutes_value(tenant: Tenant | None) -> int | None:
    if tenant is None:
        return None
    raw_value = getattr(tenant, "demo_reset_time_minutes", None)
    if raw_value is None:
        return (
            DEMO_RESET_DEFAULT_TIME_MINUTES
            if demo_reset_interval_days_value(tenant) is not None
            else None
        )
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return DEMO_RESET_DEFAULT_TIME_MINUTES
    if value < 0 or value >= (24 * 60):
        return DEMO_RESET_DEFAULT_TIME_MINUTES
    return value


def format_demo_reset_time_input(minutes: int | None) -> str:
    if minutes is None:
        return ""
    resolved = max(0, min(int(minutes), (24 * 60) - 1))
    hour, minute = divmod(resolved, 60)
    return f"{hour:02d}:{minute:02d}"


def next_demo_reset_at(tenant: Tenant | None) -> datetime | None:
    if not is_reserved_demo_tenant(tenant):
        return None
    interval_days = demo_reset_interval_days_value(tenant)
    reset_time_minutes = demo_reset_time_minutes_value(tenant)
    if interval_days is None or reset_time_minutes is None:
        return None
    baseline = getattr(tenant, "demo_last_reset_at", None) or getattr(tenant, "created_at", None)
    if baseline is None:
        return None
    hour, minute = divmod(reset_time_minutes, 60)
    scheduled = datetime.combine(
        baseline.date() + timedelta(days=interval_days),
        time(hour=hour, minute=minute),
    )
    while scheduled <= baseline:
        scheduled += timedelta(days=interval_days)
    return scheduled


def demo_reset_due_now(tenant: Tenant | None, *, now: datetime | None = None) -> bool:
    scheduled = next_demo_reset_at(tenant)
    if scheduled is None:
        return False
    return scheduled <= (now or utcnow())


def format_demo_reset_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.strftime("%d/%m/%Y %H:%M")


def should_auto_reset_demo_tenant(tenant: Tenant | None, *, now=None) -> bool:
    return demo_reset_due_now(tenant, now=now or utcnow())


def _create_default_demo_user(db: Session, tenant_id: int) -> User:
    signature_updated_at = utcnow()
    existing = (
        db.execute(
            select(User)
            .where(
                User.tenant_id == int(tenant_id),
                User.email == DEMO_DEFAULT_EMAIL,
            )
            .limit(1)
        )
        .scalars()
        .first()
    )
    if existing is not None:
        existing.first_name = DEMO_DEFAULT_FIRST_NAME
        existing.last_name = DEMO_DEFAULT_LAST_NAME
        existing.role = ROLE_TENANT_ADMIN
        existing.password_hash = hash_password(DEMO_DEFAULT_PASSWORD)
        existing.is_active = True
        existing.saved_signature_data_uri = DEMO_SIGNATURE_DATA_URI
        existing.saved_signature_signer_name = DEMO_DEFAULT_SIGNATURE_NAME
        existing.saved_signature_updated_at = signature_updated_at
        return existing

    user = User(
        **user_identity_kwargs(email=DEMO_DEFAULT_EMAIL, role=ROLE_TENANT_ADMIN),
        first_name=DEMO_DEFAULT_FIRST_NAME,
        last_name=DEMO_DEFAULT_LAST_NAME,
        password_hash=hash_password(DEMO_DEFAULT_PASSWORD),
        is_active=True,
        saved_signature_data_uri=DEMO_SIGNATURE_DATA_URI,
        saved_signature_signer_name=DEMO_DEFAULT_SIGNATURE_NAME,
        saved_signature_updated_at=signature_updated_at,
        tenant_id=int(tenant_id),
    )
    db.add(user)
    db.flush()
    return user


def reset_demo_tenant_data(
    db: Session,
    request: Request | None,
    *,
    tenant: Tenant,
    current_user: User | None = None,
    reset_reason: str = "manual",
) -> dict[str, int]:
    tenant_id = int(tenant.id)
    tenant_upload_dir = company_logo_upload_dir(tenant_id, create=False).parent
    users = list(
        db.execute(
            select(User)
            .where(User.tenant_id == tenant_id)
            .order_by(User.id.asc())
        ).scalars()
    )
    user_ids = [int(user.id) for user in users if getattr(user, "id", None) is not None]

    if user_ids:
        db.execute(
            update(AuditEvent)
            .where(AuditEvent.user_id.in_(user_ids))
            .values(user_id=None)
        )

    shutil.rmtree(tenant_upload_dir, ignore_errors=True)

    db.execute(delete(AuditEvent).where(AuditEvent.tenant_id == str(tenant_id)))
    db.execute(delete(AIUsageLog).where(AIUsageLog.tenant_id == tenant_id))
    for model in _DELETE_CASCADE_MODELS:
        db.execute(delete(model).where(model.tenant_id == tenant_id))
    db.execute(delete(User).where(User.tenant_id == tenant_id))

    tenant.is_active = True
    _seed_tenant_baseline(
        db,
        tenant_id,
        company_name=str(tenant.name or "").strip(),
        include_shared_reference_data=False,
    )
    _apply_demo_company_branding(db, tenant_id)
    dataset_counts = seed_demo_dataset(db, tenant_id)

    default_demo_user_created = False
    if is_reserved_demo_tenant(tenant):
        _create_default_demo_user(db, tenant_id)
        default_demo_user_created = True

    tenant.demo_last_reset_at = utcnow()
    audit_log(
        db,
        request,
        action="TENANT_RESET_DEMO",
        entity_type="tenant",
        entity_id=tenant.id,
        summary=f"Reset demo tenant {tenant.name}",
        details={
            "subdomain": tenant.subdomain,
            "deleted_user_count": len(users),
            "reseeded": True,
            "reason": reset_reason,
            "default_demo_user_created": default_demo_user_created,
            "dataset": dataset_counts,
        },
        user=current_user,
        tenant_id=None,
    )
    db.commit()
    return dataset_counts


def maybe_auto_reset_demo_tenant(
    db: Session,
    request: Request,
    *,
    tenant: Tenant | None,
) -> bool:
    if not should_auto_reset_demo_tenant(tenant):
        return False
    if tenant is None:
        return False
    try:
        reset_demo_tenant_data(
            db,
            request,
            tenant=tenant,
            current_user=None,
            reset_reason="automatic",
        )
        return True
    except Exception:
        db.rollback()
        logger.exception("Automatic demo reset failed for tenant_id=%s", getattr(tenant, "id", None))
        return False
