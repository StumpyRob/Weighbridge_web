import base64
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select

from app.config import settings
from app.models import (
    AuditEvent,
    CompanySetting,
    Container,
    Customer,
    Destination,
    DirectionEnum,
    Driver,
    Haulier,
    PrintAgent,
    PrintDestination,
    PrintJob,
    PrintTemplate,
    Product,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
    Unit,
    User,
    Vehicle,
    PlatformSetting,
)
from app.services.email_service import EmailSendResult
from app.services.print_agents import hash_print_agent_key
from app.services.print_payload import build_ticket_print_payload


def _set_ticket_browser_destination(
    db_session,
    *,
    code: str,
    template_format: str,
    content: str,
    auto_print_on_complete: bool = False,
) -> PrintDestination:
    template = PrintTemplate(
        code=code,
        description=code,
        document_type="TICKET",
        format=template_format,
        content=content,
        is_active=True,
    )
    db_session.add(template)
    db_session.flush()

    destination = PrintDestination(
        name=f"{code} Destination",
        description=f"{code} Destination",
        document_type="TICKET",
        template_id=template.id,
        delivery_type="PRINT_LOCAL_BROWSER",
        delivery_config=(
            {"auto_print_on_complete": True} if auto_print_on_complete else {}
        ),
        is_default=True,
        is_active=True,
    )
    db_session.add(destination)
    db_session.commit()
    db_session.refresh(destination)
    return destination


def _set_ticket_pull_destination(
    db_session,
    *,
    code: str,
    content: str,
    agent_id: str,
    printer_name: str = "Yard Printer",
    copies: int = 1,
    auto_print_on_complete: bool = False,
) -> PrintDestination:
    template = PrintTemplate(
        code=code,
        description=code,
        document_type="TICKET",
        format="TEXT",
        content=content,
        is_active=True,
    )
    db_session.add(template)
    db_session.flush()

    destination = PrintDestination(
        name=f"{code} Destination",
        description=f"{code} Destination",
        document_type="TICKET",
        template_id=template.id,
        delivery_type="PRINT_AGENT_PULL",
        delivery_config={
            "agent_id": agent_id,
            "printer_name": printer_name,
            "copies": copies,
            **({"auto_print_on_complete": True} if auto_print_on_complete else {}),
        },
        is_default=True,
        is_active=True,
    )
    db_session.add(destination)
    db_session.commit()
    db_session.refresh(destination)
    return destination


def _create_complete_sale_ticket(
    db_session,
    *,
    ticket_no: str,
    invoice_email: str | None = None,
) -> Ticket:
    customer = Customer(
        account_code=f"AC-{ticket_no}",
        name=f"Customer {ticket_no}",
        invoice_email=invoice_email,
    )
    db_session.add(customer)
    db_session.flush()

    ticket = Ticket(
        ticket_no=ticket_no,
        datetime=datetime(2026, 2, 20, 12, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        customer_id=customer.id,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


def _create_open_sale_ticket(db_session, *, ticket_no: str) -> Ticket:
    customer = Customer(
        account_code=f"AC-{ticket_no}",
        name=f"Customer {ticket_no}",
    )
    unit = Unit(name=f"Each {ticket_no}", unit_type="COUNT", is_active=True)
    product = Product(
        code=f"P-{ticket_no}",
        description=f"Product {ticket_no}",
        unit=unit,
        unit_price=Decimal("12.50"),
    )
    db_session.add_all([customer, unit, product])
    db_session.flush()

    ticket = Ticket(
        ticket_no=ticket_no,
        datetime=datetime(2026, 3, 31, 9, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        customer_id=customer.id,
        product_id=product.id,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


def _complete_ticket(client, ticket: Ticket):
    return client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "complete",
            "datetime": ticket.datetime.strftime("%Y-%m-%dT%H:%M"),
            "direction": "INWARD",
            "transaction_type": "SALE",
            "customer_id": str(ticket.customer_id or ""),
            "product_id": str(ticket.product_id or ""),
            "qty": "1",
            "unit_price": "",
        },
        follow_redirects=False,
    )


def _upsert_company_setting(db_session, **values) -> CompanySetting:
    setting = db_session.execute(
        select(CompanySetting).order_by(CompanySetting.id.asc()).limit(1)
    ).scalars().first()
    if setting is None:
        setting = CompanySetting()
        db_session.add(setting)
    for key, value in values.items():
        setattr(setting, key, value)
    db_session.commit()
    db_session.refresh(setting)
    return setting


def _ticket_email_audit_event(db_session, *, action: str, ticket_id: int) -> AuditEvent | None:
    return db_session.execute(
        select(AuditEvent)
        .where(
            AuditEvent.action == action,
            AuditEvent.entity_type == "ticket",
            AuditEvent.entity_id == str(ticket_id),
        )
        .order_by(AuditEvent.id.desc())
    ).scalars().first()


def test_build_ticket_print_payload_sale_fields(db_session):
    customer = Customer(account_code="C-PRINT-SALE", name="Print Sale Customer")
    vehicle = Vehicle(registration="PRINT123")
    unit = Unit(name="Each", unit_type="COUNT", is_active=True)
    product = Product(
        code="P-PRINT-SALE",
        description="Print Sale Product",
        unit=unit,
        unit_price=Decimal("12.50"),
    )
    db_session.add_all([customer, vehicle, unit, product])
    db_session.flush()

    ticket = Ticket(
        ticket_no="T-PRINT-SALE-1",
        datetime=datetime(2026, 2, 19, 9, 30, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        customer_id=customer.id,
        vehicle_id=vehicle.id,
        product_id=product.id,
        final_disposal=True,
        used_on_site=False,
        qty=Decimal("2.000"),
        unit_price=Decimal("12.50"),
        total=Decimal("25.00"),
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    payload = build_ticket_print_payload(db_session, ticket)

    assert payload["ticket_no"] == "T-PRINT-SALE-1"
    assert payload["transaction_type"] == "SALE"
    assert payload["is_sale"] is True
    assert payload["customer_name"] == "Print Sale Customer"
    assert payload["vehicle_reg"] == "PRINT123"
    assert payload["product_code"] == "P-PRINT-SALE"
    assert payload["final_disposal"] is True
    assert payload["used_on_site"] is False
    assert "logo_data_uri" in payload


def test_build_ticket_print_payload_uses_company_uploaded_logo(
    db_session,
    monkeypatch,
    tmp_path,
):
    company = db_session.execute(
        select(CompanySetting).order_by(CompanySetting.id.asc()).limit(1)
    ).scalars().first()
    if company is None:
        company = CompanySetting()
        db_session.add(company)
    company.name = "Ticket Logo Co"
    company.company_logo_path = "/static/uploads/company/ticket-logo.png"
    db_session.flush()
    tenant_id = int(company.tenant_id or 1)

    uploads_root = tmp_path / "uploads"
    upload_dir = uploads_root / "tenants" / str(tenant_id) / "company"
    upload_dir.mkdir(parents=True, exist_ok=True)
    logo_file = upload_dir / "ticket-logo.png"
    logo_file.write_bytes(b"\x89PNG\r\n\x1a\nlogo-bytes")
    monkeypatch.setattr(settings, "uploads_dir", str(uploads_root))

    ticket = Ticket(
        ticket_no="T-PRINT-LOGO-1",
        datetime=datetime(2026, 2, 19, 9, 45, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    payload = build_ticket_print_payload(db_session, ticket)

    assert payload["logo_data_uri"].startswith("data:image/png;base64,")


def test_build_ticket_print_payload_waste_fields(db_session):
    customer = Customer(account_code="C-PRINT-WASTE", name="Print Waste Customer")
    haulier = Haulier(name="Print Haulier", carrier_licence_number="CBDU12345")
    driver = Driver(name="Print Driver")
    container = Container(name="Print Container")
    destination = Destination(name="Print Destination")
    db_session.add_all([customer, haulier, driver, container, destination])
    db_session.flush()

    ticket = Ticket(
        ticket_no="T-PRINT-WASTE-1",
        datetime=datetime(2026, 2, 19, 10, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        customer_id=customer.id,
        haulier_id=haulier.id,
        driver_id=driver.id,
        container_id=container.id,
        destination_id=destination.id,
        final_disposal=False,
        used_on_site=True,
        gross_kg=Decimal("21000.000"),
        tare_kg=Decimal("8000.000"),
        net_kg=Decimal("13000.000"),
        ewc_code_display="17 09 04",
        ewc_code_6="170904",
        ewc_description="Mixed construction waste",
        ewc_hazardous=False,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    payload = build_ticket_print_payload(db_session, ticket)

    assert payload["transaction_type"] == "WASTEIN"
    assert payload["is_waste"] is True
    assert payload["haulier_name"] == "Print Haulier"
    assert payload["driver_name"] == "Print Driver"
    assert payload["destination_name"] == "Print Destination"
    assert payload["ewc_code"] == "17 09 04"
    assert payload["final_disposal"] is False
    assert payload["used_on_site"] is True


def test_build_ticket_print_payload_walk_in_fields(db_session):
    unit = Unit(name="Each", unit_type="COUNT", is_active=True)
    product = Product(
        code="P-PRINT-WALKIN",
        description="Print Walk-in Product",
        unit=unit,
        unit_price=Decimal("8.00"),
    )
    db_session.add_all([unit, product])
    db_session.flush()

    ticket = Ticket(
        ticket_no="T-PRINT-WALKIN-1",
        datetime=datetime(2026, 2, 19, 10, 30, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        product_id=product.id,
        walk_in_sale=True,
        qty=Decimal("1.000"),
        unit_price=Decimal("8.00"),
        total=Decimal("8.00"),
        dont_invoice=True,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    payload = build_ticket_print_payload(db_session, ticket)

    assert payload["is_sale"] is True
    assert payload["customer_name"] == ""
    assert payload["ticket_no"] == "T-PRINT-WALKIN-1"


def test_ticket_print_thermal_route_contains_key_fields(client, db_session):
    customer = Customer(account_code="C-PRINT-ROUTE", name="Print Route Customer")
    vehicle = Vehicle(registration="RTE123")
    unit = Unit(name="Each", unit_type="COUNT", is_active=True)
    product = Product(
        code="P-PRINT-ROUTE",
        description="Print Route Product",
        unit=unit,
        unit_price=Decimal("9.50"),
    )
    db_session.add_all([customer, vehicle, unit, product])
    db_session.flush()

    ticket = Ticket(
        ticket_no="T-PRINT-ROUTE-1",
        datetime=datetime(2026, 2, 19, 11, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        customer_id=customer.id,
        vehicle_id=vehicle.id,
        product_id=product.id,
        qty=Decimal("3.000"),
        unit_price=Decimal("9.50"),
        total=Decimal("28.50"),
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    _set_ticket_browser_destination(
        db_session,
        code="TICKET_ROUTE_TEXT",
        template_format="TEXT",
        content=(
            "Ticket: {{ payload.ticket_no }}\n"
            "Transaction: {{ payload.transaction_type }}\n"
            "Vehicle: {{ payload.vehicle_reg }}\n"
            "Customer: {{ payload.customer_name }}"
        ),
    )

    response = client.get(f"/tickets/{ticket.id}/preview")

    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    assert "Ticket: T-PRINT-ROUTE-1" in response.text
    assert "Transaction: SALE" in response.text
    assert "Vehicle: RTE123" in response.text
    assert "Customer: Print Route Customer" in response.text


def test_ticket_print_a4_route_renders_html_preview(client, db_session):
    ticket = Ticket(
        ticket_no="T-PRINT-A4-1",
        datetime=datetime(2026, 2, 19, 11, 30, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    _set_ticket_browser_destination(
        db_session,
        code="TICKET_ROUTE_HTML",
        template_format="HTML",
        content=(
            "<html><body><div class=\"ticket-header\">"
            "<h1>Ticket {{ payload.ticket_no }}</h1>"
            "</div></body></html>"
        ),
    )

    response = client.get(f"/tickets/{ticket.id}/preview")

    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    assert "ticket-header" in response.text
    assert "<h1>Ticket T-PRINT-A4-1</h1>" in response.text


def test_ticket_pdf_download_route_returns_attachment_pdf_headers(
    client,
    db_session,
    monkeypatch,
):
    import app.routes.tickets as tickets_routes

    ticket = Ticket(
        ticket_no="T-PDF-1",
        datetime=datetime(2026, 2, 19, 11, 35, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    _set_ticket_browser_destination(
        db_session,
        code="TICKET_ROUTE_PDF",
        template_format="HTML",
        content="<html><body><h1>Ticket {{ payload.ticket_no }}</h1></body></html>",
    )

    called: dict[str, object] = {}

    def _fake_render_html_pdf_bytes(rendered_html, **kwargs):
        called["rendered_html"] = rendered_html
        called["kwargs"] = kwargs
        return b"%PDF-1.4\n%stub-ticket-pdf\n"

    monkeypatch.setattr(
        tickets_routes,
        "render_html_pdf_bytes",
        _fake_render_html_pdf_bytes,
    )

    response = client.get(f"/tickets/{ticket.id}/pdf")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.headers.get("content-disposition", "") == 'attachment; filename="Ticket-T-PDF-1.pdf"'
    assert response.content.startswith(b"%PDF")
    assert "T-PDF-1" in str(called["rendered_html"])


def test_ticket_print_thermal_route_requires_complete(client, db_session):
    ticket = Ticket(
        ticket_no="T-PRINT-THERMAL-OPEN-1",
        datetime=datetime(2026, 2, 19, 11, 40, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.get(f"/tickets/{ticket.id}/preview")

    assert response.status_code == 400


def test_ticket_print_a4_route_requires_complete(client, db_session):
    ticket = Ticket(
        ticket_no="T-PRINT-A4-OPEN-1",
        datetime=datetime(2026, 2, 19, 11, 50, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.get(f"/tickets/{ticket.id}/preview")

    assert response.status_code == 400


def test_ticket_detail_shows_email_ticket_form_with_defaults(client, db_session):
    _upsert_company_setting(
        db_session,
        name="Ticket Email Co",
        ticket_email_subject_template="",
        ticket_email_body_template="",
    )
    ticket = _create_complete_sale_ticket(
        db_session,
        ticket_no="T-EMAIL-UI-1",
        invoice_email="billing@example.com",
    )
    _set_ticket_browser_destination(
        db_session,
        code="TICKET_EMAIL_UI",
        template_format="HTML",
        content="<html><body>Ticket {{ payload.ticket_no }}</body></html>",
    )

    response = client.get(f"/tickets/{ticket.id}")

    assert response.status_code == 200
    assert "Email Ticket" in response.text
    assert "Edit Email" in response.text
    assert 'data-document-email-primary-form' in response.text
    assert 'data-document-email-primary' in response.text
    assert f'action="/tickets/{ticket.id}/email"' in response.text
    assert 'name="to_email"' in response.text
    assert 'value="billing@example.com"' in response.text
    assert 'value="Ticket T-EMAIL-UI-1 from Ticket Email Co"' in response.text
    assert "Please find attached ticket T-EMAIL-UI-1." in response.text
    assert "Ticket Email Co" in response.text
    assert "Uses the configured ticket document output and platform email settings" in response.text


def test_ticket_primary_email_action_opens_form_when_customer_email_missing(client, db_session):
    ticket = _create_complete_sale_ticket(
        db_session,
        ticket_no="T-EMAIL-UI-NOEMAIL-1",
        invoice_email=None,
    )
    _set_ticket_browser_destination(
        db_session,
        code="TICKET_EMAIL_UI_NOEMAIL",
        template_format="HTML",
        content="<html><body>Ticket {{ payload.ticket_no }}</body></html>",
    )

    response = client.get(f"/tickets/{ticket.id}")

    assert response.status_code == 200
    assert f'href="/tickets/{ticket.id}?edit_email=1"' in response.text
    assert 'data-document-email-primary-form' not in response.text

    opened = client.get(f"/tickets/{ticket.id}?edit_email=1")
    assert opened.status_code == 200
    assert 'document-email-card" open' in opened.text


def test_ticket_send_by_email_uses_tenant_configured_subject_and_body_template(
    client,
    db_session,
    monkeypatch,
):
    import app.routes.tickets as tickets_routes

    _upsert_company_setting(
        db_session,
        name="Ticket Template Co",
        ticket_email_subject_template="Configured {ticket_no} / {company_name} / FD:{final_disposal} / UOS:{used_on_site}",
        ticket_email_body_template="Body for {ticket_no} from {company_name} (final disposal: {final_disposal}, used on site: {used_on_site})",
    )
    ticket = _create_complete_sale_ticket(
        db_session,
        ticket_no="T-EMAIL-TEMPLATE-1",
        invoice_email="billing@example.com",
    )
    ticket.final_disposal = True
    ticket.used_on_site = False
    db_session.commit()
    _set_ticket_browser_destination(
        db_session,
        code="TICKET_EMAIL_TEMPLATE",
        template_format="HTML",
        content="<html><body>EMAIL {{ payload.ticket_no }}</body></html>",
    )
    db_session.add(
        PlatformSetting(
            email_provider="resend",
            resend_api_key="re_test_api_key",
            from_email="platform@example.com",
            from_display_name="Weighbridge Web",
        )
    )
    db_session.commit()

    called: dict[str, object] = {}

    monkeypatch.setattr(
        tickets_routes,
        "render_html_pdf_bytes",
        lambda *_args, **_kwargs: b"%PDF-1.4\n%ticket-template-email\n",
    )

    def _fake_send_email(**kwargs):
        called.update(kwargs)
        return EmailSendResult(ok=True)

    monkeypatch.setattr(tickets_routes, "send_email", _fake_send_email)

    detail_response = client.get(f"/tickets/{ticket.id}")
    assert detail_response.status_code == 200
    assert (
        'value="Configured T-EMAIL-TEMPLATE-1 / Ticket Template Co / FD:Yes / UOS:No"'
        in detail_response.text
    )
    assert (
        "Body for T-EMAIL-TEMPLATE-1 from Ticket Template Co (final disposal: Yes, used on site: No)"
        in detail_response.text
    )

    response = client.post(
        f"/tickets/{ticket.id}/email",
        data={"to_email": "customer@example.com"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert (
        called["subject"]
        == "Configured T-EMAIL-TEMPLATE-1 / Ticket Template Co / FD:Yes / UOS:No"
    )
    assert (
        called["text_body"]
        == "Body for T-EMAIL-TEMPLATE-1 from Ticket Template Co (final disposal: Yes, used on site: No)"
    )


def test_ticket_send_by_email_happy_path_attaches_pdf_and_writes_audit(
    client,
    db_session,
    monkeypatch,
):
    import app.routes.tickets as tickets_routes

    ticket = _create_complete_sale_ticket(
        db_session,
        ticket_no="T-EMAIL-SEND-1",
        invoice_email="billing@example.com",
    )
    _set_ticket_browser_destination(
        db_session,
        code="TICKET_EMAIL_SEND",
        template_format="HTML",
        content="<html><body>EMAIL {{ payload.ticket_no }}</body></html>",
    )
    db_session.add(
        PlatformSetting(
            email_provider="resend",
            resend_api_key="re_test_api_key",
            from_email="platform@example.com",
            from_display_name="Weighbridge Web",
        )
    )
    db_session.commit()

    pdf_inputs: dict[str, object] = {}
    called: dict[str, object] = {}

    def _fake_render_html_pdf_bytes(rendered_html, **kwargs):
        pdf_inputs["rendered_html"] = rendered_html
        pdf_inputs["kwargs"] = kwargs
        return b"%PDF-1.4\n%ticket-email-pdf\n"

    def _fake_send_email(**kwargs):
        called.update(kwargs)
        return EmailSendResult(ok=True)

    monkeypatch.setattr(
        tickets_routes,
        "render_html_pdf_bytes",
        _fake_render_html_pdf_bytes,
    )
    monkeypatch.setattr(
        tickets_routes,
        "send_email",
        _fake_send_email,
    )

    response = client.post(
        f"/tickets/{ticket.id}/email",
        data={
            "to_email": "customer@example.com",
            "cc_email": "ops@example.com;accounts@example.com",
            "subject": "Ticket T-EMAIL-SEND-1",
            "message": "Attached ticket T-EMAIL-SEND-1.",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/tickets/{ticket.id}?email_sent=1"
    assert "T-EMAIL-SEND-1" in str(pdf_inputs["rendered_html"])
    assert called["to"] == "customer@example.com"
    assert called["cc"] == "ops@example.com;accounts@example.com"
    assert called["subject"] == "Ticket T-EMAIL-SEND-1"
    assert called["text_body"] == "Attached ticket T-EMAIL-SEND-1."

    attachments = called["attachments"]
    assert len(attachments) == 1
    attachment = attachments[0]
    assert attachment.filename == "Ticket-T-EMAIL-SEND-1.pdf"
    assert attachment.content_type == "application/pdf"
    assert attachment.content_bytes.startswith(b"%PDF")

    audit_event = _ticket_email_audit_event(
        db_session,
        action="TICKET_EMAIL_SENT",
        ticket_id=ticket.id,
    )

    assert audit_event is not None
    assert audit_event.details_json.get("ticket_no") == "T-EMAIL-SEND-1"
    assert audit_event.details_json.get("recipient") == "customer@example.com"
    assert audit_event.details_json.get("cc") == "ops@example.com;accounts@example.com"
    assert audit_event.details_json.get("subject") == "Ticket T-EMAIL-SEND-1"
    assert audit_event.details_json.get("status") == "sent"


def test_ticket_send_by_email_reports_missing_resend_config_and_writes_audit(
    client,
    db_session,
    monkeypatch,
):
    import app.routes.tickets as tickets_routes

    ticket = _create_complete_sale_ticket(
        db_session,
        ticket_no="T-EMAIL-ERR-1",
        invoice_email="billing@example.com",
    )
    _set_ticket_browser_destination(
        db_session,
        code="TICKET_EMAIL_ERR",
        template_format="HTML",
        content="<html><body>EMAIL {{ payload.ticket_no }}</body></html>",
    )
    pdf_called = {"value": False}

    def _unexpected_render_html_pdf_bytes(*args, **kwargs):
        pdf_called["value"] = True
        raise AssertionError("PDF rendering should not run")

    monkeypatch.setattr(
        tickets_routes,
        "render_html_pdf_bytes",
        _unexpected_render_html_pdf_bytes,
    )

    response = client.post(
        f"/tickets/{ticket.id}/email",
        data={"to_email": "customer@example.com"},
    )

    assert response.status_code == 400
    assert "Ticket email failed." in response.text
    assert "Resend API key is not configured." in response.text
    assert "Email Ticket" in response.text
    assert pdf_called["value"] is False

    audit_event = _ticket_email_audit_event(
        db_session,
        action="TICKET_EMAIL_FAILED",
        ticket_id=ticket.id,
    )

    assert audit_event is not None
    assert audit_event.details_json.get("error") == "Resend API key is not configured."
    assert audit_event.details_json.get("status") == "failed"


def test_ticket_receipt_route_returns_200_for_valid_ticket(client, db_session):
    ticket = Ticket(
        ticket_no="T-PRINT-RECEIPT-1",
        datetime=datetime(2026, 2, 19, 12, 30, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.get(f"/tickets/{ticket.id}/receipt")

    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    assert "Ticket Receipt" in response.text
    assert "T-PRINT-RECEIPT-1" in response.text
    assert "DRAFT" not in response.text


def test_ticket_receipt_open_ticket_displays_draft_indicator(client, db_session):
    ticket = Ticket(
        ticket_no="T-PRINT-RECEIPT-DRAFT-1",
        datetime=datetime(2026, 2, 19, 12, 45, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.get(f"/tickets/{ticket.id}/receipt")

    assert response.status_code == 200
    assert "DRAFT - PREVIEW ONLY" in response.text
    assert "draft-badge" in response.text


def test_ticket_receipt_contains_core_fields(client, db_session):
    customer = Customer(account_code="C-PRINT-RECEIPT", name="Receipt Customer")
    vehicle = Vehicle(registration="RCPT123")
    unit = Unit(name="Each", unit_type="COUNT", is_active=True)
    product = Product(
        code="P-PRINT-RECEIPT",
        description="Receipt Product",
        unit=unit,
        unit_price=Decimal("11.50"),
    )
    db_session.add_all([customer, vehicle, unit, product])
    db_session.flush()

    ticket = Ticket(
        ticket_no="T-PRINT-RECEIPT-2",
        datetime=datetime(2026, 2, 19, 13, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        customer_id=customer.id,
        vehicle_id=vehicle.id,
        product_id=product.id,
        gross_kg=Decimal("4000.000"),
        tare_kg=Decimal("1000.000"),
        net_kg=Decimal("3000.000"),
        qty=Decimal("2.000"),
        unit_price=Decimal("11.50"),
        total=Decimal("23.00"),
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.get(f"/tickets/{ticket.id}/receipt")

    assert response.status_code == 200
    assert "T-PRINT-RECEIPT-2" in response.text
    assert "19/02/2026 13:00" in response.text
    assert "Receipt Customer" in response.text
    assert "Receipt Product" in response.text
    assert "Gross" in response.text
    assert "Tare" in response.text
    assert "Net" in response.text
    assert "Qty" in response.text
    assert "Unit price" in response.text
    assert "Total" in response.text
    assert "23.00" in response.text


def test_ticket_receipt_route_creates_print_job_log_entry(client, db_session):
    ticket = Ticket(
        ticket_no="T-PRINT-RECEIPT-JOB-1",
        datetime=datetime(2026, 2, 19, 14, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.get(f"/tickets/{ticket.id}/receipt")
    assert response.status_code == 200

    job = (
        db_session.query(PrintJob)
        .filter(PrintJob.ticket_id == ticket.id, PrintJob.document_type == "TICKET")
        .order_by(PrintJob.id.desc())
        .first()
    )
    assert job is not None
    assert job.delivery_type == "PRINT_LOCAL_BROWSER"
    assert job.status == "SENT"


def test_ticket_direct_print_local_browser_route_remains_unchanged(client, db_session):
    ticket = _create_complete_sale_ticket(db_session, ticket_no="T-BROWSER-PRINT-1")
    _set_ticket_browser_destination(
        db_session,
        code="TICKET_BROWSER_PRINT_ROUTE",
        template_format="TEXT",
        content="Ticket {{ payload.ticket_no }}",
    )

    response = client.post(f"/tickets/{ticket.id}/print", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/tickets/{ticket.id}/preview?")
    job = (
        db_session.query(PrintJob)
        .filter(PrintJob.ticket_id == ticket.id, PrintJob.document_type == "TICKET")
        .order_by(PrintJob.id.desc())
        .first()
    )
    assert job is not None
    assert job.delivery_type == "PRINT_LOCAL_BROWSER"
    assert job.status == "SENT"
    assert job.trigger_source == "MANUAL"


def test_ticket_completion_does_not_auto_print_when_disabled(client, db_session):
    ticket = _create_open_sale_ticket(db_session, ticket_no="T-AUTO-DISABLED-1")
    _set_ticket_browser_destination(
        db_session,
        code="TICKET_AUTO_DISABLED",
        template_format="TEXT",
        content="Ticket {{ payload.ticket_no }}",
        auto_print_on_complete=False,
    )

    response = _complete_ticket(client, ticket)

    assert response.status_code == 303
    assert response.headers["location"] == f"/tickets/{ticket.id}?completed=1"
    jobs = (
        db_session.query(PrintJob)
        .filter(PrintJob.ticket_id == ticket.id, PrintJob.document_type == "TICKET")
        .all()
    )
    assert jobs == []


def test_ticket_completion_auto_prints_once_for_browser_destination(client, db_session):
    ticket = _create_open_sale_ticket(db_session, ticket_no="T-AUTO-BROWSER-1")
    _set_ticket_browser_destination(
        db_session,
        code="TICKET_AUTO_BROWSER",
        template_format="TEXT",
        content="Ticket {{ payload.ticket_no }}",
        auto_print_on_complete=True,
    )

    response = _complete_ticket(client, ticket)

    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/tickets/{ticket.id}/preview?")
    assert "completed=1" in response.headers["location"]
    assert "auto_printed=1" in response.headers["location"]
    jobs = (
        db_session.query(PrintJob)
        .filter(PrintJob.ticket_id == ticket.id, PrintJob.document_type == "TICKET")
        .order_by(PrintJob.id.asc())
        .all()
    )
    assert len(jobs) == 1
    assert jobs[0].delivery_type == "PRINT_LOCAL_BROWSER"
    assert jobs[0].status == "SENT"
    assert jobs[0].trigger_source == "AUTO_ON_COMPLETE"


def test_ticket_manual_print_still_works_with_auto_print_destination(client, db_session):
    ticket = _create_complete_sale_ticket(db_session, ticket_no="T-AUTO-MANUAL-1")
    _set_ticket_browser_destination(
        db_session,
        code="TICKET_AUTO_MANUAL",
        template_format="TEXT",
        content="Ticket {{ payload.ticket_no }}",
        auto_print_on_complete=True,
    )

    response = client.post(f"/tickets/{ticket.id}/print", follow_redirects=False)

    assert response.status_code == 303
    job = (
        db_session.query(PrintJob)
        .filter(PrintJob.ticket_id == ticket.id, PrintJob.document_type == "TICKET")
        .order_by(PrintJob.id.desc())
        .first()
    )
    assert job is not None
    assert job.trigger_source == "MANUAL"
    assert job.status == "SENT"


def test_ticket_non_completion_save_does_not_create_duplicate_auto_print_job(
    client,
    db_session,
):
    ticket = _create_open_sale_ticket(db_session, ticket_no="T-AUTO-SAVE-1")
    _set_ticket_browser_destination(
        db_session,
        code="TICKET_AUTO_SAVE",
        template_format="TEXT",
        content="Ticket {{ payload.ticket_no }}",
        auto_print_on_complete=True,
    )

    response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "save",
            "datetime": ticket.datetime.strftime("%Y-%m-%dT%H:%M"),
            "direction": "INWARD",
            "transaction_type": "SALE",
            "customer_id": str(ticket.customer_id or ""),
            "product_id": str(ticket.product_id or ""),
            "qty": "1",
            "unit_price": "",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    jobs = (
        db_session.query(PrintJob)
        .filter(PrintJob.ticket_id == ticket.id, PrintJob.document_type == "TICKET")
        .all()
    )
    assert jobs == []


def test_ticket_completion_auto_prints_using_pull_pipeline(client, db_session):
    agent = PrintAgent(
        id="agent-auto-pull-1",
        name="Auto Pull Agent",
        api_key=hash_print_agent_key("agent-auto-pull-key"),
        status="OFFLINE",
    )
    db_session.add(agent)
    db_session.commit()

    ticket = _create_open_sale_ticket(db_session, ticket_no="T-AUTO-PULL-1")
    _set_ticket_pull_destination(
        db_session,
        code="TICKET_AUTO_PULL",
        content="PULL {{ payload.ticket_no }}",
        agent_id=agent.id,
        printer_name="Yard Pull Printer",
        copies=2,
        auto_print_on_complete=True,
    )

    response = _complete_ticket(client, ticket)

    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/tickets/{ticket.id}?")
    assert "completed=1" in response.headers["location"]
    assert "auto_printed=1" in response.headers["location"]
    job = (
        db_session.query(PrintJob)
        .filter(PrintJob.ticket_id == ticket.id, PrintJob.document_type == "TICKET")
        .order_by(PrintJob.id.desc())
        .first()
    )
    assert job is not None
    assert job.delivery_type == "PRINT_AGENT_PULL"
    assert job.status == "PENDING"
    assert job.trigger_source == "AUTO_ON_COMPLETE"
    assert job.payload_format == "PDF"
    assert job.payload_mime_type == "application/pdf"
    assert base64.b64decode(job.rendered_bytes_base64.encode("ascii")).startswith(b"%PDF")
    assert job.delivery_config_json["printer_name"] == "Yard Pull Printer"
    assert job.delivery_config_json["copies"] == 2


def test_ticket_print_last_again_creates_replayed_job_for_current_user(
    client,
    db_session,
):
    ticket = _create_complete_sale_ticket(db_session, ticket_no="T-REPLAY-USER-1")
    _set_ticket_browser_destination(
        db_session,
        code="TICKET_REPLAY_USER",
        template_format="TEXT",
        content="Ticket {{ payload.ticket_no }}",
    )

    first_response = client.post(f"/tickets/{ticket.id}/print", follow_redirects=False)
    assert first_response.status_code == 303

    second_response = client.post("/tickets/print-last", follow_redirects=False)
    assert second_response.status_code == 303

    jobs = (
        db_session.query(PrintJob)
        .filter(PrintJob.ticket_id == ticket.id, PrintJob.document_type == "TICKET")
        .order_by(PrintJob.id.asc())
        .all()
    )
    current_user_id = db_session.execute(
        select(User.id).order_by(User.id.asc()).limit(1)
    ).scalar_one()
    assert len(jobs) == 2
    assert jobs[-1].id != jobs[0].id
    assert jobs[-1].created_by_user_id == current_user_id
