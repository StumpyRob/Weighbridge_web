from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path

from sqlalchemy import select

from app.config import settings
from app.models import (
    CompanySetting,
    Customer,
    Invoice,
    InvoiceLine,
    PrintDestination,
    PrintTemplate,
)
from app.services.pdf import render_invoice_pdf_html


def _make_invoice_with_line(db_session) -> Invoice:
    customer = Customer(
        account_code="C-COMPANY-1",
        name="Company Logo Customer",
        address_line1="1 Branding Way",
        city="Leeds",
        postcode="LS1 1AA",
    )
    db_session.add(customer)
    db_session.flush()

    invoice = Invoice(
        invoice_no="INV-COMPANY-1",
        customer_id=customer.id,
        invoice_date=date(2026, 2, 22),
        due_date=date(2026, 3, 1),
        status="DRAFT",
        net_total=Decimal("100.00"),
        vat_total=Decimal("20.00"),
        gross_total=Decimal("120.00"),
        customer_snapshot_json={"vat_number": "GB-COMPANY-1"},
    )
    db_session.add(invoice)
    db_session.flush()

    line = InvoiceLine(
        invoice_id=invoice.id,
        description="Company logo line",
        quantity=Decimal("1.000"),
        unit_price=Decimal("100.00"),
        net=Decimal("100.00"),
        vat=Decimal("20.00"),
        gross=Decimal("120.00"),
        product_snapshot_json={"final_disposal_wip": False, "used_on_site_wip": False},
    )
    db_session.add(line)
    db_session.commit()
    return invoice


def _ensure_default_invoice_destination(db_session) -> None:
    template = db_session.execute(
        select(PrintTemplate).where(PrintTemplate.code == "COMPANY_LOGO_INVOICE_TEMPLATE")
    ).scalars().first()
    if template is None:
        template = PrintTemplate(
            code="COMPANY_LOGO_INVOICE_TEMPLATE",
            description="Company logo invoice template",
            document_type="INVOICE",
            format="HTML",
            content=(
                "<html><body>"
                "<h1>{{ company.name }}</h1>"
                "{% if company.logo_url %}<img src=\"{{ company.logo_url }}\" />{% endif %}"
                "<div>{{ invoice.invoice_no }}</div>"
                "</body></html>"
            ),
            is_active=True,
        )
        db_session.add(template)
        db_session.flush()

    destination = db_session.execute(
        select(PrintDestination).where(
            PrintDestination.document_type == "INVOICE",
            PrintDestination.is_default.is_(True),
            PrintDestination.is_active.is_(True),
        )
    ).scalars().first()
    if destination is None:
        destination = PrintDestination(
            name="Company Logo Invoice Destination",
            description="Default invoice destination for logo tests",
            document_type="INVOICE",
            template_id=template.id,
            delivery_type="PRINT_LOCAL_BROWSER",
            delivery_config={},
            is_default=True,
            is_active=True,
        )
        db_session.add(destination)
    else:
        destination.template_id = template.id
        destination.is_active = True
        destination.is_default = True
    db_session.commit()


def test_company_logo_upload_persists_file_and_path(client, db_session, monkeypatch, tmp_path):
    upload_dir = tmp_path / "uploads" / "company"
    monkeypatch.setattr(settings, "company_logo_upload_dir", str(upload_dir))

    response = client.post(
        "/admin/company",
        data={
            "name": "Acme Recycling Ltd",
            "logo_action": "upload",
        },
        files={
            "company_logo_file": (
                "logo.png",
                BytesIO(b"\x89PNG\r\n\x1a\nfake-png-data"),
                "image/png",
            )
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    setting = db_session.execute(
        select(CompanySetting).order_by(CompanySetting.id.asc()).limit(1)
    ).scalar_one()
    assert setting.company_logo_path is not None
    assert setting.company_logo_path.startswith("/static/uploads/company/")
    assert setting.company_logo_updated_at is not None

    uploaded_file = upload_dir / Path(setting.company_logo_path).name
    assert uploaded_file.is_file()

    remove_response = client.post(
        "/admin/company",
        data={
            "name": "Acme Recycling Ltd",
            "logo_action": "remove",
        },
        follow_redirects=False,
    )
    assert remove_response.status_code == 303
    db_session.refresh(setting)
    assert setting.company_logo_path is None
    assert not uploaded_file.exists()


def test_invoice_pdf_html_contains_company_logo_url_when_set(db_session):
    _ensure_default_invoice_destination(db_session)
    db_session.add(
        CompanySetting(
            name="Acme Logo Co",
            company_logo_path="/static/uploads/company/logo-test.png",
        )
    )
    db_session.commit()

    invoice = _make_invoice_with_line(db_session)
    html = render_invoice_pdf_html(invoice.id, db_session)

    assert "/static/uploads/company/logo-test.png" in html
    assert "Acme Logo Co" in html


def test_invoice_pdf_html_embeds_uploaded_company_logo_as_data_uri(
    db_session,
    monkeypatch,
    tmp_path,
):
    _ensure_default_invoice_destination(db_session)
    upload_dir = tmp_path / "uploads" / "company"
    upload_dir.mkdir(parents=True, exist_ok=True)
    logo_file = upload_dir / "logo-inline.png"
    logo_file.write_bytes(b"\x89PNG\r\n\x1a\nlogo-bytes")

    monkeypatch.setattr(settings, "company_logo_upload_dir", str(upload_dir))
    db_session.add(
        CompanySetting(
            name="Acme Inline Logo Co",
            company_logo_path="/static/uploads/company/logo-inline.png",
        )
    )
    db_session.commit()

    invoice = _make_invoice_with_line(db_session)
    html = render_invoice_pdf_html(invoice.id, db_session)

    assert "data:image/png;base64," in html
    assert "Acme Inline Logo Co" in html
