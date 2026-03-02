from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import html as html_lib
import logging
import mimetypes
import os
from pathlib import Path
import re
from threading import Lock
from typing import Any
from urllib.parse import urljoin, urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    CompanySetting,
    Customer,
    Invoice,
    InvoiceLine,
    PrintDestination,
    PrintTemplate,
)
from .print_context import build_print_base_context
from .print_payload import build_print_payload
from .print_render import render_template_content

INVOICE_TEMPLATE_DOCUMENT_TYPE = "INVOICE"
INVOICE_DEFAULT_TEMPLATE_CODE = "INVOICE_SYSTEM"
INVOICE_DEFAULT_TEMPLATE_DESCRIPTION = "Invoice (System)"
INVOICE_LEGACY_TEMPLATE_CODES = (
    "invoice_default",
    "invoice_a4_default",
    "inv_a4_standard",
)
_logger = logging.getLogger(__name__)
_renderer_status_lock = Lock()
_windows_dll_dir_handles: list[object] = []
PRINT_SAFE_STYLE_ELEMENT_ID = "print-safe-enforcement"
SINGLE_PAGE_TEMPLATE_ERROR = (
    "Template exceeds one page. Reduce content/margins or font sizes."
)
EXTERNAL_RESOURCE_BLOCK_ERROR = (
    "External resource blocked: templates may only use local assets or data URIs."
)
PRINT_SAFE_CSS = """
@page {
  size: A4;
  margin: 12mm;
}

* {
  background-image: none !important;
  box-shadow: none !important;
  text-shadow: none !important;
  filter: none !important;
  backdrop-filter: none !important;
}
"""


@dataclass(slots=True, frozen=True)
class PdfRendererStatus:
    available: bool
    detail: str | None = None


_renderer_status: PdfRendererStatus | None = None


def _configure_windows_weasyprint_dlls() -> None:
    if os.name != "nt":
        return

    configured_raw = str(os.environ.get("WEASYPRINT_DLL_DIRECTORIES") or "").strip()
    configured_dirs = [item.strip() for item in configured_raw.split(os.pathsep) if item.strip()]
    fallback_dirs = [
        r"C:\msys64\ucrt64\bin",
        r"C:\msys64\mingw64\bin",
    ]
    candidate_dirs = configured_dirs + [item for item in fallback_dirs if item not in configured_dirs]
    if not candidate_dirs:
        return

    current_path_entries = [item for item in str(os.environ.get("PATH") or "").split(os.pathsep) if item]
    path_changed = False
    for candidate in candidate_dirs:
        directory = Path(candidate)
        if not directory.is_dir():
            continue
        resolved = str(directory.resolve())
        if resolved not in current_path_entries:
            current_path_entries.insert(0, resolved)
            path_changed = True
        add_dll_directory = getattr(os, "add_dll_directory", None)
        if callable(add_dll_directory):
            try:
                handle = add_dll_directory(resolved)
            except OSError:
                continue
            _windows_dll_dir_handles.append(handle)

    if path_changed:
        os.environ["PATH"] = os.pathsep.join(current_path_entries)


def check_invoice_pdf_renderer(*, force: bool = False) -> PdfRendererStatus:
    global _renderer_status
    with _renderer_status_lock:
        if _renderer_status is not None and not force:
            return _renderer_status

        try:
            _configure_windows_weasyprint_dlls()
            from weasyprint import HTML  # type: ignore

            probe = HTML(string="<html><body>weasyprint-probe</body></html>").write_pdf()
            if not probe.startswith(b"%PDF"):
                raise RuntimeError("WeasyPrint probe did not return PDF bytes.")
            _renderer_status = PdfRendererStatus(available=True, detail=None)
            _logger.info("WeasyPrint self-check passed; invoice PDF renderer is active.")
        except Exception as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            _renderer_status = PdfRendererStatus(available=False, detail=detail)
            _logger.exception("WeasyPrint self-check failed; FALLBACK MODE ACTIVE.")
        return _renderer_status


def invoice_pdf_renderer_status() -> PdfRendererStatus:
    return check_invoice_pdf_renderer()


def resolve_default_invoice_pdf_template(db: Session) -> PrintTemplate | None:
    return resolve_default_template_for_document_type(
        db,
        document_type=INVOICE_TEMPLATE_DOCUMENT_TYPE,
        require_active=True,
    )


def render_invoice_pdf(
    invoice_id: int,
    db: Session,
    *,
    template: PrintTemplate | None = None,
    allow_builtin_template_fallback: bool = True,
    base_url: str | None = None,
    allow_fallback: bool = True,
    include_fallback_warning: bool = False,
) -> bytes:
    html = render_invoice_pdf_html(
        invoice_id,
        db,
        template=template,
        allow_builtin_template_fallback=allow_builtin_template_fallback,
    )
    return _html_to_pdf_bytes(
        html,
        base_url=base_url,
        allow_fallback=allow_fallback,
        include_fallback_warning=include_fallback_warning,
        enforce_print_safe=True,
        enforce_single_page=True,
    )


def render_html_pdf_bytes(
    html: str,
    *,
    base_url: str | None = None,
    allow_fallback: bool = True,
    include_fallback_warning: bool = False,
    enforce_print_safe: bool = True,
    enforce_single_page: bool = True,
) -> bytes:
    return _html_to_pdf_bytes(
        html,
        base_url=base_url,
        allow_fallback=allow_fallback,
        include_fallback_warning=include_fallback_warning,
        enforce_print_safe=enforce_print_safe,
        enforce_single_page=enforce_single_page,
    )


def inject_print_safe_css(html: str) -> str:
    source = str(html or "")
    if not source.strip():
        return source
    if PRINT_SAFE_STYLE_ELEMENT_ID in source:
        return source

    style_block = (
        f"<style id=\"{PRINT_SAFE_STYLE_ELEMENT_ID}\" data-print-safe=\"1\">"
        f"{PRINT_SAFE_CSS}</style>"
    )
    head_close_pattern = re.compile(r"</head>", re.IGNORECASE)
    if head_close_pattern.search(source):
        return head_close_pattern.sub(f"{style_block}</head>", source, count=1)

    body_open_pattern = re.compile(r"<body[^>]*>", re.IGNORECASE)
    if body_open_pattern.search(source):
        return body_open_pattern.sub(lambda match: f"{style_block}{match.group(0)}", source, count=1)

    return f"{style_block}{source}"


def count_html_pages(
    html: str,
    *,
    base_url: str | None = None,
) -> int:
    status = check_invoice_pdf_renderer()
    if not status.available:
        detail = status.detail or "Unknown WeasyPrint error."
        raise RuntimeError(
            "HTML print validation requires WeasyPrint. "
            f"Renderer self-check failed: {detail}"
        )

    resolved_base_url = _resolve_pdf_base_url(base_url)
    allowed_hosts = _allowed_template_resource_hosts(resolved_base_url)
    try:
        _configure_windows_weasyprint_dlls()
        from weasyprint import HTML, default_url_fetcher  # type: ignore

        document = HTML(
            string=html,
            base_url=resolved_base_url,
            url_fetcher=_secure_template_url_fetcher(
                default_url_fetcher,
                allowed_hosts=allowed_hosts,
            ),
        ).render()
    except ValueError as exc:
        message = str(exc)
        if EXTERNAL_RESOURCE_BLOCK_ERROR in message:
            raise RuntimeError(message) from exc
        raise
    except Exception as exc:
        message = str(exc)
        if EXTERNAL_RESOURCE_BLOCK_ERROR in message:
            raise RuntimeError(message) from exc
        raise RuntimeError(
            f"WeasyPrint rendering failed during single-page validation: {exc}"
        ) from exc

    return len(document.pages)


def prepare_html_for_print_output(
    html: str,
) -> str:
    return inject_print_safe_css(html)


def ensure_single_page_html(
    html: str,
    *,
    base_url: str | None = None,
) -> None:
    page_count = count_html_pages(html, base_url=base_url)
    if page_count != 1:
        raise ValueError(SINGLE_PAGE_TEMPLATE_ERROR)


def render_invoice_pdf_html(
    invoice_id: int,
    db: Session,
    *,
    template: PrintTemplate | None = None,
    allow_builtin_template_fallback: bool = True,
) -> str:
    context = build_invoice_pdf_context(invoice_id, db)
    resolved_template = template or resolve_default_invoice_pdf_template(db)
    if resolved_template is not None:
        return render_invoice_template_content(
            resolved_template.content,
            context,
            db=db,
        )
    raise RuntimeError("No invoice system template configured for INVOICE.")


def render_invoice_template_content(
    template_content: str,
    context: dict[str, object],
    *,
    db: Session | None = None,
) -> str:
    payload = context.get("payload") if isinstance(context.get("payload"), dict) else {}
    return render_template_content(
        template_content,
        db=db,
        payload=payload,
        extra_context=context,
    )


def render_invoice_template_preview_html(
    db: Session,
    *,
    template_content: str,
    invoice_id: int | None = None,
) -> tuple[str, Invoice | None]:
    context, invoice = build_invoice_pdf_preview_context(db, invoice_id=invoice_id)
    return (
        render_invoice_template_content(
            template_content,
            context,
            db=db,
        ),
        invoice,
    )


def resolve_default_template_for_document_type(
    db: Session,
    *,
    document_type: str,
    require_active: bool = True,
) -> PrintTemplate | None:
    normalized = str(document_type or "").strip().upper()
    if not normalized:
        return None

    destination = (
        db.execute(
            select(PrintDestination)
            .where(
                PrintDestination.document_type == normalized,
                PrintDestination.is_default.is_(True),
                PrintDestination.is_active.is_(True),
            )
            .order_by(PrintDestination.id.asc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    if destination is None or not destination.template_id:
        return None

    template = db.get(PrintTemplate, destination.template_id)
    if template is None:
        return None
    if str(template.document_type or "").strip().upper() != normalized:
        return None
    if require_active and not bool(template.is_active):
        return None

    return template


def find_seeded_invoice_pdf_template(
    db: Session,
    *,
    require_active: bool = False,
) -> PrintTemplate | None:
    candidate_codes = (
        INVOICE_DEFAULT_TEMPLATE_CODE,
        *INVOICE_LEGACY_TEMPLATE_CODES,
    )
    for code in candidate_codes:
        template = (
            db.execute(
                select(PrintTemplate)
                .where(
                    func.lower(PrintTemplate.code) == code.lower(),
                    PrintTemplate.document_type == INVOICE_TEMPLATE_DOCUMENT_TYPE,
                )
                .limit(1)
            )
            .scalars()
            .first()
        )
        if template is None:
            continue
        if require_active and not bool(template.is_active):
            continue
        return template
    return None


def ensure_seed_invoice_pdf_template(db: Session) -> tuple[PrintTemplate, bool]:
    changed = False
    template = find_seeded_invoice_pdf_template(db, require_active=False)
    if template is not None and template.code.lower() != INVOICE_DEFAULT_TEMPLATE_CODE.lower():
        has_target_code = (
            db.execute(
                select(PrintTemplate.id)
                .where(
                    func.lower(PrintTemplate.code)
                    == INVOICE_DEFAULT_TEMPLATE_CODE.lower(),
                    PrintTemplate.document_type == INVOICE_TEMPLATE_DOCUMENT_TYPE,
                )
                .limit(1)
            ).first()
            is not None
        )
        if not has_target_code:
            template.code = INVOICE_DEFAULT_TEMPLATE_CODE
            changed = True
    if template is None:
        template = PrintTemplate(
            code=INVOICE_DEFAULT_TEMPLATE_CODE,
            description=INVOICE_DEFAULT_TEMPLATE_DESCRIPTION,
            document_type=INVOICE_TEMPLATE_DOCUMENT_TYPE,
            format="HTML",
            content=_read_builtin_invoice_pdf_template_content(),
            is_system=True,
            is_active=True,
        )
        db.add(template)
        db.flush()
        changed = True
    elif not bool(template.is_active):
        template.is_active = True
        changed = True
    if template.description != INVOICE_DEFAULT_TEMPLATE_DESCRIPTION:
        template.description = INVOICE_DEFAULT_TEMPLATE_DESCRIPTION
        changed = True
    if not bool(template.is_system):
        template.is_system = True
        changed = True
    if not str(template.content or "").strip():
        template.content = _read_builtin_invoice_pdf_template_content()
        changed = True

    return template, changed


def _read_builtin_invoice_pdf_template_content() -> str:
    candidate = Path(__file__).resolve().parents[1] / "templates" / "invoices" / "pdf.html"
    if not candidate.is_file():
        raise RuntimeError("Built-in invoice PDF template file is missing.")
    try:
        content = candidate.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError("Built-in invoice PDF template could not be read.") from exc
    if not content:
        raise RuntimeError("Built-in invoice PDF template is empty.")
    return content


def build_invoice_pdf_context(invoice_id: int, db: Session) -> dict[str, object]:
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        raise LookupError("Invoice not found.")

    lines = _invoice_lines_for_render(db, invoice.id)
    if not lines:
        raise ValueError("Invoice has no line items.")

    return _build_invoice_pdf_context_from_rows(invoice=invoice, lines=lines, db=db)


def build_invoice_pdf_preview_context(
    db: Session,
    *,
    invoice_id: int | None = None,
) -> tuple[dict[str, object], Invoice | None]:
    resolved: Invoice | None = None
    if invoice_id:
        candidate = db.get(Invoice, invoice_id)
        if candidate and _invoice_has_lines(db, candidate.id):
            resolved = candidate

    if resolved is None:
        resolved = (
            db.execute(
                select(Invoice)
                .where(
                    select(InvoiceLine.id)
                    .where(InvoiceLine.invoice_id == Invoice.id)
                    .exists()
                )
                .order_by(Invoice.invoice_date.desc(), Invoice.id.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )

    if resolved is None:
        return _sample_invoice_pdf_context(db), None

    return build_invoice_pdf_context(resolved.id, db), resolved


def _invoice_lines_for_render(db: Session, invoice_id: int) -> list[InvoiceLine]:
    return (
        db.execute(
            select(InvoiceLine)
            .where(InvoiceLine.invoice_id == invoice_id)
            .order_by(InvoiceLine.id.asc())
        )
        .scalars()
        .all()
    )


def _invoice_has_lines(db: Session, invoice_id: int) -> bool:
    row = db.execute(
        select(InvoiceLine.id)
        .where(InvoiceLine.invoice_id == invoice_id)
        .limit(1)
    ).first()
    return row is not None


def _build_invoice_pdf_context_from_rows(
    *,
    invoice: Invoice,
    lines: list[InvoiceLine],
    db: Session,
) -> dict[str, object]:
    customer = db.get(Customer, invoice.customer_id) if invoice.customer_id else None
    customer_snapshot = _safe_snapshot_dict(invoice.customer_snapshot_json)
    vat_number = _snapshot_text(customer_snapshot.get("vat_number"))
    credit_limit_pence = _snapshot_int(customer_snapshot.get("credit_limit_pence"))
    is_cash_account = bool(customer_snapshot.get("is_cash_account"))

    rendered_lines: list[dict[str, object]] = []
    for line in lines:
        product_snapshot = _safe_snapshot_dict(line.product_snapshot_json)
        final_disposal_wip = bool(product_snapshot.get("final_disposal_wip"))
        used_on_site_wip = bool(product_snapshot.get("used_on_site_wip"))
        quantity_text = _format_quantity(line.quantity)
        unit_price_text = _format_money(line.unit_price)
        net_text = _format_money(line.net)
        vat_text = _format_money(line.vat)
        gross_text = _format_money(line.gross)
        rendered_lines.append(
            {
                "ticket_id": line.ticket_id,
                "description": line.description or "",
                "quantity_text": quantity_text,
                "unit_price_text": unit_price_text,
                "net_text": net_text,
                "vat_text": vat_text,
                "gross_text": gross_text,
                # Token-friendly aliases
                "qty": quantity_text,
                "unit_price": unit_price_text,
                "net": net_text,
                "vat": vat_text,
                "gross": gross_text,
                "final_disposal_wip": final_disposal_wip,
                "used_on_site_wip": used_on_site_wip,
                "wip_flags_text": (
                    f"Final disposal: {'Yes' if final_disposal_wip else 'No'} | "
                    f"Used on site: {'Yes' if used_on_site_wip else 'No'}"
                ),
            }
        )

    invoice_date_text = _format_date(invoice.invoice_date)
    due_date_text = _format_date(invoice.due_date)
    net_total_text = _format_money(invoice.net_total)
    vat_total_text = _format_money(invoice.vat_total)
    gross_total_text = _format_money(invoice.gross_total)
    customer_billing_lines = _customer_billing_lines(customer)

    base_context = build_print_base_context(db)
    company_context = (
        dict(base_context.get("company"))
        if isinstance(base_context.get("company"), dict)
        else {}
    )
    company_name = str(base_context.get("company_name") or company_context.get("name") or "")
    company_lines = list(base_context.get("company_lines") or company_context.get("lines") or [])
    company_logo_url = str(
        base_context.get("company_logo_url") or company_context.get("logo_url") or ""
    )
    company_logo_path = str(company_context.get("logo_path") or "")

    invoice_payload = build_print_payload(
        db,
        INVOICE_TEMPLATE_DOCUMENT_TYPE,
        source_id=int(invoice.id),
    )
    invoice_payload["datetime"] = invoice_payload.get("datetime") or invoice_date_text
    invoice_payload["due_date"] = invoice_payload.get("due_date") or (
        due_date_text if due_date_text != "-" else ""
    )
    invoice_payload["line_items"] = invoice_payload.get("line_items") or rendered_lines

    context: dict[str, object] = {
        **base_context,
        # Legacy keys used by the system template
        "invoice_id": invoice.id,
        "invoice_no": invoice.invoice_no,
        "invoice_date_text": invoice_date_text,
        "due_date_text": due_date_text,
        "status": invoice.status or "",
        "customer_name": _customer_name(customer, invoice.customer_id),
        "customer_account_code": (customer.account_code if customer else "") or "",
        "customer_billing_lines": customer_billing_lines,
        "customer_vat_number": vat_number,
        "customer_is_cash_account": is_cash_account,
        "customer_credit_limit_text": (
            _format_money(Decimal(credit_limit_pence) / Decimal("100"))
            if credit_limit_pence is not None
            else None
        ),
        "lines": rendered_lines,
        "net_total_text": net_total_text,
        "vat_total_text": vat_total_text,
        "gross_total_text": gross_total_text,
        "company_name": company_name,
        "company_lines": company_lines,
        "company_logo_url": company_logo_url,
        "payload": invoice_payload,
        # New token model for editable invoice templates
        "invoice": {
            "id": invoice.id,
            "invoice_no": str(invoice_payload.get("invoice_no") or ""),
            "invoice_date": str(invoice_payload.get("datetime") or ""),
            "due_date": str(invoice_payload.get("due_date") or ""),
            "status": str(invoice_payload.get("status") or ""),
        },
        "customer": {
            "name": str(invoice_payload.get("customer_name") or ""),
            "billing_lines": customer_billing_lines,
            "vat_number": vat_number or "",
            "is_cash_account": is_cash_account,
            "credit_limit_pence": credit_limit_pence,
            "credit_limit": (
                _format_money(Decimal(credit_limit_pence) / Decimal("100"))
                if credit_limit_pence is not None
                else ""
            ),
        },
        "totals": {
            "net": str(invoice_payload.get("net_total_display") or net_total_text),
            "vat": str(invoice_payload.get("vat_total_display") or vat_total_text),
            "gross": str(invoice_payload.get("gross_total_display") or gross_total_text),
        },
        "company": {
            "name": company_name,
            "lines": company_lines,
            "logo_path": company_logo_path,
            "logo_url": company_logo_url,
        },
    }
    return context


def _sample_invoice_pdf_context(db: Session | None = None) -> dict[str, object]:
    base_context = build_print_base_context(db)
    payload = build_print_payload(db, INVOICE_TEMPLATE_DOCUMENT_TYPE, source_id=None)
    company_context = (
        dict(base_context.get("company"))
        if isinstance(base_context.get("company"), dict)
        else {}
    )
    company_name = str(base_context.get("company_name") or company_context.get("name") or "Your Company Name")
    company_lines = list(
        base_context.get("company_lines")
        or company_context.get("lines")
        or [
            "Company Address Line 1",
            "Company Address Line 2",
            "Company Town, POSTCODE",
        ]
    )
    company_logo_url = str(
        base_context.get("company_logo_url") or company_context.get("logo_url") or ""
    )
    company_logo_path = str(company_context.get("logo_path") or "")

    sample_line = {
        "ticket_id": 1001,
        "description": "Sample line item",
        "quantity_text": "1",
        "unit_price_text": "100.00",
        "net_text": "100.00",
        "vat_text": "20.00",
        "gross_text": "120.00",
        "qty": "1",
        "unit_price": "100.00",
        "net": "100.00",
        "vat": "20.00",
        "gross": "120.00",
        "final_disposal_wip": False,
        "used_on_site_wip": False,
        "wip_flags_text": "Final disposal: No | Used on site: No",
    }
    return {
        **base_context,
        "invoice_id": 0,
        "invoice_no": str(payload.get("invoice_no") or "INV-SAMPLE"),
        "invoice_date_text": str(payload.get("datetime") or date.today().strftime("%d/%m/%Y")),
        "due_date_text": "",
        "status": str(payload.get("status") or "DRAFT"),
        "customer_name": str(payload.get("customer_name") or "Sample Customer"),
        "customer_account_code": str(payload.get("customer_account_code") or "C-SAMPLE"),
        "customer_billing_lines": ["1 Sample Street", "Town", "POSTCODE"],
        "customer_vat_number": "",
        "customer_is_cash_account": False,
        "customer_credit_limit_text": None,
        "lines": list(payload.get("line_items") or [sample_line]),
        "net_total_text": str(payload.get("net_total_display") or "100.00"),
        "vat_total_text": str(payload.get("vat_total_display") or "20.00"),
        "gross_total_text": str(payload.get("gross_total_display") or "120.00"),
        "company_name": company_name,
        "company_lines": company_lines,
        "company_logo_url": company_logo_url,
        "payload": payload,
        "invoice": {
            "id": 0,
            "invoice_no": str(payload.get("invoice_no") or "INV-SAMPLE"),
            "invoice_date": str(payload.get("datetime") or date.today().strftime("%d/%m/%Y")),
            "due_date": "",
            "status": str(payload.get("status") or "DRAFT"),
        },
        "customer": {
            "name": str(payload.get("customer_name") or "Sample Customer"),
            "billing_lines": ["1 Sample Street", "Town", "POSTCODE"],
            "vat_number": "",
            "is_cash_account": False,
            "credit_limit_pence": None,
            "credit_limit": "",
        },
        "totals": {
            "net": str(payload.get("net_total_display") or "100.00"),
            "vat": str(payload.get("vat_total_display") or "20.00"),
            "gross": str(payload.get("gross_total_display") or "120.00"),
        },
        "company": {
            "name": company_name,
            "lines": company_lines,
            "logo_path": company_logo_path,
            "logo_url": company_logo_url,
        },
    }


def _html_to_pdf_bytes(
    html: str,
    *,
    base_url: str | None = None,
    allow_fallback: bool = True,
    include_fallback_warning: bool = False,
    enforce_print_safe: bool = False,
    enforce_single_page: bool = False,
) -> bytes:
    resolved_base_url = _resolve_pdf_base_url(base_url)
    allowed_hosts = _allowed_template_resource_hosts(resolved_base_url)
    html_for_render = str(html or "")
    if enforce_print_safe:
        html_for_render = prepare_html_for_print_output(html_for_render)

    status = check_invoice_pdf_renderer()
    if enforce_single_page and not status.available:
        detail = status.detail or "Unknown WeasyPrint error."
        raise RuntimeError(
            "HTML print validation requires WeasyPrint. "
            f"Renderer self-check failed: {detail}"
        )

    if status.available:
        try:
            _configure_windows_weasyprint_dlls()
            from weasyprint import HTML, default_url_fetcher  # type: ignore

            document_html = HTML(
                string=html_for_render,
                base_url=resolved_base_url,
                url_fetcher=_secure_template_url_fetcher(
                    default_url_fetcher,
                    allowed_hosts=allowed_hosts,
                ),
            )
            if enforce_single_page:
                document = document_html.render()
                if len(document.pages) != 1:
                    raise ValueError(SINGLE_PAGE_TEMPLATE_ERROR)
                return document.write_pdf()
            return document_html.write_pdf()
        except ValueError as exc:
            message = str(exc)
            if EXTERNAL_RESOURCE_BLOCK_ERROR in message:
                raise RuntimeError(message) from exc
            raise
        except Exception as exc:
            message = str(exc)
            if EXTERNAL_RESOURCE_BLOCK_ERROR in message:
                raise RuntimeError(message) from exc
            _logger.exception("WeasyPrint render failed for invoice PDF.")
            if enforce_single_page or not allow_fallback:
                raise RuntimeError(
                    f"WeasyPrint rendering failed (FALLBACK MODE ACTIVE): {exc}"
                ) from exc
            fallback_html = (
                _prepend_fallback_warning(html_for_render, detail=str(exc))
                if include_fallback_warning
                else html_for_render
            )
            return _fallback_pdf_from_html(fallback_html)

    if not allow_fallback:
        detail = status.detail or "Unknown WeasyPrint error."
        raise RuntimeError(
            "Invoice PDF renderer unavailable (FALLBACK MODE ACTIVE). "
            f"WeasyPrint self-check failed: {detail}"
        )

    fallback_html = (
        _prepend_fallback_warning(html_for_render, detail=status.detail)
        if include_fallback_warning
        else html_for_render
    )
    return _fallback_pdf_from_html(fallback_html)


def _resolve_pdf_base_url(explicit_base_url: str | None) -> str | None:
    explicit = str(explicit_base_url or "").strip()
    if explicit:
        return explicit
    configured = str(settings.app_public_base_url or "").strip()
    if configured:
        return configured
    return None


def _allowed_template_resource_hosts(base_url: str | None) -> set[str]:
    hosts = {"localhost", "127.0.0.1", "::1", "testserver"}
    for candidate in (base_url, str(settings.app_public_base_url or "").strip()):
        raw = str(candidate or "").strip()
        if not raw:
            continue
        parsed = urlsplit(raw)
        if parsed.scheme.lower() not in {"http", "https"}:
            continue
        hostname = str(parsed.hostname or "").strip().lower()
        if hostname:
            hosts.add(hostname)
    return hosts


def _is_template_resource_url_allowed(url: str, *, allowed_hosts: set[str]) -> bool:
    raw = str(url or "").strip()
    if not raw:
        return True

    parsed = urlsplit(raw)
    scheme = parsed.scheme.lower()

    if scheme == "data":
        return True
    if scheme == "file":
        return True
    if scheme in {"http", "https"}:
        hostname = str(parsed.hostname or "").strip().lower()
        return bool(hostname and hostname in allowed_hosts)
    if scheme:
        return False

    if parsed.netloc:
        hostname = str(parsed.hostname or "").strip().lower()
        return bool(hostname and hostname in allowed_hosts)
    return True


def _secure_template_url_fetcher(default_fetcher, *, allowed_hosts: set[str]):
    def _fetch(url: str, *args, **kwargs):
        if not _is_template_resource_url_allowed(url, allowed_hosts=allowed_hosts):
            raise ValueError(f"{EXTERNAL_RESOURCE_BLOCK_ERROR} ({url})")
        return default_fetcher(url, *args, **kwargs)

    return _fetch


def _prepend_fallback_warning(html: str, *, detail: str | None) -> str:
    escaped_detail = html_lib.escape(str(detail or "").strip())
    detail_suffix = f" ({escaped_detail})" if escaped_detail else ""
    warning_banner = (
        "<div style=\"font-family: sans-serif; border: 1px solid #f59e0b; "
        "background: #fffbeb; color: #92400e; padding: 8px 10px; margin-bottom: 10px;\">"
        "WARNING: PDF fallback mode active. Install WeasyPrint system dependencies "
        "for full invoice layout rendering."
        f"{detail_suffix}</div>"
    )
    body_pattern = re.compile(r"<body[^>]*>", re.IGNORECASE)
    if body_pattern.search(html):
        return body_pattern.sub(
            lambda match: f"{match.group(0)}{warning_banner}",
            html,
            count=1,
        )
    return f"{warning_banner}{html}"


def _fallback_pdf_from_html(html: str) -> bytes:
    plain_text = _flatten_html_to_text(html)
    return _basic_pdf_from_text(plain_text)


def _flatten_html_to_text(html: str) -> str:
    with_line_breaks = re.sub(
        r"(?i)</(p|div|h1|h2|h3|h4|h5|h6|tr|li|table|thead|tbody|tfoot|section|header)>",
        "\n",
        html,
    )
    with_line_breaks = re.sub(r"(?i)<br\s*/?>", "\n", with_line_breaks)
    without_tags = re.sub(r"<[^>]+>", "", with_line_breaks)
    unescaped = html_lib.unescape(without_tags)
    lines = [re.sub(r"\s+", " ", line).strip() for line in unescaped.splitlines()]
    compact = [line for line in lines if line]
    return "\n".join(compact)


def _basic_pdf_from_text(text: str) -> bytes:
    lines = text.splitlines()
    if not lines:
        lines = [""]
    max_lines = 70
    if len(lines) > max_lines:
        lines = lines[: max_lines - 1] + ["... (content truncated in fallback PDF renderer)"]

    content_ops = ["BT", "/F1 10 Tf", "50 790 Td", "14 TL"]
    for line in lines:
        escaped = _escape_pdf_text(line)
        content_ops.append(f"({escaped}) Tj")
        content_ops.append("T*")
    content_ops.append("ET")
    stream = ("\n".join(content_ops) + "\n").encode("latin-1", errors="replace")

    objects: list[bytes] = []
    objects.append(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")
    objects.append(b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n")
    objects.append(
        b"3 0 obj\n"
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>\n"
        b"endobj\n"
    )
    objects.append(
        b"4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n"
    )
    objects.append(
        b"5 0 obj\n<< /Length "
        + str(len(stream)).encode("ascii")
        + b" >>\nstream\n"
        + stream
        + b"endstream\nendobj\n"
    )

    header = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    pdf_body = bytearray(header)
    offsets = [0]
    for obj in objects:
        offsets.append(len(pdf_body))
        pdf_body.extend(obj)

    xref_start = len(pdf_body)
    pdf_body.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    pdf_body.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf_body.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf_body.extend(
        (
            "trailer\n"
            f"<< /Size {len(offsets)} /Root 1 0 R >>\n"
            "startxref\n"
            f"{xref_start}\n"
            "%%EOF\n"
        ).encode("ascii")
    )
    return bytes(pdf_body)


def _escape_pdf_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _format_money(value: Any) -> str:
    money = _decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{money:,.2f}"


def _format_quantity(value: Any) -> str:
    qty = _decimal(value).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    return f"{qty:.3f}".rstrip("0").rstrip(".") or "0"


def _format_date(value: date | None) -> str:
    if not value:
        return "-"
    return value.strftime("%d/%m/%Y")


def _decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def _safe_snapshot_dict(raw: Any) -> dict:
    return raw if isinstance(raw, dict) else {}


def _snapshot_text(raw: Any) -> str | None:
    value = str(raw or "").strip()
    return value or None


def _snapshot_int(raw: Any) -> int | None:
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _customer_name(customer: Customer | None, customer_id: int | None) -> str:
    if customer and customer.name:
        return customer.name
    if customer_id:
        return f"Customer #{customer_id}"
    return "Customer"


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


def _company_setting(db: Session) -> CompanySetting | None:
    return (
        db.execute(select(CompanySetting).order_by(CompanySetting.id.asc()).limit(1))
        .scalars()
        .first()
    )


def _company_name(company: CompanySetting | None) -> str:
    if company and str(company.name or "").strip():
        return str(company.name or "").strip()
    return "Your Company Name"


def _company_lines(company: CompanySetting | None) -> list[str]:
    if company is None:
        return [
            "Company Address Line 1",
            "Company Address Line 2",
            "Company Town, POSTCODE",
        ]
    lines: list[str] = []
    for value in (company.address_line1, company.address_line2):
        part = str(value or "").strip()
        if part:
            lines.append(part)
    city = str(company.city or "").strip()
    postcode = str(company.postcode or "").strip()
    city_postcode = " ".join(part for part in (city, postcode) if part).strip()
    if city_postcode:
        lines.append(city_postcode)
    country = str(company.country or "").strip()
    if country:
        lines.append(country)
    return lines or [
        "Company Address Line 1",
        "Company Address Line 2",
        "Company Town, POSTCODE",
    ]


def _company_logo_path(company: CompanySetting | None) -> str:
    if company is None:
        return ""
    current = str(company.company_logo_path or "").strip()
    if current:
        return resolve_company_logo_web_path(current)
    return ""


def _company_logo_url(company: CompanySetting | None) -> str:
    logo_path = _company_logo_path(company)
    if not logo_path:
        return ""
    if logo_path.startswith("data:"):
        return logo_path
    data_uri = _company_logo_data_uri(logo_path)
    if data_uri:
        return data_uri
    if logo_path.startswith("/static/uploads/company/"):
        _logger.warning(
            "Company logo path %s could not be resolved on disk; falling back to URL.",
            logo_path,
        )
    if logo_path.startswith(("http://", "https://")):
        return logo_path
    if logo_path.startswith("/"):
        public_base = str(settings.app_public_base_url or "").strip()
        if public_base:
            base = public_base if public_base.endswith("/") else f"{public_base}/"
            return urljoin(base, logo_path.lstrip("/"))
    return logo_path


def _company_logo_data_uri(logo_path: str) -> str:
    source = _logo_file_from_logo_path(logo_path)
    if source is None or not source.is_file():
        return ""
    try:
        payload = source.read_bytes()
    except OSError:
        return ""
    if not payload:
        return ""
    mime = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _logo_file_from_logo_path(logo_path: str) -> Path | None:
    normalized = str(logo_path or "").strip()
    if not normalized:
        return None
    if normalized.startswith("/static/uploads/company/"):
        filename = Path(normalized).name
        if not filename:
            return None
        for upload_root in _logo_upload_root_candidates():
            candidate = (upload_root / filename).resolve()
            if candidate.is_file():
                return candidate
        return None
    if normalized.startswith("/media/"):
        media_root = Path(str(settings.media_root or "").strip() or "app/media")
        relative = normalized.removeprefix("/media/").strip().lstrip("/\\")
        if not relative:
            return None
        return (media_root.resolve() / relative).resolve()
    if Path(normalized).is_absolute():
        absolute = Path(normalized)
        return absolute if absolute.is_file() else None
    return None


def _logo_upload_root_candidates() -> tuple[Path, ...]:
    configured = Path(
        str(settings.company_logo_upload_dir or "").strip() or "app/static/uploads/company"
    )
    service_default = Path("app/static/uploads/company")
    package_default = Path(__file__).resolve().parents[1] / "static" / "uploads" / "company"
    docker_alt = Path("/app/static/uploads/company")
    docker_repo = Path("/app/app/static/uploads/company")

    candidates: list[Path] = []
    for root in (configured, service_default, package_default, docker_alt, docker_repo):
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved in candidates:
            continue
        candidates.append(resolved)
    return tuple(candidates)
