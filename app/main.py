import logging
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
import secrets
from typing import Iterator

from fastapi import Depends, FastAPI, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .auth import (
    ROLE_SUPERADMIN,
    SESSION_PLATFORM_MODE_KEY,
    SESSION_ROLE_KEY,
    SESSION_TENANT_ID_KEY,
    SESSION_USER_ID_KEY,
    ensure_user_role,
    is_superadmin_user,
    require_user,
)
from .config import settings
from .db import get_db
from .models import Customer, Invoice, Product, Ticket, TicketStatusEnum, User, Vehicle
from .models.base import utcnow
from .routes import api_router
from .routers.lookups import router as lookups_router
from .security_hardening import (
    CSRF_COOKIE_NAME,
    CSRF_FORM_FIELD,
    CSRF_HEADER_NAME,
    apply_security_headers,
    csrf_forbidden_response,
    generate_csrf_token,
    is_state_changing_method,
    set_csrf_cookie,
    validate_production_secret,
)
from .services.system_setup import get_company_setting, missing_required_lookup_messages
from .services.tenants import ensure_demo_tenant, get_tenant_by_subdomain
from .services.uploads import company_logo_upload_dir
from .services.credit import (
    INVOICE_OUTSTANDING_EXCLUDED_STATUSES,
    INVOICE_OUTSTANDING_ISSUED_STATUSES,
)
from .services.pdf import check_invoice_pdf_renderer
from .services.ui_branding import get_branding, nav_foreground_color, normalize_hex_color
from .tenancy import (
    host_without_port,
    prefix_tenant_route_target,
    reset_request_tenant_context,
    resolve_subdomain,
    set_request_tenant_context,
    split_tenant_route_path,
    tenant_route_prefix,
)
from .templating import templates

logger = logging.getLogger(__name__)

_SYSTEM_GUARD_PREFIXES = (
    "/tickets",
    "/customers",
    "/vehicles",
    "/products",
    "/invoices",
    "/lookups",
)
_LOGIN_REQUIRED_PREFIXES = (
    "/tickets",
    "/customers",
    "/vehicles",
    "/products",
    "/invoices",
    "/lookups",
    "/admin",
    "/platform",
)
_LOGIN_EXEMPT_PATHS = (
    "/bootstrap",
    "/platform/bootstrap",
)
_UPLOADS_STATIC_PREFIX = "/static/uploads/"
_PUBLIC_ALLOWED_EXACT_PATHS = {
    "/",
}
_PUBLIC_ALLOWED_PREFIXES = (
    "/t/",
    "/static/",
    "/media/",
)
_TENANT_ONLY_PREFIXES = (
    "/tickets",
    "/customers",
    "/vehicles",
    "/products",
    "/invoices",
    "/lookups",
    "/setup",
    "/admin/company",
    "/admin/printing",
)
_PLATFORM_ONLY_PREFIXES = (
    "/platform",
    "/admin/tenants",
    "/admin/system-status",
    "/admin/dev-mode",
    "/bootstrap",
)
_LEGACY_SINGLE_HOSTS = {
    "localhost",
    "127.0.0.1",
    "testserver",
}


class _StaticFilesWithoutSharedUploads(StaticFiles):
    async def get_response(self, path: str, scope) -> Response:
        normalized = str(path or "").replace("\\", "/").lstrip("/").lower()
        if normalized == "uploads/company" or normalized.startswith("uploads/company/"):
            return PlainTextResponse("Not Found", status_code=404)
        return await super().get_response(path, scope)


def _strip_non_production_routes(app: FastAPI) -> None:
    filtered_routes = []
    for route in app.router.routes:
        path = str(getattr(route, "path", "")).lower()
        if path == "/admin/dev-mode":
            filtered_routes.append(route)
            continue
        if "debug" in path or "__" in path or "dev" in path:
            continue
        filtered_routes.append(route)
    app.router.routes = filtered_routes


def _is_exact_base_domain(host_name: str) -> bool:
    base_domain = settings.effective_base_domain
    return bool(base_domain and host_name == base_domain)


def _is_marketing_host(host_name: str) -> bool:
    base_domain = settings.effective_base_domain
    marketing_subdomain = settings.effective_marketing_subdomain
    return bool(base_domain and marketing_subdomain and host_name == f"{marketing_subdomain}.{base_domain}")


def _request_scope_path(request: Request) -> str:
    return str(request.scope.get("path", "") or "")


def _apply_tenant_route_redirect_prefix(request: Request, response: Response) -> Response:
    route_prefix = str(getattr(getattr(request, "state", None), "tenant_route_prefix", "") or "").strip()
    if not route_prefix:
        return response
    location = str(response.headers.get("location", "") or "").strip()
    if not location:
        return response
    scoped_location = prefix_tenant_route_target(route_prefix, location)
    if scoped_location and scoped_location != location:
        response.headers["location"] = scoped_location
    return response


def _public_host_mode(request: Request) -> bool:
    return bool(getattr(getattr(request, "state", None), "public_host_mode", False))


def _path_allowed_for_public_host(path: str) -> bool:
    target = str(path or "").strip() or "/"
    if target in _PUBLIC_ALLOWED_EXACT_PATHS:
        return True
    return any(target.startswith(prefix) for prefix in _PUBLIC_ALLOWED_PREFIXES)


def _ticket_status_value(status: TicketStatusEnum | str) -> str:
    return str(getattr(status, "value", status or "")).upper()


def _format_weight_kg(value: object) -> str:
    try:
        amount = Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        amount = Decimal("0")
    if amount == amount.to_integral_value():
        return f"{int(amount):,} kg"
    return f"{amount:,.3f} kg"


def _format_currency(value: object) -> str:
    try:
        amount = Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        amount = Decimal("0")
    return f"GBP {amount:,.2f}"


def _normalize_decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _datetime_bounds_for_day(target_day: date) -> tuple[datetime, datetime]:
    day_start = datetime.combine(target_day, time.min)
    next_day = datetime.combine(target_day + timedelta(days=1), time.min)
    return day_start, next_day


def _normalize_dashboard_period(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"today", "7d", "30d"}:
        return normalized
    return "7d"


def _dashboard_period_window(period: str, today: date) -> tuple[datetime, datetime]:
    today_start, tomorrow_start = _datetime_bounds_for_day(today)
    if period == "today":
        return today_start, tomorrow_start
    if period == "30d":
        return datetime.combine(today - timedelta(days=29), time.min), tomorrow_start
    return datetime.combine(today - timedelta(days=6), time.min), tomorrow_start


def _trim_decimal_text(value: Decimal) -> str:
    text = f"{value:f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _throughput_display_unit(max_weight_kg: Decimal, total_weight_kg: Decimal) -> str:
    threshold = max(max_weight_kg, total_weight_kg)
    return "tonnes" if threshold >= Decimal("1000") else "kg"


def _format_throughput_weight(value_kg: object, *, unit: str) -> str:
    amount = _normalize_decimal(value_kg)
    if unit == "tonnes":
        tonnes = (amount / Decimal("1000")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        return f"{_trim_decimal_text(tonnes)} tonnes"
    return _format_weight_kg(amount)


def _dashboard_ticket_rows(
    db: Session,
    *,
    limit: int = 6,
    status: str | None = None,
    include_void: bool = False,
    oldest_first: bool = False,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> list[dict[str, object]]:
    stmt = (
        select(Ticket, Vehicle, Customer, Product)
        .outerjoin(Vehicle, Ticket.vehicle_id == Vehicle.id)
        .outerjoin(Customer, Ticket.customer_id == Customer.id)
        .outerjoin(Product, Ticket.product_id == Product.id)
    )
    if date_from is not None:
        stmt = stmt.where(Ticket.datetime >= date_from)
    if date_to is not None:
        stmt = stmt.where(Ticket.datetime < date_to)
    if status:
        stmt = stmt.where(Ticket.status == status)
    elif not include_void:
        stmt = stmt.where(Ticket.status != _ticket_status_value(TicketStatusEnum.VOID))

    order_column = Ticket.datetime.asc() if oldest_first else Ticket.datetime.desc()
    stmt = stmt.order_by(order_column, Ticket.id.desc()).limit(limit)

    rows = []
    for ticket, vehicle, customer, product in db.execute(stmt).all():
        ticket_status = _ticket_status_value(ticket.status)
        rows.append(
            {
                "ticket_id": int(ticket.id),
                "ticket_no": str(ticket.ticket_no or ""),
                "status": ticket_status,
                "status_class": f"dashboard-status-pill--{ticket_status.lower()}",
                "datetime_display": ticket.datetime.strftime("%d/%m/%Y %H:%M"),
                "time_display": ticket.datetime.strftime("%H:%M"),
                "customer_name": (
                    str(customer.name or "").strip()
                    if customer is not None and str(customer.name or "").strip()
                    else "Unassigned"
                ),
                "vehicle_registration": (
                    str(vehicle.registration or "").strip()
                    if vehicle is not None and str(vehicle.registration or "").strip()
                    else str(ticket.vehicle_reg_text or "").strip() or "-"
                ),
                "product_name": (
                    str(product.description or "").strip()
                    if product is not None and str(product.description or "").strip()
                    else "-"
                ),
                "net_kg_display": _format_weight_kg(ticket.net_kg),
            }
        )
    return rows


def _dashboard_invoice_rows(
    db: Session,
    *,
    limit: int = 5,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[dict[str, object]]:
    stmt = (
        select(Invoice, Customer)
        .outerjoin(Customer, Invoice.customer_id == Customer.id)
        .order_by(Invoice.invoice_date.desc(), Invoice.id.desc())
        .limit(limit)
    )
    if date_from is not None:
        stmt = stmt.where(Invoice.invoice_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(Invoice.invoice_date < date_to)

    rows = []
    for invoice, customer in db.execute(stmt).all():
        status = str(invoice.status or "").upper()
        rows.append(
            {
                "invoice_no": str(invoice.invoice_no or ""),
                "invoice_date_display": invoice.invoice_date.strftime("%d/%m/%Y"),
                "customer_name": (
                    str(customer.name or "").strip()
                    if customer is not None and str(customer.name or "").strip()
                    else "Unassigned"
                ),
                "status": status or "-",
                "status_class": f"dashboard-status-pill--{status.lower()}" if status else "",
                "gross_total_display": _format_currency(invoice.gross_total),
            }
        )
    return rows


def _dashboard_invoice_ready_rows(
    db: Session,
    *,
    limit: int = 5,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> list[dict[str, object]]:
    stmt = (
        select(Ticket, Customer)
        .outerjoin(Customer, Ticket.customer_id == Customer.id)
        .where(
            Ticket.status == _ticket_status_value(TicketStatusEnum.COMPLETE),
            Ticket.invoice_id.is_(None),
            Ticket.dont_invoice.is_(False),
        )
        .order_by(Ticket.datetime.desc(), Ticket.id.desc())
        .limit(limit)
    )
    if date_from is not None:
        stmt = stmt.where(Ticket.datetime >= date_from)
    if date_to is not None:
        stmt = stmt.where(Ticket.datetime < date_to)

    rows = []
    for ticket, customer in db.execute(stmt).all():
        rows.append(
            {
                "ticket_id": int(ticket.id),
                "ticket_no": str(ticket.ticket_no or ""),
                "datetime_display": ticket.datetime.strftime("%d/%m/%Y %H:%M"),
                "customer_name": (
                    str(customer.name or "").strip()
                    if customer is not None and str(customer.name or "").strip()
                    else "Unassigned"
                ),
                "net_kg_display": _format_weight_kg(ticket.net_kg),
            }
        )
    return rows


def _build_dashboard_chart_points(
    activity_datetimes: list[datetime],
    *,
    period: str,
    today: date,
) -> tuple[list[dict[str, object]], int, str, str]:
    chart_points: list[dict[str, object]] = []
    max_count = 0

    if period == "today":
        activity_counts: dict[int, int] = {}
        for activity_datetime in activity_datetimes:
            bucket_hour = (activity_datetime.hour // 3) * 3
            activity_counts[bucket_hour] = activity_counts.get(bucket_hour, 0) + 1
        for start_hour in range(0, 24, 3):
            count = int(activity_counts.get(start_hour, 0))
            max_count = max(max_count, count)
            end_hour = min(start_hour + 3, 24)
            chart_points.append(
                {
                    "date_label": f"{start_hour:02d}:00-{end_hour:02d}:00",
                    "short_label": f"{start_hour:02d}",
                    "count": count,
                    "height_percent": 0,
                }
            )
        chart_title = "Ticket Activity Today"
        chart_empty_message = "No ticket activity has been recorded today."
    else:
        day_count = 30 if period == "30d" else 7
        start_day = today - timedelta(days=day_count - 1)
        activity_counts: dict[date, int] = {}
        for activity_datetime in activity_datetimes:
            activity_day = activity_datetime.date()
            activity_counts[activity_day] = activity_counts.get(activity_day, 0) + 1
        for day_offset in range(day_count):
            point_day = start_day + timedelta(days=day_offset)
            count = int(activity_counts.get(point_day, 0))
            max_count = max(max_count, count)
            chart_points.append(
                {
                    "date_label": point_day.strftime("%d %b"),
                    "short_label": point_day.strftime("%a"),
                    "count": count,
                    "height_percent": 0,
                }
            )
        chart_title = "Tickets Processed Per Day"
        chart_empty_message = (
            "No ticket activity has been recorded in the last 30 days."
            if period == "30d"
            else "No ticket activity has been recorded in the last 7 days."
        )

    for point in chart_points:
        count = int(point["count"])
        point["height_percent"] = (
            max(14, round((count / max_count) * 100))
            if max_count and count
            else 0
        )
    return chart_points, max_count, chart_title, chart_empty_message


def _build_weight_throughput_chart(
    rows: list[tuple[datetime, object]],
    *,
    period: str,
    today: date,
) -> dict[str, object]:
    point_weights: dict[int | date, Decimal] = {}

    if period == "today":
        for activity_datetime, net_kg in rows:
            bucket_hour = (activity_datetime.hour // 3) * 3
            point_weights[bucket_hour] = point_weights.get(bucket_hour, Decimal("0")) + _normalize_decimal(net_kg)

        chart_points: list[dict[str, object]] = []
        max_weight = Decimal("0")
        total_weight = Decimal("0")
        for start_hour in range(0, 24, 3):
            weight_kg = point_weights.get(start_hour, Decimal("0"))
            total_weight += weight_kg
            max_weight = max(max_weight, weight_kg)
            end_hour = min(start_hour + 3, 24)
            chart_points.append(
                {
                    "date_label": f"{start_hour:02d}:00-{end_hour:02d}:00",
                    "short_label": f"{start_hour:02d}",
                    "weight_kg": weight_kg,
                    "weight_kg_raw": _trim_decimal_text(weight_kg),
                    "height_percent": 0,
                    "value_label": "",
                }
            )
    else:
        day_count = 30 if period == "30d" else 7
        start_day = today - timedelta(days=day_count - 1)
        for activity_datetime, net_kg in rows:
            activity_day = activity_datetime.date()
            point_weights[activity_day] = point_weights.get(activity_day, Decimal("0")) + _normalize_decimal(net_kg)

        chart_points = []
        max_weight = Decimal("0")
        total_weight = Decimal("0")
        for day_offset in range(day_count):
            point_day = start_day + timedelta(days=day_offset)
            weight_kg = point_weights.get(point_day, Decimal("0"))
            total_weight += weight_kg
            max_weight = max(max_weight, weight_kg)
            chart_points.append(
                {
                    "date_label": point_day.strftime("%d %b"),
                    "short_label": point_day.strftime("%a"),
                    "weight_kg": weight_kg,
                    "weight_kg_raw": _trim_decimal_text(weight_kg),
                    "height_percent": 0,
                    "value_label": "",
                }
            )

    unit = _throughput_display_unit(max_weight, total_weight)
    for point in chart_points:
        weight_kg = point["weight_kg"]
        point["value_label"] = _format_throughput_weight(weight_kg, unit=unit)
        point["height_percent"] = (
            max(14, round((float(weight_kg) / float(max_weight)) * 100))
            if max_weight > 0 and weight_kg > 0
            else 0
        )

    return {
        "title": "Weight Throughput",
        "summary": f"Total processed this period: {_format_throughput_weight(total_weight, unit=unit)}",
        "unit": unit,
        "points": chart_points,
        "has_data": total_weight > 0,
        "empty_message": "No completed ticket weight has been recorded for this period.",
    }


def _build_tenant_dashboard(db: Session, *, period: str) -> dict[str, object]:
    today = utcnow().date()
    today_start, tomorrow_start = _datetime_bounds_for_day(today)
    overview_period = _normalize_dashboard_period(period)
    overview_start, overview_end = _dashboard_period_window(overview_period, today)
    overview_start_date = overview_start.date()
    overview_end_date = overview_end.date()

    open_status = _ticket_status_value(TicketStatusEnum.OPEN)
    complete_status = _ticket_status_value(TicketStatusEnum.COMPLETE)
    void_status = _ticket_status_value(TicketStatusEnum.VOID)

    open_tickets = int(
        db.execute(
            select(func.count(Ticket.id)).where(Ticket.status == open_status)
        ).scalar_one()
        or 0
    )
    completed_today = int(
        db.execute(
            select(func.count(Ticket.id)).where(
                Ticket.status == complete_status,
                Ticket.datetime >= today_start,
                Ticket.datetime < tomorrow_start,
            )
        ).scalar_one()
        or 0
    )
    total_weight_today = (
        db.execute(
            select(func.coalesce(func.sum(Ticket.net_kg), 0)).where(
                Ticket.status == complete_status,
                Ticket.datetime >= today_start,
                Ticket.datetime < tomorrow_start,
            )
        ).scalar_one()
        or 0
    )

    invoice_status_upper = func.upper(func.coalesce(Invoice.status, ""))
    invoices_pending = int(
        db.execute(
            select(func.count(Invoice.id))
            .where(invoice_status_upper != "")
            .where(
                or_(
                    invoice_status_upper.in_(INVOICE_OUTSTANDING_ISSUED_STATUSES),
                    ~invoice_status_upper.in_(INVOICE_OUTSTANDING_EXCLUDED_STATUSES),
                )
            )
        ).scalar_one()
        or 0
    )

    recent_tickets = _dashboard_ticket_rows(
        db,
        limit=6,
        date_from=overview_start,
        date_to=overview_end,
    )
    open_ticket_rows = _dashboard_ticket_rows(
        db,
        limit=6,
        status=open_status,
        oldest_first=True,
    )
    todays_traffic_rows = _dashboard_ticket_rows(
        db,
        limit=20,
        status=complete_status,
        date_from=today_start,
        date_to=tomorrow_start,
    )
    recent_invoices = _dashboard_invoice_rows(
        db,
        limit=5,
        date_from=overview_start_date,
        date_to=overview_end_date,
    )
    invoice_ready_rows = _dashboard_invoice_ready_rows(
        db,
        limit=5,
        date_from=overview_start,
        date_to=overview_end,
    )

    recent_activity_datetimes = db.execute(
        select(Ticket.datetime).where(
            Ticket.datetime >= overview_start,
            Ticket.datetime < overview_end,
            Ticket.status != void_status,
        )
    ).scalars().all()
    weight_throughput_rows = db.execute(
        select(Ticket.datetime, Ticket.net_kg).where(
            Ticket.datetime >= overview_start,
            Ticket.datetime < overview_end,
            Ticket.status == complete_status,
        )
    ).all()
    chart_points, max_count, chart_title, chart_empty_message = _build_dashboard_chart_points(
        recent_activity_datetimes,
        period=overview_period,
        today=today,
    )
    weight_throughput = _build_weight_throughput_chart(
        weight_throughput_rows,
        period=overview_period,
        today=today,
    )

    activity_count = int(
        db.execute(
            select(func.count(Ticket.id)).where(Ticket.status != void_status)
        ).scalar_one()
        or 0
    )
    invoice_count = int(
        db.execute(select(func.count(Invoice.id))).scalar_one() or 0
    )
    empty_state = activity_count == 0 and invoice_count == 0
    invoice_ready_count = int(
        db.execute(
            select(func.count(Ticket.id)).where(
                Ticket.status == complete_status,
                Ticket.invoice_id.is_(None),
                Ticket.dont_invoice.is_(False),
                Ticket.datetime >= overview_start,
                Ticket.datetime < overview_end,
            )
        ).scalar_one()
        or 0
    )

    period_labels = {
        "today": "Today",
        "7d": "Last 7 Days",
        "30d": "Last 30 Days",
    }

    return {
        "summary_cards": [
            {
                "key": "open_tickets",
                "label": "Open Tickets",
                "value": str(open_tickets),
                "hint": "Currently awaiting completion",
            },
            {
                "key": "completed_today",
                "label": "Completed Today",
                "value": str(completed_today),
                "hint": today.strftime("%d %b %Y"),
            },
            {
                "key": "total_weight_today",
                "label": "Total Weight Today",
                "value": _format_weight_kg(total_weight_today),
                "hint": "Completed tickets only",
            },
            {
                "key": "invoices_pending",
                "label": "Invoices Pending",
                "value": str(invoices_pending),
                "hint": "Outstanding issued invoices",
            },
        ],
        "overview_period": overview_period,
        "period_label": period_labels[overview_period],
        "period_options": (
            {"key": "today", "label": "Today", "active": overview_period == "today"},
            {"key": "7d", "label": "7 Days", "active": overview_period == "7d"},
            {"key": "30d", "label": "30 Days", "active": overview_period == "30d"},
        ),
        "recent_tickets": recent_tickets,
        "open_tickets": open_ticket_rows,
        "todays_traffic": todays_traffic_rows,
        "recent_invoices": recent_invoices,
        "invoice_ready_tickets": invoice_ready_rows,
        "invoice_ready_count": invoice_ready_count,
        "chart_points": chart_points,
        "chart_title": chart_title,
        "chart_empty_message": chart_empty_message,
        "chart_has_data": max_count > 0,
        "weight_throughput": weight_throughput,
        "empty_state": empty_state,
    }


def create_app(dev_mode: bool | None = None) -> FastAPI:
    effective_dev_mode = settings.dev_mode if dev_mode is None else dev_mode
    validate_production_secret(
        dev_mode=bool(effective_dev_mode),
        secret_key=settings.effective_secret_key,
    )

    app = FastAPI(title="weighbridge_web")

    app.include_router(api_router)
    app.include_router(lookups_router)

    if effective_dev_mode:
        from .routes.dev import router as dev_router

        app.include_router(dev_router)

    def _tenant_company_logo_file(request: Request, filename: str) -> Response:
        if bool(getattr(request.state, "platform_mode", False)):
            return PlainTextResponse("Not Found", status_code=404)
        tenant_id = getattr(request.state, "tenant_id", None)
        if tenant_id is None:
            return PlainTextResponse("Not Found", status_code=404)

        raw_name = str(filename or "").strip()
        if not raw_name:
            return PlainTextResponse("Not Found", status_code=404)
        if "/" in raw_name or "\\" in raw_name:
            return PlainTextResponse("Not Found", status_code=404)
        safe_name = Path(raw_name).name
        if not safe_name or safe_name in {".", ".."} or safe_name != raw_name:
            return PlainTextResponse("Not Found", status_code=404)

        tenant_logo_dir = company_logo_upload_dir(int(tenant_id), create=False).resolve()
        logo_path = (tenant_logo_dir / safe_name).resolve()
        try:
            logo_path.relative_to(tenant_logo_dir)
        except ValueError:
            return PlainTextResponse("Not Found", status_code=404)
        if not logo_path.is_file():
            return PlainTextResponse("Not Found", status_code=404)
        return FileResponse(str(logo_path))

    app.add_api_route(
        "/static/uploads/company/{filename:path}",
        _tenant_company_logo_file,
        methods=["GET"],
        include_in_schema=False,
    )

    def _ensure_upload_dirs() -> Path:
        uploads_root = Path(str(settings.effective_uploads_dir or "").strip()).resolve()
        uploads_root.mkdir(parents=True, exist_ok=True)
        return uploads_root

    _ensure_upload_dirs()
    media_dir = Path(settings.media_root).resolve()
    media_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=str(media_dir)), name="media")
    app.mount("/static", _StaticFilesWithoutSharedUploads(directory="app/static"), name="static")

    def _path_needs_system_guard(path: str) -> bool:
        target = str(path or "")
        return any(target.startswith(prefix) for prefix in _SYSTEM_GUARD_PREFIXES)

    def _path_requires_login(path: str) -> bool:
        target = str(path or "")
        if target in _LOGIN_EXEMPT_PATHS:
            return False
        return any(target.startswith(prefix) for prefix in _LOGIN_REQUIRED_PREFIXES)

    def _apply_cache_control_headers(path: str, response: Response) -> None:
        request_path = str(path or "")
        if request_path.startswith(_UPLOADS_STATIC_PREFIX):
            response.headers["Cache-Control"] = "public, max-age=86400"
            return

        content_type = str(response.headers.get("content-type", "")).lower()
        if "text/html" in content_type:
            response.headers["Cache-Control"] = "no-store"

    def _request_host_value(request: Request) -> str:
        host_value = str(request.headers.get("host", "") or request.url.hostname or "")
        if settings.effective_trust_forwarded_host:
            forwarded_host = str(request.headers.get("x-forwarded-host", "") or "").strip()
            if forwarded_host:
                host_value = forwarded_host.split(",", 1)[0].strip()
        return host_value

    def _maybe_brand_plain_error_response(request: Request, response: Response) -> Response:
        if request.method not in {"GET", "HEAD"}:
            return response
        if _public_host_mode(request):
            return response

        status_code = int(response.status_code or 0)
        template_name = {
            403: "errors/403.html",
            404: "errors/404.html",
            500: "errors/500.html",
        }.get(status_code)
        if template_name is None:
            return response

        content_type = str(response.headers.get("content-type", "")).lower()
        if "text/plain" not in content_type and "text/html" not in content_type:
            return response

        body = getattr(response, "body", b"")
        if not isinstance(body, (bytes, bytearray)):
            return response
        normalized = str(body.decode("utf-8", errors="ignore") or "").strip().lower()
        plain_error_payloads = {
            "forbidden",
            "not found",
            "internal server error",
            "<h1>internal server error</h1>",
        }
        if normalized not in plain_error_payloads:
            return response

        return templates.TemplateResponse(
            request,
            template_name,
            {"request": request},
            status_code=status_code,
        )

    @contextmanager
    def _request_db(request: Request) -> Iterator:
        dep = request.app.dependency_overrides.get(get_db, get_db)
        db_gen = dep()
        db = next(db_gen)
        try:
            yield db
        finally:
            try:
                next(db_gen)
            except StopIteration:
                pass

    def _load_session_user(request: Request, db) -> User | None:
        user_id = request.session.get(SESSION_USER_ID_KEY)
        if user_id is None:
            return None
        try:
            parsed_user_id = int(user_id)
        except (TypeError, ValueError):
            request.session.pop(SESSION_USER_ID_KEY, None)
            return None

        platform_mode = bool(getattr(request.state, "platform_mode", False))
        legacy_single_host = bool(getattr(request.state, "legacy_single_host", False))

        user = db.get(User, parsed_user_id)
        if user is None and (not platform_mode) and legacy_single_host:
            user = (
                db.execute(
                    select(User)
                    .execution_options(skip_tenant_scope=True)
                    .where(User.id == parsed_user_id)
                )
                .scalars()
                .first()
            )
        if user is None or not bool(user.is_active):
            request.session.pop(SESSION_USER_ID_KEY, None)
            return None

        role = ensure_user_role(db, user, allow_bootstrap=True)
        request_tenant_id = getattr(request.state, "tenant_id", None)
        session_tenant_id = request.session.get(SESSION_TENANT_ID_KEY)
        if session_tenant_id in ("", None):
            session_tenant_id = None
        elif isinstance(session_tenant_id, str) and session_tenant_id.isdigit():
            session_tenant_id = int(session_tenant_id)

        if platform_mode:
            if role != ROLE_SUPERADMIN or getattr(user, "tenant_id", None) is not None:
                request.session.clear()
                return None
            request.session[SESSION_PLATFORM_MODE_KEY] = True
            request.session[SESSION_TENANT_ID_KEY] = None
            request.session[SESSION_ROLE_KEY] = role
            return user

        if request_tenant_id is None:
            request.session.clear()
            return None
        if role == ROLE_SUPERADMIN:
            if not legacy_single_host:
                request.session.clear()
                return None
            request.session[SESSION_PLATFORM_MODE_KEY] = False
            request.session[SESSION_TENANT_ID_KEY] = int(request_tenant_id)
            request.session[SESSION_ROLE_KEY] = role
            return user

        if session_tenant_id is not None and int(session_tenant_id) != int(request_tenant_id):
            request.session.clear()
            return None

        if getattr(user, "tenant_id", None) is None:
            if not legacy_single_host:
                request.session.clear()
                return None
            user.tenant_id = int(request_tenant_id)
            db.commit()

        if int(getattr(user, "tenant_id", 0) or 0) != int(request_tenant_id):
            request.session.clear()
            return None

        request.session[SESSION_PLATFORM_MODE_KEY] = False
        request.session[SESSION_TENANT_ID_KEY] = int(request_tenant_id)
        request.session[SESSION_ROLE_KEY] = role
        return user

    def _uninitialized_response(request: Request, *, superadmin: bool) -> HTMLResponse:
        message = "System not initialized. Please contact your administrator."
        if superadmin:
            message = "System not initialized. Visit /setup (superadmin)."
        return templates.TemplateResponse(
            request,
            "system/uninitialized.html",
            {
                "request": request,
                "is_superadmin": superadmin,
                "message": message,
            },
            status_code=503,
        )

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        csrf_cookie = request.cookies.get(CSRF_COOKIE_NAME)
        should_set_csrf_cookie = False
        if not csrf_cookie:
            csrf_cookie = generate_csrf_token()
            should_set_csrf_cookie = True
        request.state.csrf_token = csrf_cookie
        request.state.current_user = None
        request.state.tenant = None
        request.state.tenant_id = None
        request.state.platform_mode = False
        request.state.request_subdomain = ""
        request.state.tenant_route_prefix = ""
        request.state.public_host_mode = False
        request.state.legacy_single_host = False

        original_request_path = _request_scope_path(request)
        tenant_path_match = split_tenant_route_path(original_request_path)
        tenant_path_subdomain = ""
        if tenant_path_match is not None:
            tenant_path_subdomain, stripped_path = tenant_path_match
            request.state.tenant_route_prefix = tenant_route_prefix(tenant_path_subdomain)
            request.scope["path"] = stripped_path
            request.scope["raw_path"] = stripped_path.encode("utf-8")

        request_path = _request_scope_path(request)
        tenant_context_tokens = set_request_tenant_context(
            tenant_id=None,
            platform_mode=False,
        )

        def _finalize_response(response: Response, *, force_set_csrf: bool = False) -> Response:
            response = _apply_tenant_route_redirect_prefix(request, response)
            _apply_cache_control_headers(request_path, response)
            if should_set_csrf_cookie or force_set_csrf:
                set_csrf_cookie(response, request, csrf_cookie)
            apply_security_headers(response)
            return response

        def _plain_error(message: str, status_code: int) -> Response:
            return _finalize_response(PlainTextResponse(message, status_code=status_code))

        def _switch_tenant_context(*, tenant_id: int | None, platform_mode: bool) -> None:
            nonlocal tenant_context_tokens
            reset_request_tenant_context(tenant_context_tokens)
            tenant_context_tokens = set_request_tenant_context(
                tenant_id=tenant_id,
                platform_mode=platform_mode,
            )

        try:
            # 1) Resolve host + tenant/platform mode.
            with _request_db(request) as db:
                host_value = _request_host_value(request)
                host_name = host_without_port(host_value)
                request.state.legacy_single_host = host_name in _LEGACY_SINGLE_HOSTS
                allowed_hosts = settings.effective_allowed_hosts
                if allowed_hosts:
                    host_allowed = host_name in allowed_hosts or any(
                        host_name.endswith(f".{allowed}") for allowed in allowed_hosts
                    )
                    if not host_allowed:
                        return _plain_error("Unknown tenant", 404)

                ensure_demo_tenant(db, create_missing=False)
                if db.new or db.dirty:
                    db.commit()

                if tenant_path_subdomain:
                    request.state.request_subdomain = tenant_path_subdomain
                    tenant = get_tenant_by_subdomain(db, tenant_path_subdomain)
                    if tenant is None:
                        return _plain_error("Unknown tenant", 404)
                    if not bool(tenant.is_active):
                        return _plain_error("Tenant disabled", 403)
                    request.state.tenant = tenant
                    request.state.tenant_id = int(tenant.id)
                    _switch_tenant_context(tenant_id=int(tenant.id), platform_mode=False)
                elif _is_exact_base_domain(host_name) or _is_marketing_host(host_name):
                    request.state.public_host_mode = True
                    request.session.pop(SESSION_PLATFORM_MODE_KEY, None)
                else:
                    subdomain = resolve_subdomain(host_value)
                    request.state.request_subdomain = subdomain

                    if subdomain == settings.effective_platform_subdomain:
                        request.state.platform_mode = True
                        _switch_tenant_context(tenant_id=None, platform_mode=True)
                    else:
                        tenant = get_tenant_by_subdomain(db, subdomain)
                        if tenant is None and subdomain == settings.effective_demo_tenant_subdomain:
                            tenant = ensure_demo_tenant(db, create_missing=True)
                            if tenant is not None:
                                db.commit()

                        if tenant is None:
                            return _plain_error("Unknown tenant", 404)
                        if not bool(tenant.is_active):
                            return _plain_error("Tenant disabled", 403)
                        request.state.tenant = tenant
                        request.state.tenant_id = int(tenant.id)
                        _switch_tenant_context(tenant_id=int(tenant.id), platform_mode=False)

            # 2) Enforce mode-specific route access.
            if request.state.platform_mode and any(
                request_path.startswith(prefix) for prefix in _TENANT_ONLY_PREFIXES
            ):
                return _plain_error("Unknown tenant", 404)

            if request.state.public_host_mode and not _path_allowed_for_public_host(request_path):
                return _plain_error("Not Found", 404)

            platform_only = any(request_path.startswith(prefix) for prefix in _PLATFORM_ONLY_PREFIXES)
            allow_legacy_platform_route = bool(
                request.state.legacy_single_host
                and (
                    request_path.startswith("/bootstrap")
                    or request_path.startswith("/platform/bootstrap")
                    or request_path.startswith("/admin/system-status")
                    or request_path.startswith("/admin/dev-mode")
                )
            )
            if (not request.state.platform_mode) and platform_only and (not allow_legacy_platform_route):
                return _plain_error("Not Found", 404)

            # 3) Load session user once.
            if not request.state.public_host_mode:
                with _request_db(request) as db:
                    request.state.current_user = _load_session_user(request, db)

            # 4) Enforce login once.
            tenant_root_requires_login = bool(
                request_path == "/"
                and not request.state.public_host_mode
                and not request.state.platform_mode
                and not request.state.legacy_single_host
            )
            if tenant_root_requires_login or _path_requires_login(request_path):
                authenticated = require_user(request)
                if not isinstance(authenticated, User):
                    return _finalize_response(authenticated)

            # 5) Enforce CSRF once for mutating requests.
            if is_state_changing_method(request.method):
                submitted_token = str(request.headers.get(CSRF_HEADER_NAME, "")).strip()
                if not submitted_token:
                    content_type = str(request.headers.get("content-type", "")).lower()
                    if content_type.startswith(
                        "application/x-www-form-urlencoded"
                    ) or content_type.startswith("multipart/form-data"):
                        body = await request.body()

                        def _receive_with_body(payload: bytes):
                            sent = False

                            async def _inner():
                                nonlocal sent
                                if sent:
                                    return {
                                        "type": "http.request",
                                        "body": b"",
                                        "more_body": False,
                                    }
                                sent = True
                                return {
                                    "type": "http.request",
                                "body": payload,
                                "more_body": False,
                            }

                            return _inner

                        form = None
                        try:
                            form_request = Request(request.scope, _receive_with_body(body))
                            form = await form_request.form()
                        except Exception:
                            form = None
                        submitted_token = (
                            str(form.get(CSRF_FORM_FIELD, "")).strip() if form else ""
                        )
                        request._receive = _receive_with_body(body)
                if not submitted_token or not csrf_cookie or not secrets.compare_digest(
                    submitted_token, csrf_cookie
                ):
                    return _finalize_response(csrf_forbidden_response(request), force_set_csrf=True)

            # 6) Enforce setup guard once.
            if _path_needs_system_guard(request_path):
                with _request_db(request) as db:
                    company = get_company_setting(db)
                    if not bool(company and getattr(company, "is_initialized", False)):
                        return _finalize_response(
                            _uninitialized_response(
                                request,
                                superadmin=is_superadmin_user(
                                    db, getattr(request.state, "current_user", None)
                                ),
                            )
                        )

            # 7) Execute request and finalize response.
            downstream = await call_next(request)
            downstream = _maybe_brand_plain_error_response(request, downstream)
            return _finalize_response(downstream)
        finally:
            reset_request_tenant_context(tenant_context_tokens)

    session_secret = str(settings.effective_secret_key or "").strip() or "dev-session-secret"
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        same_site="lax",
        https_only=not bool(effective_dev_mode),
    )

    @app.on_event("startup")
    def startup_printing_bootstrap() -> None:
        _ensure_upload_dirs()
        check_invoice_pdf_renderer()

    @app.get("/health", tags=["health"])
    def health_check() -> dict:
        return {"status": "ok"}

    @app.get("/branding.css", include_in_schema=False)
    def branding_css(db: Session = Depends(get_db)) -> PlainTextResponse:
        branding = get_branding(db)
        nav_color = normalize_hex_color(branding.get("nav_color", ""), default="#14213D")
        nav_foreground = nav_foreground_color(nav_color)
        primary_color = str(branding.get("primary_color", "") or "#FCA311")
        primary_contrast = str(branding.get("primary_contrast_hex", "") or "#111827")
        primary_soft = str(branding.get("primary_soft_rgba", "") or "rgba(252, 163, 17, 0.16)")
        logo_url = str(branding.get("logo_url", "") or "").replace("'", "\\'")
        try:
            nav_logo_height = int(branding.get("nav_logo_height_px", 34) or 34)
        except (TypeError, ValueError):
            nav_logo_height = 34
        nav_logo_height = max(20, min(80, nav_logo_height))

        css = (
            ":root {\n"
            f"  --theme-navbar-bg: {nav_color};\n"
            f"  --theme-primary: {primary_color};\n"
            f"  --theme-primary-contrast: {primary_contrast};\n"
            f"  --theme-primary-soft: {primary_soft};\n"
            f"  --theme-logo-url: url('{logo_url}');\n"
            f"  --theme-nav-logo-height: {nav_logo_height}px;\n"
            f"  --nav-bg: {nav_color};\n"
            f"  --nav-fg: {nav_foreground};\n"
            f"  --primary: {primary_color};\n"
            "}\n"
        )
        response = PlainTextResponse(css, media_type="text/css")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", response_class=HTMLResponse)
    def index(
        request: Request,
        period: str | None = None,
        db: Session = Depends(get_db),
    ) -> HTMLResponse:
        if _public_host_mode(request):
            host_name = host_without_port(str(request.url.hostname or ""))
            if _is_exact_base_domain(host_name) and settings.effective_base_domain:
                marketing_host = f"{settings.effective_marketing_subdomain}.{settings.effective_base_domain}"
                return RedirectResponse(url=f"https://{marketing_host}/", status_code=307)
            return templates.TemplateResponse(
                request,
                "marketing_home.html",
                {
                    "request": request,
                    "marketing_base_domain": settings.effective_base_domain or host_without_port(str(request.url.hostname or "")),
                },
            )
        if bool(getattr(request.state, "platform_mode", False)):
            return RedirectResponse(url="/platform/tenants", status_code=303)
        company = get_company_setting(db)
        initialized = bool(company and getattr(company, "is_initialized", False))
        missing_required = missing_required_lookup_messages(db) if initialized else []
        user_count = int(db.execute(select(func.count(User.id))).scalar_one_or_none() or 0)
        setup_ready = initialized and len(missing_required) == 0
        current_user = getattr(request.state, "current_user", None)
        show_dashboard = current_user is not None
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "request": request,
                "dashboard": _build_tenant_dashboard(
                    db,
                    period=_normalize_dashboard_period(period),
                )
                if show_dashboard
                else None,
                "show_dashboard": show_dashboard,
                "show_first_time_setup": not setup_ready,
                "setup_ready": setup_ready,
                "setup_initialized": initialized,
                "missing_required_lookups": missing_required,
                "needs_first_admin": user_count == 0,
            },
        )

    @app.get("/reports", response_class=HTMLResponse)
    def reports(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "reports.html", {"request": request})

    @app.get("/admin", response_class=HTMLResponse)
    def admin(request: Request) -> HTMLResponse:
        if bool(getattr(request.state, "platform_mode", False)):
            return RedirectResponse(url="/platform/tenants", status_code=303)
        return templates.TemplateResponse(request, "admin.html", {"request": request})

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.exception("Unhandled server error", exc_info=exc)
        accept = str(request.headers.get("accept", "")).lower()
        if "application/json" in accept:
            return JSONResponse(
                {"detail": "Internal Server Error"},
                status_code=500,
            )
        return HTMLResponse("<h1>Internal Server Error</h1>", status_code=500)

    if dev_mode is False:
        _strip_non_production_routes(app)

    return app

app = create_app()
