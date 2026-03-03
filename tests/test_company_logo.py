from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest
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


@pytest.fixture()
def client(client_logged_in_no_setup):
    return client_logged_in_no_setup


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


def test_company_logo_upload_accepts_png_with_generic_content_type(
    client,
    db_session,
    monkeypatch,
    tmp_path,
):
    uploads_root = tmp_path / "uploads"
    monkeypatch.setattr(settings, "uploads_dir", str(uploads_root))

    response = client.post(
        "/admin/company",
        data={
            "name": "Acme Transparent",
            "logo_action": "upload",
            "show_nav_logo": "1",
            "show_nav_title": "1",
        },
        files={
            "company_logo_file": (
                "transparent-logo.png",
                BytesIO(b"\x89PNG\r\n\x1a\nalpha-png"),
                "application/octet-stream",
            )
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    setting = db_session.execute(
        select(CompanySetting).order_by(CompanySetting.id.asc()).limit(1)
    ).scalar_one()
    assert setting.company_logo_path is not None
    assert setting.company_logo_path.endswith(".png")


def test_company_settings_get_does_not_create_row(client, db_session):
    before_count = db_session.execute(select(CompanySetting)).scalars().all()
    assert len(before_count) == 0

    response = client.get("/admin/company")

    assert response.status_code == 200
    after_count = db_session.execute(select(CompanySetting)).scalars().all()
    assert len(after_count) == 0


def test_company_settings_rejects_invalid_hex_colors(client):
    response = client.post(
        "/admin/company",
        data={
            "name": "Acme Recycling Ltd",
            "navbar_color_hex": "not-a-color",
            "primary_color_hex": "#12",
            "nav_logo_height_px": "34",
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "Navbar colour must be a valid HEX colour" in response.text
    assert "Primary colour must be a valid HEX colour" in response.text


def test_company_settings_theme_values_apply_globally(client, db_session):
    response = client.post(
        "/admin/company",
        data={
            "name": "Acme Theme Ltd",
            "navbar_color_hex": "#112233",
            "primary_color_hex": "#EE7700",
            "nav_logo_height_px": "44",
            "show_nav_logo": "0",
            "show_nav_title": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    setting = db_session.execute(
        select(CompanySetting).order_by(CompanySetting.id.asc()).limit(1)
    ).scalar_one()
    assert setting.navbar_color_hex == "#112233"
    assert setting.primary_color_hex == "#EE7700"
    assert setting.nav_logo_height_px == 44
    assert bool(setting.show_nav_logo) is False
    assert bool(setting.show_nav_title) is True
    setting.is_initialized = True
    db_session.commit()

    themed_page = client.get("/tickets")
    assert themed_page.status_code == 200
    assert "--theme-navbar-bg: #112233" in themed_page.text
    assert "--theme-primary: #EE7700" in themed_page.text


def test_base_template_uses_company_branding_for_logo_favicon_and_theme(
    client,
    db_session,
    monkeypatch,
    tmp_path,
):
    uploads_root = tmp_path / "uploads"
    company_dir = uploads_root / "company"
    company_dir.mkdir(parents=True, exist_ok=True)
    (company_dir / "logo-nav.png").write_bytes(b"\x89PNG\r\n\x1a\nlogo-nav-bytes")
    monkeypatch.setattr(settings, "uploads_dir", str(uploads_root))

    db_session.add(
        CompanySetting(
            name="Acme Branding",
            company_logo_path="/static/uploads/company/logo-nav.png",
            navbar_color_hex="#112233",
            primary_color_hex="#EE7700",
            nav_logo_height_px=48,
            show_nav_logo=True,
            show_nav_title=True,
            is_initialized=True,
        )
    )
    db_session.commit()

    response = client.get("/tickets")

    assert response.status_code == 200
    assert "--theme-navbar-bg: #112233" in response.text
    assert "--theme-primary: #EE7700" in response.text
    assert "--theme-nav-logo-height: 48px" in response.text
    assert 'rel="icon"' in response.text
    assert "/static/uploads/company/logo-nav.png" in response.text
    assert 'class="brand__logo"' in response.text
    assert "Acme Branding" in response.text


def test_base_template_respects_nav_brand_visibility_toggles(
    client,
    db_session,
    monkeypatch,
    tmp_path,
):
    uploads_root = tmp_path / "uploads"
    company_dir = uploads_root / "company"
    company_dir.mkdir(parents=True, exist_ok=True)
    (company_dir / "logo-toggle.png").write_bytes(b"\x89PNG\r\n\x1a\nlogo-toggle-bytes")
    monkeypatch.setattr(settings, "uploads_dir", str(uploads_root))

    db_session.add(
        CompanySetting(
            name="Toggle Branding",
            company_logo_path="/static/uploads/company/logo-toggle.png",
            show_nav_logo=True,
            show_nav_title=False,
            is_initialized=True,
        )
    )
    db_session.commit()

    response_logo_only = client.get("/tickets")
    assert response_logo_only.status_code == 200
    assert 'class="brand__logo"' in response_logo_only.text
    assert 'class="brand__text"' not in response_logo_only.text
    assert "/static/uploads/company/logo-toggle.png" in response_logo_only.text

    update = client.post(
        "/admin/company",
        data={
            "name": "Toggle Branding",
            "show_nav_logo": "0",
            "show_nav_title": "1",
        },
        follow_redirects=False,
    )
    assert update.status_code == 303

    response_title_only = client.get("/tickets")
    assert response_title_only.status_code == 200
    assert 'class="brand__logo"' not in response_title_only.text
    assert 'class="brand__text">Toggle Branding<' in response_title_only.text


def test_base_template_falls_back_to_default_logo_when_upload_missing(client, db_session):
    db_session.add(
        CompanySetting(
            name="Missing Logo Co",
            company_logo_path="/static/uploads/company/missing-logo.png",
            show_nav_logo=True,
            show_nav_title=True,
            is_initialized=True,
        )
    )
    db_session.commit()

    response = client.get("/tickets")

    assert response.status_code == 200
    assert "/static/uploads/company/missing-logo.png" not in response.text
    assert "/static/img/default-company-logo.svg" in response.text


def test_invoice_pdf_html_contains_company_logo_url_when_set(
    db_session,
    monkeypatch,
    tmp_path,
):
    uploads_root = tmp_path / "uploads"
    company_dir = uploads_root / "company"
    company_dir.mkdir(parents=True, exist_ok=True)
    (company_dir / "logo-test.png").write_bytes(b"\x89PNG\r\n\x1a\nlogo-test-bytes")
    monkeypatch.setattr(settings, "uploads_dir", str(uploads_root))

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

    assert (
        "/static/uploads/company/logo-test.png" in html
        or "data:image/png;base64," in html
    )
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
