from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import Container, Destination, Driver, Haulier, Product, Ticket, Unit

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/debug/integrity", response_class=HTMLResponse)
def debug_integrity(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    if not settings.debug:
        raise HTTPException(status_code=404)

    negative_net = (
        db.execute(
            select(Ticket).where(
                Ticket.gross_kg.is_not(None),
                Ticket.tare_kg.is_not(None),
                Ticket.gross_kg < Ticket.tare_kg,
            )
        )
        .scalars()
        .all()
    )

    complete_missing_weights = (
        db.execute(
            select(Ticket).where(
                Ticket.status == "COMPLETE",
                or_(Ticket.gross_kg.is_(None), Ticket.tare_kg.is_(None)),
            )
        )
        .scalars()
        .all()
    )

    inactive_refs: list[dict[str, object]] = []

    for ticket, haulier in db.execute(
        select(Ticket, Haulier)
        .join(Haulier, Ticket.haulier_id == Haulier.id)
        .where(Haulier.is_active.is_(False))
    ).all():
        inactive_refs.append(
            {"ticket": ticket, "issue": f"Haulier inactive: {haulier.name}"}
        )

    for ticket, driver in db.execute(
        select(Ticket, Driver)
        .join(Driver, Ticket.driver_id == Driver.id)
        .where(Driver.is_active.is_(False))
    ).all():
        inactive_refs.append(
            {"ticket": ticket, "issue": f"Driver inactive: {driver.name}"}
        )

    for ticket, container in db.execute(
        select(Ticket, Container)
        .join(Container, Ticket.container_id == Container.id)
        .where(Container.is_active.is_(False))
    ).all():
        inactive_refs.append(
            {"ticket": ticket, "issue": f"Container inactive: {container.name}"}
        )

    for ticket, destination in db.execute(
        select(Ticket, Destination)
        .join(Destination, Ticket.destination_id == Destination.id)
        .where(Destination.is_active.is_(False))
    ).all():
        inactive_refs.append(
            {
                "ticket": ticket,
                "issue": f"Destination inactive: {destination.name}",
            }
        )

    for ticket, unit in db.execute(
        select(Ticket, Unit)
        .join(Unit, Ticket.unit_id == Unit.id)
        .where(Unit.is_active.is_(False))
    ).all():
        inactive_refs.append(
            {"ticket": ticket, "issue": f"Unit inactive: {unit.name}"}
        )

    for ticket, product, unit in db.execute(
        select(Ticket, Product, Unit)
        .join(Product, Ticket.product_id == Product.id)
        .join(Unit, Product.unit_id == Unit.id)
        .where(Unit.is_active.is_(False))
    ).all():
        inactive_refs.append(
            {
                "ticket": ticket,
                "issue": f"Product unit inactive: {product.code} ({unit.name})",
            }
        )

    return templates.TemplateResponse(request, 
        "debug/integrity.html",
        {
            "request": request,
            "negative_net": negative_net,
            "complete_missing_weights": complete_missing_weights,
            "inactive_refs": inactive_refs,
        },
    )
