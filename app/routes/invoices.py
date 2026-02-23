from datetime import date, datetime, time, timedelta
import logging
from decimal import Decimal, ROUND_HALF_UP
import re
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session, joinedload

from ..config import settings
from ..constants import NOTES_MAX
from ..db import get_db
from ..models.base import utcnow
from ..models import (
    Customer,
    Invoice,
    InvoiceLine,
    InvoiceVoid,
    PaymentMethod,
    PrintJob,
    PrintProfile,
    PrintTemplate,
    Product,
    TaxRate,
    Ticket,
    Unit,
    Vehicle,
    VoidReason,
)
from ..security import validate_no_html
from ..seed import seed_invoice_void_reasons, seed_payment_methods
from ..services.pdf import (
    check_invoice_pdf_renderer,
    ensure_seed_invoice_pdf_template,
    find_seeded_invoice_pdf_template,
    render_invoice_pdf,
    render_invoice_pdf_html,
    resolve_default_invoice_pdf_template,
)
from ..services.wip_snapshots import customer_wip_snapshot, product_wip_snapshot
from ..services.printing import (
    PRINT_CONTENT_TYPE_HTML,
    PRINT_JOB_STATUS_FAILED,
    ensure_default_invoice_pdf_profile,
    execute_rendered_print,
    resolve_profile_transport,
)
from ..templating import templates

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)


LOCKED_INVOICE_STATUSES = {"VOID"}
VOID_REASON_TYPE_INVOICE = "INVOICE"

INVOICE_EXCLUSION_MISSING_QTY_PRICE = "Missing quantity/price"
INVOICE_EXCLUSION_MISSING_WEIGHT_PRICE = "Missing weight/price"
INVOICE_EXCLUSION_MISSING_PRICE = "Missing price"
INVOICE_EXCLUSION_MISSING_NET_WEIGHT = "Missing net weight"
INVOICE_EXCLUSION_ZERO_TOTAL = "Zero total"
INVOICE_EXCLUSION_UNKNOWN_UNIT_TYPE = "Unknown unit type"
WASTE_TRANSACTION_TYPES = {"WASTEIN", "WASTEOUT"}
PRINT_PROFILE_PURPOSE_INVOICE_PDF = "INVOICE_PDF"


@router.get("/invoices", response_class=HTMLResponse)
def invoices_list(
    request: Request,
    q: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    query = (
        select(Invoice, Customer)
        .join(Customer, Invoice.customer_id == Customer.id)
        .order_by(Invoice.invoice_date.desc())
    )
    if q:
        like = f"%{q}%"
        query = query.where(
            or_(Invoice.invoice_no.ilike(like), Customer.name.ilike(like))
        )
    rows = db.execute(query).all()
    return templates.TemplateResponse(request, 
        "invoices/list.html", {"request": request, "rows": rows, "q": q or ""}
    )


@router.get("/invoices/generate", response_class=HTMLResponse)
def invoices_generate_form(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    customers = db.execute(select(Customer).order_by(Customer.name)).scalars().all()
    return templates.TemplateResponse(request, 
        "invoices/generate.html",
        {
            "request": request,
            "errors": [],
            "customers": customers,
            "form": {"customer_id": "", "date_from": "", "date_to": ""},
        },
    )


@router.post("/invoices/generate", response_class=HTMLResponse)
async def invoices_generate(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    form = await request.form()
    customer_id = _parse_int(str(form.get("customer_id", "")).strip())
    date_from_raw = str(form.get("date_from", "")).strip()
    date_to_raw = str(form.get("date_to", "")).strip()

    errors: list[str] = []
    if not customer_id:
        errors.append("Customer is required.")
    date_from, date_to, date_errors = _validate_invoice_date_range(
        date_from_raw, date_to_raw
    )
    errors.extend(date_errors)

    customers = db.execute(select(Customer).order_by(Customer.name)).scalars().all()
    if errors:
        return templates.TemplateResponse(request, 
            "invoices/generate.html",
            {
                "request": request,
                "errors": errors,
                "customers": customers,
                "form": {
                    "customer_id": str(customer_id or ""),
                    "date_from": date_from_raw,
                    "date_to": date_to_raw,
                },
            },
        )

    try:
        customer_do_not_invoice, customer_must_have_po = _customer_invoice_rules(
            db, customer_id
        )
        if customer_do_not_invoice:
            return templates.TemplateResponse(
                request,
                "invoices/generate.html",
                {
                    "request": request,
                    "errors": ["No tickets found."],
                    "customers": customers,
                    "form": {
                        "customer_id": str(customer_id or ""),
                        "date_from": date_from_raw,
                        "date_to": date_to_raw,
                    },
                },
            )
        tickets = _fetch_ticket_candidates(db, customer_id, date_from, date_to)
        invoiceable_tickets = _fetch_invoiceable_ticket_candidates(
            db, customer_id, date_from, date_to
        )
        stop_blockers = _invoice_stop_blockers(db, customer_id, invoiceable_tickets)
        if stop_blockers:
            return templates.TemplateResponse(
                request,
                "invoices/generate.html",
                {
                    "request": request,
                    "errors": [_invoice_on_stop_error(stop_blockers)],
                    "customers": customers,
                    "form": {
                        "customer_id": str(customer_id or ""),
                        "date_from": date_from_raw,
                        "date_to": date_to_raw,
                    },
                },
            )
        included, excluded = _classify_tickets(
            tickets, customer_must_have_po=customer_must_have_po
        )
        included_total = _sum_included_ticket_totals(included)
    except Exception:
        logger.exception("Invoice preview failed")
        return templates.TemplateResponse(request, 
            "invoices/generate.html",
            {
                "request": request,
                "errors": ["Something went wrong generating the preview."],
                "customers": customers,
                "form": {
                    "customer_id": str(customer_id or ""),
                    "date_from": date_from_raw,
                    "date_to": date_to_raw,
                },
            },
        )

    if not tickets:
        return templates.TemplateResponse(request, 
            "invoices/generate.html",
            {
                "request": request,
                "errors": ["No tickets found."],
                "customers": customers,
                "form": {
                    "customer_id": str(customer_id or ""),
                    "date_from": date_from_raw,
                    "date_to": date_to_raw,
                },
            },
        )

    return templates.TemplateResponse(request, 
        "invoices/generate.html",
        {
            "request": request,
            "errors": [],
            "customers": customers,
            "form": {
                "customer_id": str(customer_id or ""),
                "date_from": date_from_raw,
                "date_to": date_to_raw,
            },
            "preview": {
                "included": included,
                "excluded": excluded,
                "included_total": _money(included_total),
            },
        },
    )


@router.post("/invoices/generate/confirm", response_class=HTMLResponse)
async def invoices_generate_confirm(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    form = await request.form()
    customer_id = _parse_int(str(form.get("customer_id", "")).strip())
    date_from_raw = str(form.get("date_from", "")).strip()
    date_to_raw = str(form.get("date_to", "")).strip()
    date_from, date_to, date_errors = _validate_invoice_date_range(
        date_from_raw, date_to_raw
    )

    errors: list[str] = []
    if not customer_id:
        errors.append("Customer is required.")
    errors.extend(date_errors)

    customers = db.execute(select(Customer).order_by(Customer.name)).scalars().all()
    if errors:
        return templates.TemplateResponse(request, 
            "invoices/generate.html",
            {
                "request": request,
                "errors": errors,
                "customers": customers,
                "form": {
                    "customer_id": str(customer_id or ""),
                    "date_from": date_from_raw,
                    "date_to": date_to_raw,
                },
            },
        )

    try:
        candidate_tickets = _fetch_invoiceable_ticket_candidates(
            db, customer_id, date_from, date_to
        )
    except Exception:
        logger.exception("Invoice candidate query failed")
        return templates.TemplateResponse(
            request,
            "invoices/generate.html",
            {
                "request": request,
                "errors": ["Something went wrong generating the invoice."],
                "customers": customers,
                "form": {
                    "customer_id": str(customer_id or ""),
                    "date_from": date_from_raw,
                    "date_to": date_to_raw,
                },
            },
        )
    stop_blockers = _invoice_stop_blockers(db, customer_id, candidate_tickets)
    if stop_blockers:
        return templates.TemplateResponse(
            request,
            "invoices/generate.html",
            {
                "request": request,
                "errors": [_invoice_on_stop_error(stop_blockers)],
                "customers": customers,
                "form": {
                    "customer_id": str(customer_id or ""),
                    "date_from": date_from_raw,
                    "date_to": date_to_raw,
                },
            },
        )

    ticket_filters = _invoiceable_ticket_filters(db, customer_id, date_from, date_to)

    try:
        ticket_rows = db.execute(
            select(Ticket, Product, TaxRate)
            .join(Product, Ticket.product_id == Product.id)
            .outerjoin(TaxRate, Product.tax_rate_id == TaxRate.id)
            .where(and_(*ticket_filters))
            .order_by(Ticket.datetime.asc())
        ).all()
    except Exception:
        logger.exception("Invoice confirm query failed")
        return templates.TemplateResponse(request, 
            "invoices/generate.html",
            {
                "request": request,
                "errors": ["Something went wrong generating the invoice."],
                "customers": customers,
                "form": {
                    "customer_id": str(customer_id or ""),
                    "date_from": date_from_raw,
                    "date_to": date_to_raw,
                },
            },
        )

    if not ticket_rows:
        return templates.TemplateResponse(request, 
            "invoices/generate.html",
            {
                "request": request,
                "errors": ["No invoiceable tickets found."],
                "customers": customers,
                "form": {
                    "customer_id": str(customer_id or ""),
                    "date_from": date_from_raw,
                    "date_to": date_to_raw,
                },
            },
        )

    try:
        customer = db.get(Customer, customer_id)
        invoice_date = date.today()
        due_date = None
        if customer and customer.payment_terms_days is not None:
            due_date = invoice_date + timedelta(days=max(customer.payment_terms_days, 0))
        invoice = Invoice(
            invoice_no=_generate_invoice_no(db),
            customer_id=customer_id,
            invoice_date=invoice_date,
            due_date=due_date,
            status="DRAFT",
            net_total=Decimal("0.00"),
            vat_total=Decimal("0.00"),
            gross_total=Decimal("0.00"),
            customer_snapshot_json=customer_wip_snapshot(customer),
        )
        db.add(invoice)
        db.flush()

        line_totals: list[tuple[Decimal, Decimal]] = []

        invoiceable_rows: list[tuple[Ticket, Product, TaxRate | None, Decimal, Decimal]] = []
        for ticket, product, tax_rate in ticket_rows:
            billable_qty, net, exclusion_reason = _resolve_ticket_invoice_values(
                ticket, product
            )
            if exclusion_reason:
                continue
            invoiceable_rows.append((ticket, product, tax_rate, billable_qty, net))

        if not invoiceable_rows:
            return templates.TemplateResponse(
                request,
                "invoices/generate.html",
                {
                    "request": request,
                    "errors": ["No invoiceable tickets found."],
                    "customers": customers,
                    "form": {
                        "customer_id": str(customer_id or ""),
                        "date_from": date_from_raw,
                        "date_to": date_to_raw,
                    },
                },
            )

        for ticket, product, tax_rate, billable_qty, net in invoiceable_rows:
            raw_rate = _decimal(tax_rate.rate_percent) if tax_rate else Decimal("0")
            rate = raw_rate / Decimal("100") if raw_rate > 1 else raw_rate
            vat = _money(net * rate)
            gross = net + vat

            line = InvoiceLine(
                invoice_id=invoice.id,
                ticket_id=ticket.id,
                description=_build_invoice_line_description(ticket, product, db),
                quantity=float(billable_qty),
                unit_price=_money(ticket.unit_price),
                net=net,
                vat=vat,
                gross=gross,
                product_snapshot_json=product_wip_snapshot(product),
            )
            db.add(line)
            ticket.invoice_id = invoice.id
            line_totals.append((net, vat))

        net_total = _money(sum((net for net, _ in line_totals), Decimal("0.00")))
        vat_total = _money(sum((vat for _, vat in line_totals), Decimal("0.00")))
        invoice.net_total = net_total
        invoice.vat_total = vat_total
        invoice.gross_total = _money(net_total + vat_total)

        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Invoice creation failed")
        return templates.TemplateResponse(request, 
            "invoices/generate.html",
            {
                "request": request,
                "errors": ["Something went wrong generating the invoice."],
                "customers": customers,
                "form": {
                    "customer_id": str(customer_id or ""),
                    "date_from": date_from_raw,
                    "date_to": date_to_raw,
                },
            },
        )

    return RedirectResponse(url=f"/invoices/{invoice.id}?created=1", status_code=303)


@router.get("/invoices/{invoice_id}", response_class=HTMLResponse)
def invoices_detail(
    invoice_id: int,
    request: Request,
    created: int | None = Query(None),
    paid: int | None = Query(None),
    voided: int | None = Query(None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        return templates.TemplateResponse(request, 
            "invoices/not_found.html",
            {"request": request, "invoice_id": invoice_id},
            status_code=404,
        )
    return templates.TemplateResponse(request, 
        "invoices/detail.html",
        _invoice_detail_context(
            request,
            db,
            invoice,
            errors=[],
            created=created == 1,
            paid=paid == 1,
            voided=voided == 1,
        ),
    )


@router.get("/invoices/{invoice_id}/pdf")
def invoices_download_pdf(
    invoice_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found.")
    has_line_items = db.execute(
        select(InvoiceLine.id).where(InvoiceLine.invoice_id == invoice.id).limit(1)
    ).first()
    if not has_line_items:
        raise HTTPException(status_code=400, detail="Invoice has no line items.")

    renderer_status = check_invoice_pdf_renderer()
    if not renderer_status.available:
        detail = renderer_status.detail or "Unknown WeasyPrint dependency error."
        raise HTTPException(
            status_code=500,
            detail=(
                f"Renderer unavailable for invoice {invoice.id}. "
                f"Please contact support. ({detail})"
            ),
        )

    invoice_template = _resolve_invoice_pdf_template_for_request(
        db,
        strict_mode=_invoice_pdf_strict_mode_enabled(),
    )
    try:
        pdf_bytes = render_invoice_pdf(
            invoice_id,
            db,
            template=invoice_template,
            allow_builtin_template_fallback=False,
            base_url=str(request.base_url),
            allow_fallback=False,
            include_fallback_warning=False,
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                f"Renderer unavailable for invoice {invoice.id}. "
                f"Please contact support. ({str(exc)})"
            ),
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    safe_invoice_no = _safe_invoice_filename_token(invoice.invoice_no)
    filename = f"Invoice-{safe_invoice_no}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@router.get("/invoices/{invoice_id}/pdf/html", response_class=HTMLResponse)
def invoices_pdf_html_debug(
    invoice_id: int,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if not _invoice_pdf_debug_route_enabled():
        raise HTTPException(status_code=404, detail="Not found.")
    invoice_template = _resolve_invoice_pdf_template_for_request(
        db,
        strict_mode=_invoice_pdf_strict_mode_enabled(),
    )
    try:
        html = render_invoice_pdf_html(
            invoice_id,
            db,
            template=invoice_template,
            allow_builtin_template_fallback=False,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return HTMLResponse(content=html)


@router.get("/invoices/{invoice_id}/preview", response_class=HTMLResponse)
def invoices_preview(
    invoice_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        return templates.TemplateResponse(
            request,
            "invoices/not_found.html",
            {"request": request, "invoice_id": invoice_id},
            status_code=404,
        )

    try:
        rendered_html, _ = _render_invoice_print_html(
            db,
            invoice_id=invoice.id,
            profile=None,
        )
    except HTTPException as exc:
        return HTMLResponse(str(exc.detail), status_code=exc.status_code)
    except (RuntimeError, ValueError, LookupError) as exc:
        return HTMLResponse(
            f"Invoice preview failed: {exc}",
            status_code=400,
        )

    return templates.TemplateResponse(
        request,
        "invoices/preview.html",
        {
            "request": request,
            "invoice": invoice,
            "rendered_html": rendered_html,
            "back_url": f"/invoices/{invoice.id}",
        },
    )


@router.post("/invoices/{invoice_id}/print")
async def invoices_print_dispatch(
    invoice_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found.")

    form = await request.form()
    purpose = str(form.get("purpose", "")).strip().upper() or PRINT_PROFILE_PURPOSE_INVOICE_PDF
    if purpose != PRINT_PROFILE_PURPOSE_INVOICE_PDF:
        purpose = PRINT_PROFILE_PURPOSE_INVOICE_PDF
    profile_id = _parse_int(str(form.get("profile_id", "")).strip())

    try:
        profile = _resolve_invoice_print_profile(
            db,
            profile_id=profile_id,
            require_default=True,
        )
        rendered_html, rendered_template_id = _render_invoice_print_html(
            db,
            invoice_id=invoice_id,
            profile=profile,
        )

        if profile is not None:
            _, transport_config = resolve_profile_transport(profile)
            transport_mode = str(profile.transport_mode or "").strip().upper()
            profile_ref = profile.id
        else:
            transport_config = {}
            transport_mode = "LOCAL_BROWSER"
            profile_ref = None

        result = execute_rendered_print(
            db,
            purpose=purpose,
            rendered_content=rendered_html,
            content_type=PRINT_CONTENT_TYPE_HTML,
            transport_mode=transport_mode,
            transport_config=transport_config,
            profile_id=profile_ref,
            template_id=rendered_template_id,
            ticket_id=None,
            created_by_user_id=None,
        )
    except HTTPException:
        raise
    except (RuntimeError, ValueError, OSError, NotImplementedError) as exc:
        failed_job_id = _latest_invoice_print_job_id(db)
        query = {
            "print_failed": "1",
            "print_error": f"Invoice print failed for invoice {invoice.id}.",
            "print_error_detail": str(exc) or "Print delivery failed.",
            "invoice_id": str(invoice.id),
            "print_purpose": purpose,
        }
        if profile_id is not None:
            query["print_profile_id"] = str(profile_id)
        if failed_job_id is not None:
            query["print_job_id"] = str(failed_job_id)
        return RedirectResponse(
            url=f"/invoices/{invoice.id}?{urlencode(query)}",
            status_code=303,
        )

    browser_query = urlencode(
        {
            "purpose": purpose,
            "profile_id": str(profile.id) if profile is not None else "",
            "job_id": str(result.job.id),
            "invoice_print_sent": "1",
            "invoice_print_job_id": str(result.job.id),
        }
    )
    return RedirectResponse(
        url=f"/invoices/{invoice.id}/print/browser?{browser_query}",
        status_code=303,
    )


@router.get("/invoices/{invoice_id}/print/browser", response_class=HTMLResponse)
def invoices_print_browser(
    invoice_id: int,
    request: Request,
    profile_id: str | None = Query(None),
    purpose: str | None = Query(None),
    job_id: int | None = Query(None),
    invoice_print_job_id: int | None = Query(None),
    invoice_print_sent: int | None = Query(None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        return templates.TemplateResponse(
            request,
            "invoices/not_found.html",
            {"request": request, "invoice_id": invoice_id},
            status_code=404,
        )

    resolved_profile_id = _parse_int(str(profile_id or "").strip())

    rendered_html = ""
    resolved_job_id = invoice_print_job_id or job_id
    if resolved_job_id:
        job = db.get(PrintJob, resolved_job_id)
        if (
            job
            and str(job.purpose or "").strip().upper() == "INVOICE_PDF"
            and str(job.rendered_content or "").strip()
        ):
            rendered_html = str(job.rendered_content or "")
    if not rendered_html:
        try:
            selected_profile = _resolve_invoice_print_profile(
                db,
                profile_id=resolved_profile_id,
            )
            rendered_html, _ = _render_invoice_print_html(
                db,
                invoice_id=invoice_id,
                profile=selected_profile,
            )
        except HTTPException as exc:
            return HTMLResponse(str(exc.detail), status_code=exc.status_code)
        except (RuntimeError, ValueError, LookupError) as exc:
            return HTMLResponse(
                f"Invoice print preview failed: {exc}",
                status_code=400,
            )

    back_query: dict[str, str] = {}
    if invoice_print_sent == 1:
        back_query["invoice_print_sent"] = "1"
    if resolved_job_id is not None:
        back_query["invoice_print_job_id"] = str(resolved_job_id)
    normalized_purpose = str(purpose or "").strip().upper() or PRINT_PROFILE_PURPOSE_INVOICE_PDF
    back_query["print_purpose"] = normalized_purpose
    if resolved_profile_id is not None:
        back_query["print_profile_id"] = str(resolved_profile_id)
    back_query["invoice_id"] = str(invoice.id)
    back_url = (
        f"/invoices/{invoice.id}?{urlencode(back_query)}"
        if back_query
        else f"/invoices/{invoice.id}"
    )
    return templates.TemplateResponse(
        request,
        "invoices/print_browser.html",
        {
            "request": request,
            "invoice": invoice,
            "job_id": resolved_job_id,
            "rendered_html": rendered_html,
            "back_url": back_url,
        },
    )


@router.post("/invoices/{invoice_id}/paid", response_class=HTMLResponse)
async def invoices_mark_paid(
    invoice_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        return templates.TemplateResponse(request, 
            "invoices/not_found.html",
            {"request": request, "invoice_id": invoice_id},
            status_code=404,
        )
    if invoice.status in LOCKED_INVOICE_STATUSES:
        return templates.TemplateResponse(
            request,
            "invoices/detail.html",
            _invoice_detail_context(
                request,
                db,
                invoice,
                errors=[f"Invoice is {invoice.status} and cannot be modified."],
            ),
            status_code=400,
        )
    if invoice.status == "PAID":
        return templates.TemplateResponse(
            request,
            "invoices/detail.html",
            _invoice_detail_context(
                request,
                db,
                invoice,
                errors=["Already paid."],
            ),
            status_code=400,
        )

    form = await request.form()
    payment_method_id = _parse_int(str(form.get("payment_method_id", "")).strip())
    paid_at_raw = str(form.get("paid_at", "")).strip()
    errors: list[str] = []
    if not payment_method_id:
        errors.append("Payment method is required.")
    if not paid_at_raw:
        errors.append("Paid date is required.")
    if errors:
        return templates.TemplateResponse(
            request,
            "invoices/detail.html",
            _invoice_detail_context(request, db, invoice, errors=errors),
            status_code=400,
        )

    payment_method = db.get(PaymentMethod, payment_method_id)
    if not payment_method or not payment_method.is_active:
        return templates.TemplateResponse(
            request,
            "invoices/detail.html",
            _invoice_detail_context(
                request,
                db,
                invoice,
                errors=["Payment method is invalid."],
            ),
            status_code=400,
        )

    paid_at = _parse_datetime(paid_at_raw)

    if not paid_at:
        return templates.TemplateResponse(request, 
            "invoices/detail.html",
            _invoice_detail_context(
                request, db, invoice, errors=["Paid date must be valid."]
            ),
            status_code=400,
        )

    invoice.status = "PAID"
    invoice.payment_method_id = payment_method.id
    invoice.paid_at = paid_at
    db.commit()
    return RedirectResponse(url=f"/invoices/{invoice.id}?paid=1", status_code=303)


@router.post("/invoices/{invoice_id}/void", response_class=HTMLResponse)
async def invoices_void(
    invoice_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        return templates.TemplateResponse(request, 
            "invoices/not_found.html",
            {"request": request, "invoice_id": invoice_id},
            status_code=404,
        )
    if invoice.status in LOCKED_INVOICE_STATUSES:
        return templates.TemplateResponse(
            request,
            "invoices/detail.html",
            _invoice_detail_context(
                request,
                db,
                invoice,
                errors=[f"Invoice is {invoice.status} and cannot be modified."],
            ),
            status_code=400,
        )
    if invoice.status == "PAID":
        return templates.TemplateResponse(
            request,
            "invoices/detail.html",
            _invoice_detail_context(
                request,
                db,
                invoice,
                errors=["Cannot void a paid invoice."],
            ),
            status_code=400,
        )
    if invoice.status != "DRAFT":
        return templates.TemplateResponse(
            request,
            "invoices/detail.html",
            _invoice_detail_context(
                request,
                db,
                invoice,
                errors=["Only draft invoices can be voided."],
            ),
            status_code=400,
        )

    form = await request.form()
    reason_id = _parse_int(str(form.get("void_reason_id", "")).strip())
    note = str(form.get("void_note", "")).strip()
    reason = db.get(VoidReason, reason_id) if reason_id else None
    note_errors: list[str] = []
    validate_no_html(note, "Void note", note_errors)
    if note and len(note) > NOTES_MAX:
        note_errors.append(f"Void note must be {NOTES_MAX} characters or fewer.")

    if not reason_id:
        return templates.TemplateResponse(request, 
            "invoices/detail.html",
            _invoice_detail_context(
                request, db, invoice, errors=["Void reason is required."]
            ),
            status_code=400,
        )
    if (
        not reason
        or not reason.is_active
        or (reason.reason_type or "").strip().upper() != VOID_REASON_TYPE_INVOICE
    ):
        return templates.TemplateResponse(
            request,
            "invoices/detail.html",
            _invoice_detail_context(
                request, db, invoice, errors=["Void reason is invalid."]
            ),
            status_code=400,
        )
    if note_errors:
        return templates.TemplateResponse(
            request,
            "invoices/detail.html",
            _invoice_detail_context(request, db, invoice, errors=note_errors),
            status_code=400,
        )
    invoice.status = "VOID"
    db.add(
        InvoiceVoid(
            invoice_id=invoice.id,
            reason_id=reason_id,
            note=note or "No note provided.",
            voided_at=utcnow(),
            voided_by="admin",
        )
    )
    db.commit()
    return RedirectResponse(url=f"/invoices/{invoice.id}?voided=1", status_code=303)


def _generate_invoice_no(db: Session) -> str:
    year = utcnow().year
    db.execute(
        text(
            "INSERT OR IGNORE INTO invoice_sequences (year, last_number, updated_at) "
            "VALUES (:year, 0, :updated_at)"
        ),
        {"year": year, "updated_at": utcnow()},
    )
    db.execute(
        text(
            "UPDATE invoice_sequences "
            "SET last_number = last_number + 1, updated_at = :updated_at "
            "WHERE year = :year"
        ),
        {"year": year, "updated_at": utcnow()},
    )
    next_number = db.execute(
        text("SELECT last_number FROM invoice_sequences WHERE year = :year"),
        {"year": year},
    ).scalar_one()

    return f"INV-{str(year)[2:]}-{next_number:05d}"


def _parse_date(value: str) -> date | None:
    if not value:
        return None
    text = value.strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%Y-%m-%d",
        "%d/%m/%Y",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _validate_invoice_date_range(
    date_from_raw: str, date_to_raw: str
) -> tuple[date | None, date | None, list[str]]:
    errors: list[str] = []
    date_from_text = (date_from_raw or "").strip()
    date_to_text = (date_to_raw or "").strip()

    if not date_from_text:
        errors.append("Date from is required.")
    if not date_to_text:
        errors.append("Date to is required.")

    date_from = _parse_date(date_from_text)
    date_to = _parse_date(date_to_text)

    if date_from_text and not date_from:
        errors.append("Date from must be valid (dd/mm/yyyy).")
    if date_to_text and not date_to:
        errors.append("Date to must be valid (dd/mm/yyyy).")
    if date_from and date_to and date_from > date_to:
        errors.append("Date from must be on or before date to.")

    return date_from, date_to, errors


def _parse_int(value: str) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _safe_invoice_filename_token(value: str | None) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip())
    cleaned = cleaned.strip("-")
    return cleaned or "invoice"


def _latest_invoice_print_job_id(db: Session) -> int | None:
    row = db.execute(
        select(PrintJob.id)
        .where(PrintJob.purpose == "INVOICE_PDF", PrintJob.status == PRINT_JOB_STATUS_FAILED)
        .order_by(PrintJob.id.desc())
        .limit(1)
    ).first()
    return int(row[0]) if row else None


def _invoice_print_profile_display_name(profile: PrintProfile) -> str:
    description = str(profile.description or "").strip()
    return description or str(profile.code or "").strip()


def _load_active_invoice_print_profiles(db: Session) -> list[PrintProfile]:
    return list(
        db.execute(
            select(PrintProfile)
            .where(
                PrintProfile.is_active.is_(True),
                PrintProfile.purpose == PRINT_PROFILE_PURPOSE_INVOICE_PDF,
            )
            .order_by(PrintProfile.is_default.desc(), PrintProfile.code.asc())
        ).scalars()
    )


def _default_invoice_print_profile(
    profiles: list[PrintProfile],
) -> PrintProfile | None:
    return next((row for row in profiles if row.is_default), None)


def _resolve_invoice_print_profile(
    db: Session,
    *,
    profile_id: int | None = None,
    require_default: bool = False,
) -> PrintProfile | None:
    if require_default:
        ensure_default_invoice_pdf_profile(db)

    profiles = _load_active_invoice_print_profiles(db)
    if profile_id is not None:
        selected = next((row for row in profiles if row.id == profile_id), None)
        if selected is None:
            raise ValueError("Printer not found or inactive.")
        return selected
    default_profile = _default_invoice_print_profile(profiles)
    if require_default and default_profile is None:
        raise ValueError("No printer configured. Contact admin.")
    return default_profile


def _render_invoice_print_html(
    db: Session,
    *,
    invoice_id: int,
    profile: PrintProfile | None = None,
) -> tuple[str, int | None]:
    invoice_template = _resolve_invoice_pdf_template_for_request(
        db,
        strict_mode=_invoice_pdf_strict_mode_enabled(),
    )
    if profile is not None and profile.template_id:
        profile_template = db.get(PrintTemplate, profile.template_id)
        if (
            profile_template is not None
            and bool(profile_template.is_active)
            and str(profile_template.purpose or "").strip().upper()
            == PRINT_PROFILE_PURPOSE_INVOICE_PDF
        ):
            invoice_template = profile_template
    rendered_html = render_invoice_pdf_html(
        invoice_id=invoice_id,
        db=db,
        template=invoice_template,
        allow_builtin_template_fallback=False,
    )
    template_id = int(invoice_template.id) if invoice_template is not None else None
    return rendered_html, template_id


def _invoice_print_actions_context(
    request: Request,
    db: Session,
    invoice_id: int,
    *,
    is_admin: bool = False,
) -> dict[str, object]:
    ensure_default_invoice_pdf_profile(db)
    profiles = _load_active_invoice_print_profiles(db)
    default_profile = _default_invoice_print_profile(profiles)
    selected_profile_id = _parse_int(
        str(request.query_params.get("print_profile_id", "")).strip()
    )
    selected_profile = next(
        (row for row in profiles if selected_profile_id and row.id == selected_profile_id),
        None,
    )
    selected_profile_id_value = (
        str(selected_profile.id)
        if selected_profile is not None
        else (str(default_profile.id) if default_profile is not None else "")
    )
    selected_purpose = PRINT_PROFILE_PURPOSE_INVOICE_PDF
    profiles_payload = [
        {
            "id": str(profile.id),
            "label": _invoice_print_profile_display_name(profile),
            "option_label": _invoice_print_profile_display_name(profile),
            "purpose": PRINT_PROFILE_PURPOSE_INVOICE_PDF,
            "is_default": bool(profile.is_default),
            "transport_mode": str(profile.transport_mode or "").strip().upper(),
        }
        for profile in profiles
    ]
    default_profile_map = {
        PRINT_PROFILE_PURPOSE_INVOICE_PDF: (
            str(default_profile.id) if default_profile is not None else ""
        )
    }
    default_profile_label_map = {
        PRINT_PROFILE_PURPOSE_INVOICE_PDF: (
            _invoice_print_profile_display_name(default_profile)
            if default_profile is not None
            else ""
        )
    }
    send_enabled = default_profile is not None

    return {
        "print_missing_default": not send_enabled,
        "print_actions": {
            "entity_type": "invoice",
            "entity_id": int(invoice_id),
            "send_url": f"/invoices/{invoice_id}/print",
            "preview_url": f"/invoices/{invoice_id}/preview",
            "purposes": [(PRINT_PROFILE_PURPOSE_INVOICE_PDF, "Invoice (PDF)")],
            "profiles": profiles_payload,
            "selected_purpose": selected_purpose,
            "selected_profile_id": selected_profile_id_value,
            "default_profile_id": default_profile_map[PRINT_PROFILE_PURPOSE_INVOICE_PDF],
            "default_profile_ids_by_purpose": default_profile_map,
            "default_profile_labels_by_purpose": default_profile_label_map,
            "selected_default_profile_label": default_profile_label_map[
                PRINT_PROFILE_PURPOSE_INVOICE_PDF
            ],
            "send_enabled": send_enabled,
            "send_enabled_by_purpose": {PRINT_PROFILE_PURPOSE_INVOICE_PDF: send_enabled},
            "send_label": "Send to Printer",
            "preview_label": "Preview",
            "preview_button_id": "",
            "download_url": f"/invoices/{invoice_id}/pdf",
            "download_label": "Download PDF",
            "options_open": request.query_params.get("print_options") == "1",
            "no_default_message": "No printer configured. Contact admin.",
            "require_default_profile": True,
            "show_purpose_toggle": False,
            "show_advanced_menu": bool(is_admin),
            "show_resolved_default_label": True,
        }
    }


def _resolve_invoice_pdf_template_for_request(
    db: Session,
    *,
    strict_mode: bool,
):
    default_template = resolve_default_invoice_pdf_template(db)
    if default_template is not None:
        return default_template

    if strict_mode:
        raise HTTPException(
            status_code=500,
            detail="No default invoice PDF template configured",
        )

    seeded_template = find_seeded_invoice_pdf_template(db, require_active=True)
    if seeded_template is not None:
        return seeded_template

    seeded_template, changed = ensure_seed_invoice_pdf_template(db)
    if changed:
        db.commit()
    return seeded_template


def _invoice_pdf_debug_route_enabled() -> bool:
    if settings.dev_mode or settings.debug:
        return True
    return bool(templates.env.globals.get("DEV_MODE"))


def _invoice_pdf_strict_mode_enabled() -> bool:
    return bool(settings.debug)


def _decimal(value) -> Decimal:
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def _money(value) -> Decimal:
    return _decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _active_payment_methods(db: Session) -> list[PaymentMethod]:
    methods = (
        db.execute(
            select(PaymentMethod)
            .where(PaymentMethod.is_active.is_(True))
            .order_by(PaymentMethod.code)
        )
        .scalars()
        .all()
    )
    if methods:
        return methods
    # Keep mark-paid operational in clean databases.
    seed_payment_methods(db)
    return (
        db.execute(
            select(PaymentMethod)
            .where(PaymentMethod.is_active.is_(True))
            .order_by(PaymentMethod.code)
        )
        .scalars()
        .all()
    )

def _active_void_reasons(db: Session) -> list[VoidReason]:
    seed_invoice_void_reasons(db)
    return (
        db.execute(
            select(VoidReason)
            .where(
                VoidReason.is_active.is_(True),
                func.upper(VoidReason.reason_type) == VOID_REASON_TYPE_INVOICE,
            )
            .order_by(VoidReason.code)
        )
        .scalars()
        .all()
    )


def _invoice_detail_context(
    request: Request,
    db: Session,
    invoice: Invoice,
    *,
    errors: list[str] | None = None,
    created: bool = False,
    paid: bool = False,
    voided: bool = False,
) -> dict:
    customer = db.get(Customer, invoice.customer_id)
    customer_billing_lines = _customer_billing_lines(customer)
    payment_method = (
        db.get(PaymentMethod, invoice.payment_method_id)
        if invoice.payment_method_id
        else None
    )
    invoice_void_row = db.execute(
        select(InvoiceVoid, VoidReason)
        .outerjoin(VoidReason, InvoiceVoid.reason_id == VoidReason.id)
        .where(InvoiceVoid.invoice_id == invoice.id)
        .order_by(InvoiceVoid.voided_at.desc(), InvoiceVoid.id.desc())
        .limit(1)
    ).first()
    latest_invoice_void = invoice_void_row[0] if invoice_void_row else None
    latest_invoice_void_reason = invoice_void_row[1] if invoice_void_row else None
    lines = db.execute(
        select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id).order_by(InvoiceLine.id)
    ).scalars().all()
    ticket_rows = db.execute(
        select(Ticket, Vehicle.registration)
        .outerjoin(Vehicle, Ticket.vehicle_id == Vehicle.id)
        .options(
            joinedload(Ticket.product).joinedload(Product.unit),
            joinedload(Ticket.product).joinedload(Product.ewc_code),
        )
        .where(Ticket.invoice_id == invoice.id)
        .order_by(Ticket.datetime)
    ).all()
    tickets = [row[0] for row in ticket_rows]
    has_waste_tickets = any(
        _is_waste_ticket_transaction(ticket.transaction_type) for ticket in tickets
    )
    linked_tickets = [
        {
            "ticket": ticket,
            "po_number": str(ticket.po_number or "").strip() or None,
            "is_waste_ticket": _is_waste_ticket_transaction(ticket.transaction_type),
            "vehicle_reg": _ticket_vehicle_registration(
                ticket, db, vehicle_registration=vehicle_registration
            ),
            "product_display": _ticket_product_display(ticket.product),
            "billable_display": _ticket_billable_display(ticket),
            "ewc_code_display": _ticket_ewc_code_display(ticket),
            "is_hazardous": _ticket_is_hazardous(ticket),
            "waste_producer_display": _ticket_waste_producer_display(ticket),
        }
        for ticket, vehicle_registration in ticket_rows
    ]
    is_admin = False
    print_actions_context = _invoice_print_actions_context(
        request,
        db,
        invoice.id,
        is_admin=is_admin,
    )
    return {
        "request": request,
        "invoice": invoice,
        "customer": customer,
        "customer_billing_lines": customer_billing_lines,
        "payment_method": payment_method,
        "invoice_void": latest_invoice_void,
        "invoice_void_reason": latest_invoice_void_reason,
        "lines": lines,
        "tickets": tickets,
        "linked_tickets": linked_tickets,
        "has_waste_tickets": has_waste_tickets,
        "payment_methods": _active_payment_methods(db),
        "void_reasons": _active_void_reasons(db),
        "errors": errors or [],
        "created": created,
        "paid": paid,
        "voided": voided,
        "is_admin": is_admin,
        **print_actions_context,
    }


def _build_invoice_line_description(ticket: Ticket, product: Product, db: Session) -> str:
    ticket_date = ticket.datetime.strftime("%d/%m/%Y") if ticket.datetime else "-"
    vehicle_reg = _ticket_vehicle_registration(ticket, db) or "-"
    product_label = _invoice_line_product_label(product)
    separator = " - "
    return (
        f"Ticket {ticket.ticket_no}"
        f"{separator}{ticket_date}"
        f"{separator}{vehicle_reg}"
        f"{separator}{product_label}"
    )


def _invoice_line_product_label(product: Product | None) -> str:
    if not product:
        return "Item"
    description = str(product.description or "").strip()
    code = str(product.code or "").strip()
    return description or code or "Item"


def _customer_billing_lines(customer: Customer | None) -> list[str]:
    if not customer:
        return []
    lines: list[str] = []
    for value in (customer.address_line1, customer.address_line2):
        part = str(value or "").strip()
        if part:
            lines.append(part)
    city = str(customer.city or "").strip()
    postcode = str(customer.postcode or "").strip()
    city_postcode = " ".join(part for part in (city, postcode) if part).strip()
    if city_postcode:
        lines.append(city_postcode)
    country = str(customer.country or "").strip()
    if country:
        lines.append(country)
    return lines


def _ticket_vehicle_registration(
    ticket: Ticket, db: Session, *, vehicle_registration: str | None = None
) -> str | None:
    registration = str(ticket.vehicle_reg_text or "").strip()
    if registration:
        return registration
    joined_registration = str(vehicle_registration or "").strip()
    if joined_registration:
        return joined_registration
    if ticket.vehicle_id:
        vehicle = db.get(Vehicle, ticket.vehicle_id)
        if vehicle and vehicle.registration:
            resolved = str(vehicle.registration).strip()
            if resolved:
                return resolved
    return None


def _ticket_product_display(product: Product | None) -> str:
    if not product:
        return "-"
    code = str(product.code or "").strip()
    description = str(product.description or "").strip()
    if code and description:
        return f"{code} - {description}"
    return code or description or "-"


def _ticket_unit_meta(ticket: Ticket) -> tuple[str, str]:
    product_unit = ticket.product.unit if ticket.product else None
    unit_name = str(
        ticket.pricing_unit_name or (product_unit.name if product_unit else "")
    ).strip()
    unit_type = str(
        ticket.pricing_unit_type or (product_unit.unit_type if product_unit else "")
    ).strip().upper()
    if not unit_type:
        basis = str(ticket.pricing_basis or "").strip().upper()
        if basis in {"COUNT", "WEIGHT"}:
            unit_type = basis
    return unit_name, unit_type


def _format_qty(value, *, fixed_three_decimals: bool) -> str:
    qty = _decimal(value).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    if fixed_three_decimals:
        return f"{qty:.3f}"
    return f"{qty:.3f}".rstrip("0").rstrip(".") or "0"


def _ticket_billable_display(ticket: Ticket) -> str:
    unit_name, unit_type = _ticket_unit_meta(ticket)
    billable_qty, _, _ = _resolve_ticket_invoice_values(ticket, ticket.product)

    if billable_qty is not None:
        if unit_type == "COUNT":
            return f"{_format_qty(billable_qty, fixed_three_decimals=False)} x {unit_name or 'units'}"
        return f"{_format_qty(billable_qty, fixed_three_decimals=True)} {unit_name or 'tonnes'}"

    if unit_type == "COUNT":
        count_qty = (
            ticket.pricing_qty_snapshot
            if ticket.pricing_qty_snapshot is not None
            else ticket.qty
        )
        if count_qty is None:
            return "-"
        return f"{_format_qty(count_qty, fixed_three_decimals=False)} x {unit_name or 'units'}"

    if unit_type == "WEIGHT":
        weight_qty = (
            ticket.pricing_billable_qty_snapshot
            if ticket.pricing_billable_qty_snapshot is not None
            else None
        )
        if weight_qty is not None:
            return f"{_format_qty(weight_qty, fixed_three_decimals=True)} {unit_name or 'tonnes'}"

        net_kg_value = (
            ticket.pricing_net_kg_snapshot
            if ticket.pricing_net_kg_snapshot is not None
            else ticket.net_kg
        )
        if net_kg_value is None:
            return "-"
        net_kg = _decimal(net_kg_value)
        normalized_name = unit_name.lower()
        if normalized_name in {"tonne", "tonnes"}:
            tonnes = net_kg / Decimal("1000")
            return f"{_format_qty(tonnes, fixed_three_decimals=True)} tonnes"
        if normalized_name == "kg":
            return f"{_format_qty(net_kg, fixed_three_decimals=False)} kg"
        return f"{_format_qty(net_kg, fixed_three_decimals=False)} kg"

    return "-"


def _is_waste_ticket_transaction(transaction_type) -> bool:
    if transaction_type is None:
        return False
    normalized = (
        transaction_type.value
        if hasattr(transaction_type, "value")
        else str(transaction_type)
    )
    return str(normalized).strip().upper() in WASTE_TRANSACTION_TYPES


def _ticket_ewc_code_display(ticket: Ticket) -> str | None:
    code_display = str(ticket.ewc_code_display or "").strip()
    has_star = "*" in code_display
    code_6_digits = "".join(ch for ch in code_display if ch.isdigit())
    if len(code_6_digits) == 6:
        code_display = f"{code_6_digits[0:2]} {code_6_digits[2:4]} {code_6_digits[4:6]}"
    elif not code_display:
        fallback_digits = "".join(ch for ch in str(ticket.ewc_code_6 or "") if ch.isdigit())
        if len(fallback_digits) == 6:
            code_display = f"{fallback_digits[0:2]} {fallback_digits[2:4]} {fallback_digits[4:6]}"
        else:
            code_display = str(ticket.ewc_code_6 or "").strip()
    if not code_display:
        return None
    if has_star and "*" not in code_display:
        code_display = f"{code_display}*"
    return code_display


def _ticket_waste_producer_display(ticket: Ticket) -> str | None:
    producer_name = str(ticket.waste_producer_name or "").strip()
    if producer_name:
        return producer_name
    producer_address = str(ticket.waste_producer_address or "").strip()
    return producer_address or None


def _ticket_is_hazardous(ticket: Ticket) -> bool:
    ewc_label = _ticket_ewc_code_display(ticket) or ""
    if "*" in ewc_label:
        return True
    if bool(ticket.ewc_hazardous):
        return True
    product_ewc = ticket.product.ewc_code if ticket.product else None
    return bool(getattr(product_ewc, "hazardous", False))


def _invoice_stop_blockers(
    db: Session, customer_id: int, tickets: list[Ticket]
) -> list[str]:
    blockers: list[str] = []
    customer = db.get(Customer, customer_id)
    if customer and customer.on_stop:
        blockers.append("Customer")
    if any(bool(getattr(ticket.haulier, "on_stop", False)) for ticket in tickets):
        blockers.append("Haulier")
    return blockers


def _invoice_on_stop_error(blockers: list[str]) -> str:
    if not blockers:
        return ""
    if len(blockers) == 1:
        subject = blockers[0]
        verb = "is"
    else:
        subject = "Customer and Haulier"
        verb = "are"
    return (
        f"Cannot generate invoice: {subject} {verb} ON STOP. "
        "You may record the ticket, but it cannot be completed/invoiced until stop is removed."
    )


def _customer_invoice_rules(db: Session, customer_id: int) -> tuple[bool, bool]:
    customer = db.get(Customer, customer_id)
    if not customer:
        return False, False
    return bool(customer.do_not_invoice), bool(customer.must_have_po)


def _fetch_ticket_candidates(
    db: Session, customer_id: int, date_from: date | None, date_to: date | None
) -> list[Ticket]:
    customer_do_not_invoice, _ = _customer_invoice_rules(db, customer_id)
    if customer_do_not_invoice:
        return []
    filters = [
        Ticket.customer_id == customer_id,
        Ticket.status == "COMPLETE",
        Ticket.invoice_id.is_(None),
        Ticket.dont_invoice.is_(False),
        Ticket.walk_in_sale.is_(False),
        Ticket.paid.is_(False),
    ]
    # Date filters are interpreted in server-local time (UTC by default).
    if date_from:
        filters.append(Ticket.datetime >= datetime.combine(date_from, time.min))
    if date_to:
        end_exclusive = datetime.combine(date_to + timedelta(days=1), time.min)
        filters.append(Ticket.datetime < end_exclusive)
    return (
        db.execute(
            select(Ticket)
            .options(
                joinedload(Ticket.haulier),
                joinedload(Ticket.product).joinedload(Product.unit),
            )
            .where(and_(*filters))
            .order_by(Ticket.datetime.asc())
        )
        .scalars()
        .all()
    )


def _invoiceable_ticket_filters(
    db: Session, customer_id: int, date_from: date | None, date_to: date | None
) -> list:
    customer_do_not_invoice, customer_must_have_po = _customer_invoice_rules(
        db, customer_id
    )
    if customer_do_not_invoice:
        return [text("1=0")]
    filters = [
        Ticket.customer_id == customer_id,
        Ticket.status == "COMPLETE",
        Ticket.invoice_id.is_(None),
        Ticket.dont_invoice.is_(False),
        Ticket.walk_in_sale.is_(False),
        Ticket.paid.is_(False),
        Ticket.product.has(
            and_(
                Product.unit_id.is_not(None),
                Product.unit.has(Unit.is_active.is_(True)),
            )
        ),
    ]
    if customer_must_have_po:
        filters.extend(
            [
                Ticket.po_number.is_not(None),
                func.length(func.trim(Ticket.po_number)) > 0,
            ]
        )
    # Date filters are interpreted in server-local time (UTC by default).
    if date_from:
        filters.append(Ticket.datetime >= datetime.combine(date_from, time.min))
    if date_to:
        end_exclusive = datetime.combine(date_to + timedelta(days=1), time.min)
        filters.append(Ticket.datetime < end_exclusive)
    return filters


def _fetch_invoiceable_ticket_candidates(
    db: Session, customer_id: int, date_from: date | None, date_to: date | None
) -> list[Ticket]:
    filters = _invoiceable_ticket_filters(db, customer_id, date_from, date_to)
    candidates = (
        db.execute(
            select(Ticket)
            .options(
                joinedload(Ticket.haulier),
                joinedload(Ticket.product).joinedload(Product.unit),
            )
            .where(and_(*filters))
            .order_by(Ticket.datetime.asc())
        )
        .scalars()
        .all()
    )
    return [
        ticket
        for ticket in candidates
        if _resolve_ticket_invoice_values(ticket, ticket.product)[2] is None
    ]


def _classify_tickets(
    tickets: list[Ticket],
    *,
    customer_must_have_po: bool = False,
) -> tuple[list[Ticket], list[tuple[Ticket, str]]]:
    included: list[Ticket] = []
    excluded: list[tuple[Ticket, str]] = []

    for ticket in tickets:
        if ticket.status == "VOID":
            excluded.append((ticket, "Voided"))
            continue
        if ticket.status != "COMPLETE":
            excluded.append((ticket, "Not complete"))
            continue
        if ticket.dont_invoice:
            excluded.append((ticket, "Don't invoice"))
            continue
        if ticket.invoice_id is not None:
            excluded.append((ticket, "Already invoiced"))
            continue
        if customer_must_have_po and not _has_po_number(ticket.po_number):
            excluded.append((ticket, "Missing PO"))
            continue
        _, _, exclusion_reason = _resolve_ticket_invoice_values(ticket, ticket.product)
        if exclusion_reason:
            excluded.append((ticket, exclusion_reason))
            continue

        included.append(ticket)

    return included, excluded


def _has_po_number(po_number: str | None) -> bool:
    return bool(str(po_number or "").strip())


def _sum_included_ticket_totals(tickets: list[Ticket]) -> Decimal:
    total = Decimal("0.00")
    for ticket in tickets:
        _, line_net, exclusion_reason = _resolve_ticket_invoice_values(
            ticket, ticket.product
        )
        if exclusion_reason or line_net is None:
            continue
        total += line_net
    return _money(total)


def _resolve_ticket_invoice_values(
    ticket: Ticket, product: Product | None
) -> tuple[Decimal | None, Decimal | None, str | None]:
    unit = getattr(product, "unit", None) if product else None
    unit_type = str(getattr(unit, "unit_type", "") or "").strip().upper()
    unit_price = ticket.unit_price

    has_price = unit_price is not None and _decimal(unit_price) >= 0
    price_value = _decimal(unit_price) if has_price else None

    if unit_type == "COUNT":
        qty = ticket.qty
        has_qty = qty is not None and _decimal(qty) > 0
        if not has_qty or not has_price:
            return None, None, INVOICE_EXCLUSION_MISSING_QTY_PRICE
        billable_qty = _decimal(qty)
    elif unit_type == "WEIGHT":
        has_net = ticket.net_kg is not None
        if not has_price and not has_net:
            return None, None, INVOICE_EXCLUSION_MISSING_WEIGHT_PRICE
        if not has_price:
            return None, None, INVOICE_EXCLUSION_MISSING_PRICE
        if not has_net:
            return None, None, INVOICE_EXCLUSION_MISSING_NET_WEIGHT

        snapshot_billable_qty = ticket.pricing_billable_qty_snapshot
        if snapshot_billable_qty is not None:
            billable_qty = _decimal(snapshot_billable_qty)
        else:
            computed_billable = _compute_weight_billable_qty(unit, ticket.net_kg)
            if computed_billable is None:
                return None, None, INVOICE_EXCLUSION_UNKNOWN_UNIT_TYPE
            billable_qty = computed_billable

        if billable_qty <= 0:
            return None, None, INVOICE_EXCLUSION_MISSING_WEIGHT_PRICE
    else:
        return None, None, INVOICE_EXCLUSION_UNKNOWN_UNIT_TYPE

    line_net = _money(billable_qty * price_value)
    if line_net <= 0:
        return None, None, INVOICE_EXCLUSION_ZERO_TOTAL
    return billable_qty, line_net, None


def _compute_weight_billable_qty(unit: Unit | None, net_kg_value) -> Decimal | None:
    if unit is None or net_kg_value is None:
        return None
    net_kg = _decimal(net_kg_value)
    unit_name = str(unit.name or "").strip().lower()
    if unit_name in ("tonne", "tonnes"):
        return (net_kg / Decimal("1000")).quantize(
            Decimal("0.001"), rounding=ROUND_HALF_UP
        )
    if unit_name == "kg":
        return net_kg.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return None
