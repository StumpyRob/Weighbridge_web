import re
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, true
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Container, Destination, Driver, Haulier, Ticket

router = APIRouter(prefix="/lookups")
templates = Jinja2Templates(directory="app/templates")


def _lookup_redirect_url(request: Request, base_path: str) -> str:
    params: dict[str, str] = {"saved": "1"}
    q = request.query_params.get("q")
    hide_inactive = request.query_params.get("hide_inactive")
    if hide_inactive is None:
        legacy_show = request.query_params.get("show_inactive")
        if legacy_show is not None:
            hide_inactive = "0" if _is_truthy(legacy_show) else "1"
    if q:
        params["q"] = q
    if hide_inactive is not None:
        params["hide_inactive"] = hide_inactive
    return f"{base_path}?{urlencode(params)}"


def _is_truthy(value: str | None) -> bool:
    return str(value).lower() in {"1", "true", "yes", "on"}


def _resolve_hide_inactive(request: Request, hide_inactive: int | None) -> int:
    if hide_inactive is not None:
        return 1 if hide_inactive else 0
    legacy_show = request.query_params.get("show_inactive")
    if legacy_show is None:
        return 1
    return 0 if _is_truthy(legacy_show) else 1


def _render_lookup_list(
    request: Request,
    db: Session,
    model,
    entity_plural: str,
    entity_singular: str,
    base_path: str,
    q: str | None,
    hide_inactive: int,
    error: str | None = None,
) -> HTMLResponse:
    query = select(model)
    if hide_inactive:
        query = query.where(model.is_active == true())
    if q:
        query = query.where(func.lower(model.name).contains(func.lower(q)))
    items = db.execute(query.order_by(model.name.asc())).scalars().all()
    return templates.TemplateResponse(request, 
        "lookups/list.html",
        {
            "request": request,
            "entity_plural": entity_plural,
            "entity_singular": entity_singular,
            "base_path": base_path,
            "items": items,
            "q": q or "",
            "hide_inactive": bool(hide_inactive),
            "saved": request.query_params.get("saved") == "1",
            "error": error,
        },
    )


@router.get("/hauliers", response_class=HTMLResponse)
def hauliers_list(
    request: Request,
    q: str | None = None,
    hide_inactive: int | None = Query(None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    resolved_hide = _resolve_hide_inactive(request, hide_inactive)
    return _render_lookup_list(
        request,
        db,
        Haulier,
        "Hauliers",
        "Haulier",
        "/lookups/hauliers",
        q,
        resolved_hide,
    )


@router.get("/hauliers/new", response_class=HTMLResponse)
def hauliers_new(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, 
        "lookups/form.html",
        {
            "request": request,
            "entity_plural": "Hauliers",
            "entity_singular": "Haulier",
            "base_path": "/lookups/hauliers",
            "mode": "new",
            "item": None,
            "prefill_name": "",
            "error": None,
        },
    )


@router.post("/hauliers/new", response_class=HTMLResponse)
async def hauliers_create(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    form = await request.form()
    raw_name = str(form.get("name", ""))
    name = re.sub(r"\s+", " ", raw_name.strip())
    error = None
    if not name:
        error = "Name is required."
    elif len(name) > 120:
        error = "Name must be 120 characters or fewer."
    else:
        existing = db.execute(
            select(Haulier).where(func.lower(Haulier.name) == func.lower(name))
        ).scalar_one_or_none()
        if existing:
            error = "Name already exists."

    if error:
        return templates.TemplateResponse(request, 
            "lookups/form.html",
            {
                "request": request,
                "entity_plural": "Hauliers",
                "entity_singular": "Haulier",
                "base_path": "/lookups/hauliers",
                "mode": "new",
                "item": None,
                "prefill_name": name,
                "error": error,
            },
            status_code=400,
        )

    haulier = Haulier(name=name, is_active=True)
    db.add(haulier)
    db.commit()
    return RedirectResponse(
        url=_lookup_redirect_url(request, "/lookups/hauliers"),
        status_code=303,
    )


@router.get("/hauliers/{haulier_id}/edit", response_class=HTMLResponse)
def hauliers_edit(
    haulier_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    haulier = db.get(Haulier, haulier_id)
    if not haulier:
        return templates.TemplateResponse(request, 
            "lookups/not_found.html",
            {
                "request": request,
                "entity": "Haulier",
                "entity_id": haulier_id,
                "base_path": "/lookups/hauliers",
            },
            status_code=404,
        )
    return templates.TemplateResponse(request, 
        "lookups/form.html",
        {
            "request": request,
            "entity_plural": "Hauliers",
            "entity_singular": "Haulier",
            "base_path": "/lookups/hauliers",
            "mode": "edit",
            "item": haulier,
            "prefill_name": None,
            "error": None,
        },
    )


@router.post("/hauliers/{haulier_id}/edit", response_class=HTMLResponse)
async def hauliers_update(
    haulier_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    haulier = db.get(Haulier, haulier_id)
    if not haulier:
        return templates.TemplateResponse(request, 
            "lookups/not_found.html",
            {
                "request": request,
                "entity": "Haulier",
                "entity_id": haulier_id,
                "base_path": "/lookups/hauliers",
            },
            status_code=404,
        )
    form = await request.form()
    raw_name = str(form.get("name", ""))
    name = re.sub(r"\s+", " ", raw_name.strip())
    error = None
    if not name:
        error = "Name is required."
    elif len(name) > 120:
        error = "Name must be 120 characters or fewer."
    else:
        existing = db.execute(
            select(Haulier).where(
                func.lower(Haulier.name) == func.lower(name),
                Haulier.id != haulier.id,
            )
        ).scalar_one_or_none()
        if existing:
            error = "Name already exists."

    if error:
        return templates.TemplateResponse(request, 
            "lookups/form.html",
            {
                "request": request,
                "entity_plural": "Hauliers",
                "entity_singular": "Haulier",
                "base_path": "/lookups/hauliers",
                "mode": "edit",
                "item": haulier,
                "prefill_name": name,
                "error": error,
            },
            status_code=400,
        )

    haulier.name = name
    db.commit()
    return RedirectResponse(
        url=_lookup_redirect_url(request, "/lookups/hauliers"),
        status_code=303,
    )


@router.post("/hauliers/{haulier_id}/deactivate", response_class=HTMLResponse)
def hauliers_deactivate(
    haulier_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    haulier = db.get(Haulier, haulier_id)
    if not haulier:
        return templates.TemplateResponse(request, 
            "lookups/not_found.html",
            {
                "request": request,
                "entity": "Haulier",
                "entity_id": haulier_id,
                "base_path": "/lookups/hauliers",
            },
            status_code=404,
        )
    in_use = db.execute(
        select(func.count(Ticket.id)).where(Ticket.haulier_id == haulier.id)
    ).scalar()
    if in_use and in_use > 0:
        resolved_hide = _resolve_hide_inactive(
            request, None
        )
        return _render_lookup_list(
            request,
            db,
            Haulier,
            "Hauliers",
            "Haulier",
            "/lookups/hauliers",
            request.query_params.get("q"),
            resolved_hide,
            "Cannot deactivate: in use by tickets.",
        )

    haulier.is_active = False
    db.commit()
    return RedirectResponse(
        url=_lookup_redirect_url(request, "/lookups/hauliers"),
        status_code=303,
    )


@router.post("/hauliers/{haulier_id}/reactivate", response_class=HTMLResponse)
def hauliers_reactivate(
    haulier_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    haulier = db.get(Haulier, haulier_id)
    if not haulier:
        return templates.TemplateResponse(request, 
            "lookups/not_found.html",
            {
                "request": request,
                "entity": "Haulier",
                "entity_id": haulier_id,
                "base_path": "/lookups/hauliers",
            },
            status_code=404,
        )
    haulier.is_active = True
    db.commit()
    return RedirectResponse(
        url=_lookup_redirect_url(request, "/lookups/hauliers"),
        status_code=303,
    )


@router.get("/drivers", response_class=HTMLResponse)
def drivers_list(
    request: Request,
    q: str | None = None,
    hide_inactive: int | None = Query(None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    resolved_hide = _resolve_hide_inactive(request, hide_inactive)
    return _render_lookup_list(
        request,
        db,
        Driver,
        "Drivers",
        "Driver",
        "/lookups/drivers",
        q,
        resolved_hide,
    )


@router.get("/drivers/new", response_class=HTMLResponse)
def drivers_new(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, 
        "lookups/form.html",
        {
            "request": request,
            "entity_plural": "Drivers",
            "entity_singular": "Driver",
            "base_path": "/lookups/drivers",
            "mode": "new",
            "item": None,
            "prefill_name": "",
            "error": None,
        },
    )


@router.post("/drivers/new", response_class=HTMLResponse)
async def drivers_create(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    form = await request.form()
    raw_name = str(form.get("name", ""))
    name = re.sub(r"\s+", " ", raw_name.strip())
    error = None
    if not name:
        error = "Name is required."
    elif len(name) > 120:
        error = "Name must be 120 characters or fewer."
    else:
        existing = db.execute(
            select(Driver).where(func.lower(Driver.name) == func.lower(name))
        ).scalar_one_or_none()
        if existing:
            error = "Name already exists."

    if error:
        return templates.TemplateResponse(request, 
            "lookups/form.html",
            {
                "request": request,
                "entity_plural": "Drivers",
                "entity_singular": "Driver",
                "base_path": "/lookups/drivers",
                "mode": "new",
                "item": None,
                "prefill_name": name,
                "error": error,
            },
            status_code=400,
        )

    driver = Driver(name=name, is_active=True)
    db.add(driver)
    db.commit()
    return RedirectResponse(
        url=_lookup_redirect_url(request, "/lookups/drivers"),
        status_code=303,
    )


@router.get("/drivers/{driver_id}/edit", response_class=HTMLResponse)
def drivers_edit(
    driver_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    driver = db.get(Driver, driver_id)
    if not driver:
        return templates.TemplateResponse(request, 
            "lookups/not_found.html",
            {
                "request": request,
                "entity": "Driver",
                "entity_id": driver_id,
                "base_path": "/lookups/drivers",
            },
            status_code=404,
        )
    return templates.TemplateResponse(request, 
        "lookups/form.html",
        {
            "request": request,
            "entity_plural": "Drivers",
            "entity_singular": "Driver",
            "base_path": "/lookups/drivers",
            "mode": "edit",
            "item": driver,
            "prefill_name": None,
            "error": None,
        },
    )


@router.post("/drivers/{driver_id}/edit", response_class=HTMLResponse)
async def drivers_update(
    driver_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    driver = db.get(Driver, driver_id)
    if not driver:
        return templates.TemplateResponse(request, 
            "lookups/not_found.html",
            {
                "request": request,
                "entity": "Driver",
                "entity_id": driver_id,
                "base_path": "/lookups/drivers",
            },
            status_code=404,
        )
    form = await request.form()
    raw_name = str(form.get("name", ""))
    name = re.sub(r"\s+", " ", raw_name.strip())
    error = None
    if not name:
        error = "Name is required."
    elif len(name) > 120:
        error = "Name must be 120 characters or fewer."
    else:
        existing = db.execute(
            select(Driver).where(
                func.lower(Driver.name) == func.lower(name),
                Driver.id != driver.id,
            )
        ).scalar_one_or_none()
        if existing:
            error = "Name already exists."

    if error:
        return templates.TemplateResponse(request, 
            "lookups/form.html",
            {
                "request": request,
                "entity_plural": "Drivers",
                "entity_singular": "Driver",
                "base_path": "/lookups/drivers",
                "mode": "edit",
                "item": driver,
                "prefill_name": name,
                "error": error,
            },
            status_code=400,
        )

    driver.name = name
    db.commit()
    return RedirectResponse(
        url=_lookup_redirect_url(request, "/lookups/drivers"),
        status_code=303,
    )


@router.post("/drivers/{driver_id}/deactivate", response_class=HTMLResponse)
def drivers_deactivate(
    driver_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    driver = db.get(Driver, driver_id)
    if not driver:
        return templates.TemplateResponse(request, 
            "lookups/not_found.html",
            {
                "request": request,
                "entity": "Driver",
                "entity_id": driver_id,
                "base_path": "/lookups/drivers",
            },
            status_code=404,
        )
    in_use = db.execute(
        select(func.count(Ticket.id)).where(Ticket.driver_id == driver.id)
    ).scalar()
    if in_use and in_use > 0:
        resolved_hide = _resolve_hide_inactive(request, None)
        return _render_lookup_list(
            request,
            db,
            Driver,
            "Drivers",
            "Driver",
            "/lookups/drivers",
            request.query_params.get("q"),
            resolved_hide,
            "Cannot deactivate: in use by tickets.",
        )

    driver.is_active = False
    db.commit()
    return RedirectResponse(
        url=_lookup_redirect_url(request, "/lookups/drivers"),
        status_code=303,
    )


@router.post("/drivers/{driver_id}/reactivate", response_class=HTMLResponse)
def drivers_reactivate(
    driver_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    driver = db.get(Driver, driver_id)
    if not driver:
        return templates.TemplateResponse(request, 
            "lookups/not_found.html",
            {
                "request": request,
                "entity": "Driver",
                "entity_id": driver_id,
                "base_path": "/lookups/drivers",
            },
            status_code=404,
        )
    driver.is_active = True
    db.commit()
    return RedirectResponse(
        url=_lookup_redirect_url(request, "/lookups/drivers"),
        status_code=303,
    )


@router.get("/containers", response_class=HTMLResponse)
def containers_list(
    request: Request,
    q: str | None = None,
    hide_inactive: int | None = Query(None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    resolved_hide = _resolve_hide_inactive(request, hide_inactive)
    return _render_lookup_list(
        request,
        db,
        Container,
        "Containers",
        "Container",
        "/lookups/containers",
        q,
        resolved_hide,
    )


@router.get("/containers/new", response_class=HTMLResponse)
def containers_new(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, 
        "lookups/form.html",
        {
            "request": request,
            "entity_plural": "Containers",
            "entity_singular": "Container",
            "base_path": "/lookups/containers",
            "mode": "new",
            "item": None,
            "prefill_name": "",
            "error": None,
        },
    )


@router.post("/containers/new", response_class=HTMLResponse)
async def containers_create(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    form = await request.form()
    raw_name = str(form.get("name", ""))
    name = re.sub(r"\s+", " ", raw_name.strip())
    error = None
    if not name:
        error = "Name is required."
    elif len(name) > 120:
        error = "Name must be 120 characters or fewer."
    else:
        existing = db.execute(
            select(Container).where(func.lower(Container.name) == func.lower(name))
        ).scalar_one_or_none()
        if existing:
            error = "Name already exists."

    if error:
        return templates.TemplateResponse(request, 
            "lookups/form.html",
            {
                "request": request,
                "entity_plural": "Containers",
                "entity_singular": "Container",
                "base_path": "/lookups/containers",
                "mode": "new",
                "item": None,
                "prefill_name": name,
                "error": error,
            },
            status_code=400,
        )

    container = Container(name=name, is_active=True)
    db.add(container)
    db.commit()
    return RedirectResponse(
        url=_lookup_redirect_url(request, "/lookups/containers"),
        status_code=303,
    )


@router.get("/containers/{container_id}/edit", response_class=HTMLResponse)
def containers_edit(
    container_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    container = db.get(Container, container_id)
    if not container:
        return templates.TemplateResponse(request, 
            "lookups/not_found.html",
            {
                "request": request,
                "entity": "Container",
                "entity_id": container_id,
                "base_path": "/lookups/containers",
            },
            status_code=404,
        )
    return templates.TemplateResponse(request, 
        "lookups/form.html",
        {
            "request": request,
            "entity_plural": "Containers",
            "entity_singular": "Container",
            "base_path": "/lookups/containers",
            "mode": "edit",
            "item": container,
            "prefill_name": None,
            "error": None,
        },
    )


@router.post("/containers/{container_id}/edit", response_class=HTMLResponse)
async def containers_update(
    container_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    container = db.get(Container, container_id)
    if not container:
        return templates.TemplateResponse(request, 
            "lookups/not_found.html",
            {
                "request": request,
                "entity": "Container",
                "entity_id": container_id,
                "base_path": "/lookups/containers",
            },
            status_code=404,
        )
    form = await request.form()
    raw_name = str(form.get("name", ""))
    name = re.sub(r"\s+", " ", raw_name.strip())
    error = None
    if not name:
        error = "Name is required."
    elif len(name) > 120:
        error = "Name must be 120 characters or fewer."
    else:
        existing = db.execute(
            select(Container).where(
                func.lower(Container.name) == func.lower(name),
                Container.id != container.id,
            )
        ).scalar_one_or_none()
        if existing:
            error = "Name already exists."

    if error:
        return templates.TemplateResponse(request, 
            "lookups/form.html",
            {
                "request": request,
                "entity_plural": "Containers",
                "entity_singular": "Container",
                "base_path": "/lookups/containers",
                "mode": "edit",
                "item": container,
                "prefill_name": name,
                "error": error,
            },
            status_code=400,
        )

    container.name = name
    db.commit()
    return RedirectResponse(
        url=_lookup_redirect_url(request, "/lookups/containers"),
        status_code=303,
    )


@router.post("/containers/{container_id}/deactivate", response_class=HTMLResponse)
def containers_deactivate(
    container_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    container = db.get(Container, container_id)
    if not container:
        return templates.TemplateResponse(request, 
            "lookups/not_found.html",
            {
                "request": request,
                "entity": "Container",
                "entity_id": container_id,
                "base_path": "/lookups/containers",
            },
            status_code=404,
        )
    in_use = db.execute(
        select(func.count(Ticket.id)).where(Ticket.container_id == container.id)
    ).scalar()
    if in_use and in_use > 0:
        resolved_hide = _resolve_hide_inactive(request, None)
        return _render_lookup_list(
            request,
            db,
            Container,
            "Containers",
            "Container",
            "/lookups/containers",
            request.query_params.get("q"),
            resolved_hide,
            "Cannot deactivate: in use by tickets.",
        )

    container.is_active = False
    db.commit()
    return RedirectResponse(
        url=_lookup_redirect_url(request, "/lookups/containers"),
        status_code=303,
    )


@router.post("/containers/{container_id}/reactivate", response_class=HTMLResponse)
def containers_reactivate(
    container_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    container = db.get(Container, container_id)
    if not container:
        return templates.TemplateResponse(request, 
            "lookups/not_found.html",
            {
                "request": request,
                "entity": "Container",
                "entity_id": container_id,
                "base_path": "/lookups/containers",
            },
            status_code=404,
        )
    container.is_active = True
    db.commit()
    return RedirectResponse(
        url=_lookup_redirect_url(request, "/lookups/containers"),
        status_code=303,
    )


@router.get("/destinations", response_class=HTMLResponse)
def destinations_list(
    request: Request,
    q: str | None = None,
    hide_inactive: int | None = Query(None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    resolved_hide = _resolve_hide_inactive(request, hide_inactive)
    return _render_lookup_list(
        request,
        db,
        Destination,
        "Destinations",
        "Destination",
        "/lookups/destinations",
        q,
        resolved_hide,
    )


@router.get("/destinations/new", response_class=HTMLResponse)
def destinations_new(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, 
        "lookups/form.html",
        {
            "request": request,
            "entity_plural": "Destinations",
            "entity_singular": "Destination",
            "base_path": "/lookups/destinations",
            "mode": "new",
            "item": None,
            "prefill_name": "",
            "error": None,
        },
    )


@router.post("/destinations/new", response_class=HTMLResponse)
async def destinations_create(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    form = await request.form()
    raw_name = str(form.get("name", ""))
    name = re.sub(r"\s+", " ", raw_name.strip())
    error = None
    if not name:
        error = "Name is required."
    elif len(name) > 120:
        error = "Name must be 120 characters or fewer."
    else:
        existing = db.execute(
            select(Destination).where(
                func.lower(Destination.name) == func.lower(name)
            )
        ).scalar_one_or_none()
        if existing:
            error = "Name already exists."

    if error:
        return templates.TemplateResponse(request, 
            "lookups/form.html",
            {
                "request": request,
                "entity_plural": "Destinations",
                "entity_singular": "Destination",
                "base_path": "/lookups/destinations",
                "mode": "new",
                "item": None,
                "prefill_name": name,
                "error": error,
            },
            status_code=400,
        )

    destination = Destination(name=name, is_active=True)
    db.add(destination)
    db.commit()
    return RedirectResponse(
        url=_lookup_redirect_url(request, "/lookups/destinations"),
        status_code=303,
    )


@router.get("/destinations/{destination_id}/edit", response_class=HTMLResponse)
def destinations_edit(
    destination_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    destination = db.get(Destination, destination_id)
    if not destination:
        return templates.TemplateResponse(request, 
            "lookups/not_found.html",
            {
                "request": request,
                "entity": "Destination",
                "entity_id": destination_id,
                "base_path": "/lookups/destinations",
            },
            status_code=404,
        )
    return templates.TemplateResponse(request, 
        "lookups/form.html",
        {
            "request": request,
            "entity_plural": "Destinations",
            "entity_singular": "Destination",
            "base_path": "/lookups/destinations",
            "mode": "edit",
            "item": destination,
            "prefill_name": None,
            "error": None,
        },
    )


@router.post("/destinations/{destination_id}/edit", response_class=HTMLResponse)
async def destinations_update(
    destination_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    destination = db.get(Destination, destination_id)
    if not destination:
        return templates.TemplateResponse(request, 
            "lookups/not_found.html",
            {
                "request": request,
                "entity": "Destination",
                "entity_id": destination_id,
                "base_path": "/lookups/destinations",
            },
            status_code=404,
        )
    form = await request.form()
    raw_name = str(form.get("name", ""))
    name = re.sub(r"\s+", " ", raw_name.strip())
    error = None
    if not name:
        error = "Name is required."
    elif len(name) > 120:
        error = "Name must be 120 characters or fewer."
    else:
        existing = db.execute(
            select(Destination).where(
                func.lower(Destination.name) == func.lower(name),
                Destination.id != destination.id,
            )
        ).scalar_one_or_none()
        if existing:
            error = "Name already exists."

    if error:
        return templates.TemplateResponse(request, 
            "lookups/form.html",
            {
                "request": request,
                "entity_plural": "Destinations",
                "entity_singular": "Destination",
                "base_path": "/lookups/destinations",
                "mode": "edit",
                "item": destination,
                "prefill_name": name,
                "error": error,
            },
            status_code=400,
        )

    destination.name = name
    db.commit()
    return RedirectResponse(
        url=_lookup_redirect_url(request, "/lookups/destinations"),
        status_code=303,
    )


@router.post("/destinations/{destination_id}/deactivate", response_class=HTMLResponse)
def destinations_deactivate(
    destination_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    destination = db.get(Destination, destination_id)
    if not destination:
        return templates.TemplateResponse(request, 
            "lookups/not_found.html",
            {
                "request": request,
                "entity": "Destination",
                "entity_id": destination_id,
                "base_path": "/lookups/destinations",
            },
            status_code=404,
        )
    in_use = db.execute(
        select(func.count(Ticket.id)).where(
            Ticket.destination_id == destination.id
        )
    ).scalar()
    if in_use and in_use > 0:
        resolved_hide = _resolve_hide_inactive(request, None)
        return _render_lookup_list(
            request,
            db,
            Destination,
            "Destinations",
            "Destination",
            "/lookups/destinations",
            request.query_params.get("q"),
            resolved_hide,
            "Cannot deactivate: in use by tickets.",
        )

    destination.is_active = False
    db.commit()
    return RedirectResponse(
        url=_lookup_redirect_url(request, "/lookups/destinations"),
        status_code=303,
    )


@router.post("/destinations/{destination_id}/reactivate", response_class=HTMLResponse)
def destinations_reactivate(
    destination_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    destination = db.get(Destination, destination_id)
    if not destination:
        return templates.TemplateResponse(request, 
            "lookups/not_found.html",
            {
                "request": request,
                "entity": "Destination",
                "entity_id": destination_id,
                "base_path": "/lookups/destinations",
            },
            status_code=404,
        )
    destination.is_active = True
    db.commit()
    return RedirectResponse(
        url=_lookup_redirect_url(request, "/lookups/destinations"),
        status_code=303,
    )
