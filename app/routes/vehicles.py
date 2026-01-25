from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (
    Container,
    Customer,
    Driver,
    Haulier,
    Vehicle,
    VehicleTare,
    VehicleType,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/vehicles", response_class=HTMLResponse)
def vehicles_list(
    request: Request,
    q: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    query = (
        select(Vehicle, Customer, VehicleType)
        .outerjoin(Customer, Vehicle.owner_customer_id == Customer.id)
        .outerjoin(VehicleType, Vehicle.vehicle_type_id == VehicleType.id)
        .order_by(Vehicle.registration)
    )
    if q:
        like = f"%{q}%"
        query = query.where(Vehicle.registration.ilike(like))
    rows = db.execute(query).all()
    return templates.TemplateResponse(
        "vehicles/list.html",
        {"request": request, "rows": rows, "q": q or ""},
    )


@router.get("/vehicles/new", response_class=HTMLResponse)
def vehicles_new(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return templates.TemplateResponse(
        "vehicles/new.html",
        {
            "request": request,
            "errors": [],
            "form": _empty_form(),
            "options": _load_options(db),
        },
    )


@router.post("/vehicles/new", response_class=HTMLResponse)
async def vehicles_create(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    form = await request.form()
    payload = _parse_vehicle_form(form)
    if payload["errors"]:
        return templates.TemplateResponse(
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
        vehicle_type_id=payload["vehicle_type_id"],
        default_tare_kg=payload["default_tare_kg"],
        overweight_threshold_kg=payload["overweight_threshold_kg"],
        haulier_id=payload["haulier_id"],
        driver_id=payload["driver_id"],
    )
    db.add(vehicle)
    db.commit()
    return RedirectResponse(url="/vehicles", status_code=303)


@router.get("/vehicles/{vehicle_id}", response_class=HTMLResponse)
def vehicles_edit(
    vehicle_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    vehicle = db.get(Vehicle, vehicle_id)
    if not vehicle:
        return templates.TemplateResponse(
            "vehicles/not_found.html",
            {"request": request, "vehicle_id": vehicle_id},
            status_code=404,
        )
    tares = db.execute(
        select(VehicleTare, Container)
        .join(Container, VehicleTare.container_id == Container.id)
        .where(VehicleTare.vehicle_id == vehicle.id)
        .order_by(Container.code)
    ).all()
    return templates.TemplateResponse(
        "vehicles/edit.html",
        {
            "request": request,
            "errors": [],
            "vehicle": vehicle,
            "form": _vehicle_to_form(vehicle),
            "options": _load_options(db),
            "tares": tares,
        },
    )


@router.post("/vehicles/{vehicle_id}", response_class=HTMLResponse)
async def vehicles_update(
    vehicle_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    vehicle = db.get(Vehicle, vehicle_id)
    if not vehicle:
        return templates.TemplateResponse(
            "vehicles/not_found.html",
            {"request": request, "vehicle_id": vehicle_id},
            status_code=404,
        )
    form = await request.form()
    payload = _parse_vehicle_form(form)
    if payload["errors"]:
        tares = db.execute(
            select(VehicleTare, Container)
            .join(Container, VehicleTare.container_id == Container.id)
            .where(VehicleTare.vehicle_id == vehicle.id)
            .order_by(Container.code)
        ).all()
        return templates.TemplateResponse(
            "vehicles/edit.html",
            {
                "request": request,
                "errors": payload["errors"],
                "vehicle": vehicle,
                "form": payload["form"],
                "options": _load_options(db),
                "tares": tares,
            },
            status_code=400,
        )

    vehicle.registration = payload["registration"]
    vehicle.owner_customer_id = payload["owner_customer_id"]
    vehicle.vehicle_type_id = payload["vehicle_type_id"]
    vehicle.default_tare_kg = payload["default_tare_kg"]
    vehicle.overweight_threshold_kg = payload["overweight_threshold_kg"]
    vehicle.haulier_id = payload["haulier_id"]
    vehicle.driver_id = payload["driver_id"]
    vehicle.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(url=f"/vehicles/{vehicle.id}", status_code=303)


@router.post("/vehicles/{vehicle_id}/tares", response_class=HTMLResponse)
async def vehicle_tares_add(
    vehicle_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    vehicle = db.get(Vehicle, vehicle_id)
    if not vehicle:
        return templates.TemplateResponse(
            "vehicles/not_found.html",
            {"request": request, "vehicle_id": vehicle_id},
            status_code=404,
        )

    form = await request.form()
    container_id = _parse_int(str(form.get("container_id", "")).strip())
    tare_kg = _parse_float(str(form.get("tare_kg", "")).strip())

    if container_id and tare_kg is not None:
        existing = db.execute(
            select(VehicleTare)
            .where(VehicleTare.vehicle_id == vehicle.id)
            .where(VehicleTare.container_id == container_id)
        ).scalar_one_or_none()
        if existing:
            existing.tare_kg = tare_kg
        else:
            db.add(
                VehicleTare(
                    vehicle_id=vehicle.id, container_id=container_id, tare_kg=tare_kg
                )
            )
        db.commit()

    return RedirectResponse(url=f"/vehicles/{vehicle.id}", status_code=303)


@router.post(
    "/vehicles/{vehicle_id}/tares/{tare_id}/update", response_class=HTMLResponse
)
async def vehicle_tares_update(
    vehicle_id: int, tare_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    tare = db.get(VehicleTare, tare_id)
    if not tare or tare.vehicle_id != vehicle_id:
        return RedirectResponse(url=f"/vehicles/{vehicle_id}", status_code=303)

    form = await request.form()
    tare_kg = _parse_float(str(form.get("tare_kg", "")).strip())
    if tare_kg is not None:
        tare.tare_kg = tare_kg
        db.commit()
    return RedirectResponse(url=f"/vehicles/{vehicle_id}", status_code=303)


@router.post(
    "/vehicles/{vehicle_id}/tares/{tare_id}/delete", response_class=HTMLResponse
)
def vehicle_tares_delete(
    vehicle_id: int, tare_id: int, db: Session = Depends(get_db)
) -> HTMLResponse:
    tare = db.get(VehicleTare, tare_id)
    if tare and tare.vehicle_id == vehicle_id:
        db.delete(tare)
        db.commit()
    return RedirectResponse(url=f"/vehicles/{vehicle_id}", status_code=303)


def _load_options(db: Session) -> dict[str, list[tuple[str, str]]]:
    customers = db.execute(select(Customer).order_by(Customer.name)).scalars()
    vehicle_types = db.execute(select(VehicleType).order_by(VehicleType.code)).scalars()
    hauliers = db.execute(select(Haulier).order_by(Haulier.code)).scalars()
    drivers = db.execute(select(Driver).order_by(Driver.name)).scalars()
    containers = db.execute(select(Container).order_by(Container.code)).scalars()
    return {
        "customers": [(str(row.id), row.name) for row in customers],
        "vehicle_types": [(str(row.id), row.code) for row in vehicle_types],
        "hauliers": [(str(row.id), row.code) for row in hauliers],
        "drivers": [(str(row.id), row.name) for row in drivers],
        "containers": [(str(row.id), row.code) for row in containers],
    }


def _parse_vehicle_form(form) -> dict:
    def value(key: str) -> str:
        return str(form.get(key, "")).strip()

    errors: list[str] = []
    registration = value("registration")
    vehicle_type_id = _parse_int(value("vehicle_type_id"))

    if not registration:
        errors.append("Registration is required.")
    # vehicle_type_id is optional

    return {
        "errors": errors,
        "form": {
            "registration": registration,
            "owner_customer_id": value("owner_customer_id"),
            "vehicle_type_id": value("vehicle_type_id"),
            "default_tare_kg": value("default_tare_kg"),
            "overweight_threshold_kg": value("overweight_threshold_kg"),
            "haulier_id": value("haulier_id"),
            "driver_id": value("driver_id"),
        },
        "registration": registration,
        "owner_customer_id": _parse_int(value("owner_customer_id")),
        "vehicle_type_id": vehicle_type_id,
        "default_tare_kg": _parse_float(value("default_tare_kg")),
        "overweight_threshold_kg": _parse_float(value("overweight_threshold_kg")),
        "haulier_id": _parse_int(value("haulier_id")),
        "driver_id": _parse_int(value("driver_id")),
    }


def _empty_form() -> dict:
    return {
        "registration": "",
        "owner_customer_id": "",
        "vehicle_type_id": "",
        "default_tare_kg": "",
        "overweight_threshold_kg": "",
        "haulier_id": "",
        "driver_id": "",
    }


def _vehicle_to_form(vehicle: Vehicle) -> dict:
    return {
        "registration": vehicle.registration or "",
        "owner_customer_id": str(vehicle.owner_customer_id or ""),
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
        "haulier_id": str(vehicle.haulier_id or ""),
        "driver_id": str(vehicle.driver_id or ""),
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
