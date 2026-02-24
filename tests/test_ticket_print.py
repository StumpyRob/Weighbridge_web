from datetime import datetime
from decimal import Decimal

from app.config import settings
from app.models import (
    CompanySetting,
    Container,
    Customer,
    Destination,
    DirectionEnum,
    Driver,
    Haulier,
    PrintDestination,
    PrintJob,
    PrintTemplate,
    Product,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
    Unit,
    Vehicle,
)
from app.services.print_payload import build_ticket_print_payload


def _set_ticket_browser_destination(
    db_session,
    *,
    code: str,
    template_format: str,
    content: str,
) -> None:
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
        delivery_config={},
        is_default=True,
        is_active=True,
    )
    db_session.add(destination)
    db_session.commit()


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
    assert "logo_data_uri" in payload


def test_build_ticket_print_payload_uses_company_uploaded_logo(
    db_session,
    monkeypatch,
    tmp_path,
):
    upload_dir = tmp_path / "uploads" / "company"
    upload_dir.mkdir(parents=True, exist_ok=True)
    logo_file = upload_dir / "ticket-logo.png"
    logo_file.write_bytes(b"\x89PNG\r\n\x1a\nlogo-bytes")
    monkeypatch.setattr(settings, "company_logo_upload_dir", str(upload_dir))

    db_session.add(
        CompanySetting(
            name="Ticket Logo Co",
            company_logo_path="/static/uploads/company/ticket-logo.png",
        )
    )
    db_session.flush()

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
