import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..audit import diff as audit_diff
from ..audit import log as audit_log
from ..constants import REG_MAX
from ..db import get_db
from ..permissions import (
    PERM_MANAGE_VEHICLES,
    PERM_VIEW_VEHICLES,
    require_permission,
)
from ..pagination import build_pagination_context, count_rows
from ..models.base import utcnow
from ..security import validate_no_html_fields
from ..services.edit_conflicts import (
    ROW_VERSION_FIELD,
    STALE_EDIT_MESSAGE,
    row_version_conflict,
    row_version_token,
)
from ..models import (
    Container,
    Customer,
    Driver,
    Haulier,
    Vehicle,
    VehicleTare,
    VehicleType,
)
from ..templating import templates

router = APIRouter()
REGISTRATION_SANITIZE_RE = re.compile(r"[^A-Z0-9]+")
_VEHICLE_QUERY_ERROR_MESSAGES = {
    "tare_missing": "Tare must be provided.",
    "tare_invalid": "Tare must be a valid number.",
    "tare_negative": "Tare must be 0 or greater.",
    "container_missing": "Select a container before saving a per-container tare.",
    "container_inactive": "Selected container is inactive or unavailable.",
}


def _vehicle_snapshot(vehicle: Vehicle | None) -> dict[str, object]:
    if vehicle is None:
        return {}
    return {
        "registration": str(vehicle.registration or "").strip() or None,
        "owner_customer_id": vehicle.owner_customer_id,
        "default_customer_id": vehicle.default_customer_id,
        "vehicle_type_id": vehicle.vehicle_type_id,
        "default_tare_kg": vehicle.default_tare_kg,
        "overweight_threshold_kg": vehicle.overweight_threshold_kg,
        "default_haulier_id": vehicle.default_haulier_id,
        "default_driver_id": vehicle.default_driver_id,
    }


def _resolved_tenant_id(
    request: Request,
    db: Session,
    *,
    fallback_tenant_id: int | None = None,
) -> int | None:
    tenant_id = getattr(getattr(request, "state", None), "tenant_id", None)
    if tenant_id is None:
        tenant_id = db.info.get("tenant_id")
    if tenant_id is None:
        tenant_id = fallback_tenant_id
    return int(tenant_id) if tenant_id is not None else None


def _vehicle_registration_exists(
    db: Session,
    registration: str,
    *,
    tenant_id: int | None,
    exclude_vehicle_id: int | None = None,
) -> bool:
    query = (
        select(Vehicle.id)
        .execution_options(skip_tenant_scope=True)
        .where(Vehicle.registration == registration)
    )
    if tenant_id is not None:
        query = query.where(Vehicle.tenant_id == int(tenant_id))
    if exclude_vehicle_id is not None:
        query = query.where(Vehicle.id != int(exclude_vehicle_id))
    return db.execute(query.limit(1)).scalar_one_or_none() is not None


def _vehicle_query_errors(request: Request) -> list[str]:
    error_code = str(request.query_params.get("error", "")).strip().lower()
    message = _VEHICLE_QUERY_ERROR_MESSAGES.get(error_code)
    return [message] if message else []


@router.get("/vehicles", response_class=HTMLResponse)
def vehicles_list(
    request: Request,
    q: str | None = None,
    page: str | None = None,
    page_size: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    require_permission(request, PERM_VIEW_VEHICLES)
    query = (
        select(Vehicle, Customer, VehicleType, Haulier)
        .outerjoin(Customer, Vehicle.owner_customer_id == Customer.id)
        .outerjoin(VehicleType, Vehicle.vehicle_type_id == VehicleType.id)
        .outerjoin(Haulier, Vehicle.default_haulier_id == Haulier.id)
        .order_by(Vehicle.registration)
    )
    if q:
        like = f"%{q}%"
        query = query.where(Vehicle.registration.ilike(like))
    pagination = build_pagination_context(
        request,
        page=page,
        page_size=page_size,
        total_count=count_rows(db, query),
        singular_label="vehicle",
        plural_label="vehicles",
    )
    rows = db.execute(
        query.limit(int(pagination["page_size"])).offset(
            (int(pagination["page"]) - 1) * int(pagination["page_size"])
        )
    ).all()
    return templates.TemplateResponse(request, 
        "vehicles/list.html",
        {
            "request": request,
            "rows": rows,
            "q": q or "",
            "pagination": pagination,
            "saved": request.query_params.get("saved") == "1",
        },
    )


@router.get("/vehicles/new", response_class=HTMLResponse)
def vehicles_new(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    require_permission(request, PERM_MANAGE_VEHICLES)
    options = _load_options(db)
    errors: list[str] = []
    if not options.get("vehicle_types"):
        errors.append(
            "System not initialized: missing required lookups (vehicle types)."
        )
    return templates.TemplateResponse(request, 
        "vehicles/new.html",
        {
            "request": request,
            "errors": errors,
            "form": _empty_form(),
            "options": options,
        },
        status_code=503 if errors else 200,
    )


@router.post("/vehicles/new", response_class=HTMLResponse)
async def vehicles_create(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    require_permission(request, PERM_MANAGE_VEHICLES)
    form = await request.form()
    payload = _parse_vehicle_form(form)
    tenant_id = _resolved_tenant_id(request, db)
    payload["errors"].extend(_validate_vehicle_reference_selections(db, payload))
    if payload["errors"]:
        return templates.TemplateResponse(request, 
            "vehicles/new.html",
            {
                "request": request,
                "errors": payload["errors"],
                "form": payload["form"],
                "options": _load_options(db),
            },
            status_code=400,
        )
    if _vehicle_registration_exists(
        db,
        payload["registration"],
        tenant_id=tenant_id,
    ):
        payload["errors"].append("Registration already exists.")
        return templates.TemplateResponse(
            request,
            "vehicles/new.html",
            {
                "request": request,
                "errors": payload["errors"],
                "form": payload["form"],
                "options": _load_options(db),
            },
            status_code=400,
        )

    vehicle = Vehicle(
        registration=payload["registration"],
        owner_customer_id=payload["owner_customer_id"],
        default_customer_id=payload["default_customer_id"],
        vehicle_type_id=payload["vehicle_type_id"],
        default_tare_kg=payload["default_tare_kg"],
        overweight_threshold_kg=payload["overweight_threshold_kg"],
        default_haulier_id=payload["default_haulier_id"],
        default_driver_id=payload["default_driver_id"],
    )
    db.add(vehicle)
    try:
        db.flush()
        audit_log(
            db,
            request,
            action="CREATE",
            entity_type="vehicle",
            entity_id=vehicle.id,
            summary=f"Created vehicle {vehicle.registration}",
            details=_vehicle_snapshot(vehicle),
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        payload["errors"].append("Registration already exists.")
        return templates.TemplateResponse(request, 
            "vehicles/new.html",
            {
                "request": request,
                "errors": payload["errors"],
                "form": payload["form"],
                "options": _load_options(db),
            },
            status_code=400,
        )
    return RedirectResponse(url="/vehicles?saved=1", status_code=303)


@router.get("/vehicles/{vehicle_id}", response_class=HTMLResponse)
def vehicles_edit(
    vehicle_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    require_permission(request, PERM_VIEW_VEHICLES)
    vehicle = db.get(Vehicle, vehicle_id)
    if not vehicle:
        return templates.TemplateResponse(request, 
            "vehicles/not_found.html",
            {"request": request, "vehicle_id": vehicle_id},
            status_code=404,
        )
    tares = db.execute(
        select(VehicleTare, Container)
        .join(Container, VehicleTare.container_id == Container.id)
        .where(VehicleTare.vehicle_id == vehicle.id)
        .order_by(Container.name)
    ).all()
    return templates.TemplateResponse(request, 
        "vehicles/edit.html",
        {
            "request": request,
            "errors": _vehicle_query_errors(request),
            "vehicle": vehicle,
            "row_version": row_version_token(vehicle),
            "form": _vehicle_to_form(vehicle),
            "options": _load_options(db),
            "tares": tares,
        },
    )


@router.post("/vehicles/{vehicle_id}", response_class=HTMLResponse)
async def vehicles_update(
    vehicle_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    require_permission(request, PERM_MANAGE_VEHICLES)
    vehicle = db.get(Vehicle, vehicle_id)
    if not vehicle:
        return templates.TemplateResponse(request, 
            "vehicles/not_found.html",
            {"request": request, "vehicle_id": vehicle_id},
            status_code=404,
        )
    form = await request.form()
    payload = _parse_vehicle_form(form)
    tenant_id = _resolved_tenant_id(
        request,
        db,
        fallback_tenant_id=int(vehicle.tenant_id or 0) or None,
    )
    payload["errors"].extend(
        _validate_vehicle_reference_selections(db, payload, vehicle=vehicle)
    )
    if payload["errors"]:
        tares = db.execute(
            select(VehicleTare, Container)
            .join(Container, VehicleTare.container_id == Container.id)
            .where(VehicleTare.vehicle_id == vehicle.id)
            .order_by(Container.name)
        ).all()
        return templates.TemplateResponse(request, 
            "vehicles/edit.html",
            {
                "request": request,
                "errors": payload["errors"],
                "vehicle": vehicle,
                "row_version": row_version_token(vehicle),
                "form": payload["form"],
                "options": _load_options(db),
                "tares": tares,
            },
            status_code=400,
        )
    if _vehicle_registration_exists(
        db,
        payload["registration"],
        tenant_id=tenant_id,
        exclude_vehicle_id=vehicle.id,
    ):
        payload["errors"].append("Registration already exists.")
        tares = db.execute(
            select(VehicleTare, Container)
            .join(Container, VehicleTare.container_id == Container.id)
            .where(VehicleTare.vehicle_id == vehicle.id)
            .order_by(Container.name)
        ).all()
        return templates.TemplateResponse(
            request,
            "vehicles/edit.html",
            {
                "request": request,
                "errors": payload["errors"],
                "vehicle": vehicle,
                "row_version": row_version_token(vehicle),
                "form": payload["form"],
                "options": _load_options(db),
                "tares": tares,
            },
            status_code=400,
        )
    if row_version_conflict(vehicle, form.get(ROW_VERSION_FIELD)):
        payload["errors"].append(STALE_EDIT_MESSAGE)
        tares = db.execute(
            select(VehicleTare, Container)
            .join(Container, VehicleTare.container_id == Container.id)
            .where(VehicleTare.vehicle_id == vehicle.id)
            .order_by(Container.name)
        ).all()
        return templates.TemplateResponse(
            request,
            "vehicles/edit.html",
            {
                "request": request,
                "errors": payload["errors"],
                "vehicle": vehicle,
                "row_version": row_version_token(vehicle),
                "form": payload["form"],
                "options": _load_options(db),
                "tares": tares,
            },
            status_code=409,
        )

    before_audit = _vehicle_snapshot(vehicle)
    vehicle.registration = payload["registration"]
    vehicle.owner_customer_id = payload["owner_customer_id"]
    vehicle.default_customer_id = payload["default_customer_id"]
    vehicle.vehicle_type_id = payload["vehicle_type_id"]
    vehicle.default_tare_kg = payload["default_tare_kg"]
    vehicle.overweight_threshold_kg = payload["overweight_threshold_kg"]
    vehicle.default_haulier_id = payload["default_haulier_id"]
    vehicle.default_driver_id = payload["default_driver_id"]
    vehicle.updated_at = utcnow()
    after_audit = _vehicle_snapshot(vehicle)
    change_details = audit_diff(
        before_audit,
        after_audit,
        [
            "registration",
            "owner_customer_id",
            "default_customer_id",
            "vehicle_type_id",
            "default_tare_kg",
            "overweight_threshold_kg",
            "default_haulier_id",
            "default_driver_id",
        ],
    )
    if change_details["changed"]:
        audit_log(
            db,
            request,
            action="UPDATE",
            entity_type="vehicle",
            entity_id=vehicle.id,
            summary=f"Updated vehicle {vehicle.registration}",
            details=change_details,
        )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        payload["errors"].append("Registration already exists.")
        tares = db.execute(
            select(VehicleTare, Container)
            .join(Container, VehicleTare.container_id == Container.id)
            .where(VehicleTare.vehicle_id == vehicle.id)
            .order_by(Container.name)
        ).all()
        return templates.TemplateResponse(request, 
            "vehicles/edit.html",
            {
                "request": request,
                "errors": payload["errors"],
                "vehicle": vehicle,
                "row_version": row_version_token(vehicle),
                "form": payload["form"],
                "options": _load_options(db),
                "tares": tares,
            },
            status_code=400,
        )
    return RedirectResponse(url="/vehicles?saved=1", status_code=303)


@router.post("/vehicles/{vehicle_id}/tares", response_class=HTMLResponse)
async def vehicle_tares_add(
    vehicle_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    require_permission(request, PERM_MANAGE_VEHICLES)
    vehicle = db.get(Vehicle, vehicle_id)
    if not vehicle:
        return templates.TemplateResponse(request, 
            "vehicles/not_found.html",
            {"request": request, "vehicle_id": vehicle_id},
            status_code=404,
        )

    form = await request.form()
    container_id = _parse_int(str(form.get("container_id", "")).strip())
    tare_raw = str(form.get("tare_kg", "")).strip()
    tare_kg = _parse_float(tare_raw)

    if not container_id:
        return RedirectResponse(
            url=f"/vehicles/{vehicle.id}?error=container_missing",
            status_code=303,
        )
    if not tare_raw:
        return RedirectResponse(
            url=f"/vehicles/{vehicle.id}?error=tare_missing",
            status_code=303,
        )
    if tare_kg is None:
        return RedirectResponse(
            url=f"/vehicles/{vehicle.id}?error=tare_invalid",
            status_code=303,
        )
    if tare_kg < 0:
        return RedirectResponse(
            url=f"/vehicles/{vehicle.id}?error=tare_negative",
            status_code=303,
        )

    if container_id and tare_kg is not None:
        container = db.get(Container, container_id)
        if not container or not container.is_active:
            return RedirectResponse(
                url=f"/vehicles/{vehicle.id}?error=container_inactive",
                status_code=303,
            )
        existing = db.execute(
            select(VehicleTare)
            .where(VehicleTare.vehicle_id == vehicle.id)
            .where(VehicleTare.container_id == container_id)
        ).scalar_one_or_none()
        if existing:
            before_tare = existing.tare_kg
            existing.tare_kg = tare_kg
            if before_tare != tare_kg:
                audit_log(
                    db,
                    request,
                    action="UPDATE",
                    entity_type="vehicle_tare",
                    entity_id=existing.id,
                    summary=f"Updated tare for vehicle {vehicle.registration}",
                    details={
                        "vehicle_id": vehicle.id,
                        "vehicle_registration": vehicle.registration,
                        "container_id": container.id,
                        "container_name": container.name,
                        "changed": {
                            "tare_kg": {
                                "from": before_tare,
                                "to": tare_kg,
                            }
                        },
                    },
                )
        else:
            tare = VehicleTare(
                vehicle_id=vehicle.id,
                container_id=container_id,
                tare_kg=tare_kg,
            )
            db.add(tare)
            db.flush()
            audit_log(
                db,
                request,
                action="CREATE",
                entity_type="vehicle_tare",
                entity_id=tare.id,
                summary=f"Added tare for vehicle {vehicle.registration}",
                details={
                    "vehicle_id": vehicle.id,
                    "vehicle_registration": vehicle.registration,
                    "container_id": container.id,
                    "container_name": container.name,
                    "tare_kg": tare_kg,
                },
            )
        db.commit()

    return RedirectResponse(url=f"/vehicles/{vehicle.id}", status_code=303)


@router.post(
    "/vehicles/{vehicle_id}/tares/{tare_id}/update", response_class=HTMLResponse
)
async def vehicle_tares_update(
    vehicle_id: int, tare_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    require_permission(request, PERM_MANAGE_VEHICLES)
    tare = db.get(VehicleTare, tare_id)
    if not tare or tare.vehicle_id != vehicle_id:
        return RedirectResponse(url=f"/vehicles/{vehicle_id}", status_code=303)

    form = await request.form()
    tare_raw = str(form.get("tare_kg", "")).strip()
    tare_kg = _parse_float(tare_raw)
    if not tare_raw:
        return RedirectResponse(
            url=f"/vehicles/{vehicle_id}?error=tare_missing",
            status_code=303,
        )
    if tare_kg is None:
        return RedirectResponse(
            url=f"/vehicles/{vehicle_id}?error=tare_invalid",
            status_code=303,
        )
    if tare_kg < 0:
        return RedirectResponse(
            url=f"/vehicles/{vehicle_id}?error=tare_negative",
            status_code=303,
        )
    if tare_kg is not None:
        before_tare = tare.tare_kg
        vehicle = db.get(Vehicle, vehicle_id)
        container = db.get(Container, tare.container_id) if tare.container_id else None
        tare.tare_kg = tare_kg
        if before_tare != tare_kg:
            audit_log(
                db,
                request,
                action="UPDATE",
                entity_type="vehicle_tare",
                entity_id=tare.id,
                summary=(
                    f"Updated tare for vehicle {vehicle.registration}"
                    if vehicle is not None
                    else f"Updated tare for vehicle {vehicle_id}"
                ),
                details={
                    "vehicle_id": vehicle_id,
                    "vehicle_registration": str(vehicle.registration or "").strip() or None
                    if vehicle
                    else None,
                    "container_id": tare.container_id,
                    "container_name": str(container.name or "").strip() or None
                    if container
                    else None,
                    "changed": {
                        "tare_kg": {
                            "from": before_tare,
                            "to": tare_kg,
                        }
                    },
                },
            )
        db.commit()
    return RedirectResponse(url=f"/vehicles/{vehicle_id}", status_code=303)


@router.post(
    "/vehicles/{vehicle_id}/tares/{tare_id}/delete", response_class=HTMLResponse
)
def vehicle_tares_delete(
    vehicle_id: int,
    tare_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    require_permission(request, PERM_MANAGE_VEHICLES)
    tare = db.get(VehicleTare, tare_id)
    if tare and tare.vehicle_id == vehicle_id:
        vehicle = db.get(Vehicle, vehicle_id)
        container = db.get(Container, tare.container_id) if tare.container_id else None
        audit_log(
            db,
            request,
            action="DELETE",
            entity_type="vehicle_tare",
            entity_id=tare.id,
            summary=(
                f"Deleted tare for vehicle {vehicle.registration}"
                if vehicle is not None
                else f"Deleted tare for vehicle {vehicle_id}"
            ),
            details={
                "vehicle_id": vehicle_id,
                "vehicle_registration": str(vehicle.registration or "").strip() or None
                if vehicle
                else None,
                "container_id": tare.container_id,
                "container_name": str(container.name or "").strip() or None
                if container
                else None,
                "tare_kg": tare.tare_kg,
            },
        )
        db.delete(tare)
        db.commit()
    return RedirectResponse(url=f"/vehicles/{vehicle_id}", status_code=303)


def _load_options(db: Session) -> dict[str, list[tuple[str, str]]]:
    customers = db.execute(
        select(Customer)
        .where(Customer.on_stop.is_(False))
        .order_by(Customer.name)
    ).scalars()
    vehicle_types = db.execute(select(VehicleType).order_by(VehicleType.code)).scalars()
    hauliers = db.execute(
        select(Haulier).where(Haulier.is_active.is_(True)).order_by(Haulier.name)
    ).scalars()
    drivers = db.execute(select(Driver).order_by(Driver.name)).scalars()
    containers = db.execute(select(Container).order_by(Container.name)).scalars()
    return {
        "customers": [(str(row.id), row.name) for row in customers],
        "vehicle_types": [(str(row.id), row.code) for row in vehicle_types],
        "hauliers": [(str(row.id), row.name) for row in hauliers],
        "drivers": [(str(row.id), row.name) for row in drivers],
        "containers": [(str(row.id), row.name) for row in containers],
    }


def _parse_vehicle_form(form) -> dict:
    def value(key: str) -> str:
        return str(form.get(key, "")).strip()

    errors: list[str] = []
    registration = REGISTRATION_SANITIZE_RE.sub("", value("registration").upper())
    vehicle_type_id = _parse_int(value("vehicle_type_id"))
    default_tare_raw = value("default_tare_kg")
    overweight_threshold_raw = value("overweight_threshold_kg")
    default_tare_kg = _parse_float(default_tare_raw)
    overweight_threshold_kg = _parse_float(overweight_threshold_raw)

    validate_no_html_fields(
        {
            "Registration": registration,
        },
        errors,
    )

    if not registration:
        errors.append("Registration is required.")
    elif len(registration) > REG_MAX:
        errors.append(f"Registration must be {REG_MAX} characters or fewer.")
    if default_tare_raw and default_tare_kg is None:
        errors.append("Default tare must be a valid number.")
    elif default_tare_kg is not None and default_tare_kg < 0:
        errors.append("Default tare must be 0 or greater.")
    if overweight_threshold_raw and overweight_threshold_kg is None:
        errors.append("Overweight threshold must be a valid number.")
    elif overweight_threshold_kg is not None and overweight_threshold_kg < 0:
        errors.append("Overweight threshold must be 0 or greater.")
    # vehicle_type_id is optional

    return {
        "errors": errors,
        "form": {
            "registration": registration,
            "owner_customer_id": value("owner_customer_id"),
            "default_customer_id": value("default_customer_id"),
            "vehicle_type_id": value("vehicle_type_id"),
            "default_tare_kg": default_tare_raw,
            "overweight_threshold_kg": overweight_threshold_raw,
            "default_haulier_id": value("default_haulier_id"),
            "default_driver_id": value("default_driver_id"),
        },
        "registration": registration,
        "owner_customer_id": _parse_int(value("owner_customer_id")),
        "default_customer_id": _parse_int(value("default_customer_id")),
        "vehicle_type_id": vehicle_type_id,
        "default_tare_kg": default_tare_kg,
        "overweight_threshold_kg": overweight_threshold_kg,
        "default_haulier_id": _parse_int(value("default_haulier_id")),
        "default_driver_id": _parse_int(value("default_driver_id")),
    }


def _validate_vehicle_reference_selections(
    db: Session,
    payload: dict,
    *,
    vehicle: Vehicle | None = None,
) -> list[str]:
    errors: list[str] = []
    form_data = payload.get("form") if isinstance(payload.get("form"), dict) else {}

    for field, label in (
        ("owner_customer_id", "Owner customer"),
        ("default_customer_id", "Default customer"),
    ):
        record_id = payload.get(field)
        if not record_id:
            payload[field] = None
            form_data[field] = ""
            continue
        customer = db.get(Customer, int(record_id))
        if customer is None:
            errors.append(f"{label} not found.")
            payload[field] = None
            form_data[field] = ""

    for field, model, label in (
        ("default_haulier_id", Haulier, "Default haulier"),
        ("default_driver_id", Driver, "Default driver"),
    ):
        record_id = payload.get(field)
        if not record_id:
            payload[field] = None
            form_data[field] = ""
            continue
        record = db.get(model, int(record_id))
        current_value = getattr(vehicle, field, None) if vehicle is not None else None
        if record is None:
            errors.append(f"{label} not found.")
            payload[field] = None
            form_data[field] = ""
            continue
        if not record.is_active and int(record.id) != int(current_value or 0):
            errors.append(f"{label} is inactive.")

    vehicle_type_id = payload.get("vehicle_type_id")
    if vehicle_type_id:
        vehicle_type = db.get(VehicleType, int(vehicle_type_id))
        if vehicle_type is None:
            errors.append("Vehicle type not found.")
            payload["vehicle_type_id"] = None
            form_data["vehicle_type_id"] = ""

    return errors


def _empty_form() -> dict:
    return {
        "registration": "",
        "owner_customer_id": "",
        "default_customer_id": "",
        "vehicle_type_id": "",
        "default_tare_kg": "",
        "overweight_threshold_kg": "",
        "default_haulier_id": "",
        "default_driver_id": "",
    }


def _vehicle_to_form(vehicle: Vehicle) -> dict:
    return {
        "registration": vehicle.registration or "",
        "owner_customer_id": str(vehicle.owner_customer_id or ""),
        "default_customer_id": str(vehicle.default_customer_id or ""),
        "vehicle_type_id": str(vehicle.vehicle_type_id or ""),
        "default_tare_kg": (
            f"{float(vehicle.default_tare_kg):.0f}"
            if vehicle.default_tare_kg is not None
            else ""
        ),
        "overweight_threshold_kg": (
            f"{float(vehicle.overweight_threshold_kg):.0f}"
            if vehicle.overweight_threshold_kg is not None
            else ""
        ),
        "default_haulier_id": str(vehicle.default_haulier_id or ""),
        "default_driver_id": str(vehicle.default_driver_id or ""),
    }


def _parse_int(value: str) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _parse_float(value: str) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None
