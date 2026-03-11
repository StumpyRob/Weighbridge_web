from datetime import datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

from sqlalchemy import delete, select

from app.models import (
    Customer,
    Destination,
    DirectionEnum,
    Haulier,
    PrintDestination,
    PrintJob,
    PrintTemplate,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
    Yard,
)
from app.services.print_payload import build_wtn_payload


def _create_wtn_template_and_destination(db_session) -> tuple[PrintTemplate, PrintDestination]:
    template = PrintTemplate(
        code="WTN_TEST_TEMPLATE",
        description="WTN test template",
        document_type="WTN",
        format="HTML",
        content="<html><body>WTN_PREVIEW {{ payload.wtn_no }}</body></html>",
        is_active=True,
    )
    db_session.add(template)
    db_session.flush()

    destination = PrintDestination(
        name="WTN Test Destination",
        description="WTN test destination",
        document_type="WTN",
        template_id=template.id,
        delivery_type="PRINT_LOCAL_BROWSER",
        delivery_config={},
        is_default=True,
        is_active=True,
    )
    db_session.add(destination)
    db_session.commit()
    db_session.refresh(template)
    db_session.refresh(destination)
    return template, destination


def _create_wtn_default_template_only(db_session) -> PrintTemplate:
    template = PrintTemplate(
        code="WTN_SYSTEM",
        description="Waste Transfer Note (System)",
        document_type="WTN",
        format="HTML",
        content="<html><body>WTN_SYSTEM_PREVIEW {{ payload.wtn_no }}</body></html>",
        is_system=True,
        is_active=True,
    )
    db_session.add(template)
    db_session.commit()
    db_session.refresh(template)
    return template


def test_build_wtn_payload_returns_expected_keys(db_session):
    customer = Customer(
        account_code="WTN-CUST-1",
        name="WTN Customer",
        address_line1="1 Sample Street",
        city="Leeds",
        postcode="LS1 1AA",
    )
    haulier = Haulier(name="WTN Haulier", carrier_licence_number="CBDU123456")
    destination = Destination(name="WTN Destination")
    yard = Yard(code="Y1", description="Main Yard", is_active=True)
    db_session.add_all([customer, haulier, destination, yard])
    db_session.flush()

    ticket = Ticket(
        ticket_no="T-WTN-100",
        datetime=datetime(2026, 2, 20, 9, 15, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        customer_id=customer.id,
        haulier_id=haulier.id,
        destination_id=destination.id,
        yard_id=yard.id,
        ewc_code_display="17 09 04",
        ewc_description="Mixed construction waste",
        net_kg=Decimal("12340.000"),
        waste_producer_name="WTN Producer",
        waste_producer_address="WTN Producer Address",
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    payload = build_wtn_payload(db_session, ticket.id)

    assert payload["wtn_no"] == "WTN-T-WTN-100"
    assert payload["customer_name"] == "WTN Customer"
    assert payload["producer"] == "WTN Producer"
    assert payload["carrier_name"] == "WTN Haulier"
    assert payload["ewc_code"] == "17 09 04"
    assert payload["waste_description"] == "Mixed construction waste"
    assert payload["quantity_net_kg"] == 12340.0
    assert payload["quantity_tonnes"] == 12.34
    assert payload["origin_site"] == "Main Yard"
    assert payload["destination_site"] == "WTN Destination"
    assert payload["send_ready"] is True
    assert payload["send_blockers"] == []


def test_wtn_preview_route_returns_200_for_waste_ticket(client, db_session):
    _create_wtn_template_and_destination(db_session)

    ticket = Ticket(
        ticket_no="T-WTN-PREVIEW-1",
        datetime=datetime(2026, 2, 20, 10, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        ewc_code_display="17 09 04",
        ewc_description="Waste preview",
        net_kg=Decimal("1000.000"),
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.get(f"/tickets/{ticket.id}/wtn/preview")

    assert response.status_code == 200
    assert "WTN_PREVIEW WTN-T-WTN-PREVIEW-1" in response.text
    assert "WTN Preview T-WTN-PREVIEW-1" in response.text


def test_wtn_send_blocks_if_required_fields_missing(client, db_session):
    _create_wtn_template_and_destination(db_session)

    ticket = Ticket(
        ticket_no="T-WTN-BLOCK-1",
        datetime=datetime(2026, 2, 20, 10, 30, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.post(f"/tickets/{ticket.id}/wtn/send")

    assert response.status_code == 400
    assert "Cannot send WTN. Missing required fields:" in response.text
    assert "EWC code" in response.text
    assert "Customer" in response.text
    assert "Carrier/haulier" in response.text


def test_wtn_preview_works_without_default_destination(client, db_session):
    _create_wtn_default_template_only(db_session)

    ticket = Ticket(
        ticket_no="T-WTN-NO-DEST-PREVIEW",
        datetime=datetime(2026, 2, 20, 10, 35, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        ewc_code_display="17 09 04",
        ewc_description="Waste no destination preview",
        net_kg=Decimal("1200.000"),
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.get(f"/tickets/{ticket.id}/wtn/preview")

    assert response.status_code == 200
    assert "WTN_SYSTEM_PREVIEW WTN-T-WTN-NO-DEST-PREVIEW" in response.text
    assert "Send WTN is not set up yet." in response.text


def test_wtn_send_requires_default_destination_even_when_compliant(client, db_session):
    customer = Customer(account_code="WTN-CUST-NODEST", name="WTN No Dest Customer")
    haulier = Haulier(name="WTN No Dest Haulier", carrier_licence_number="CBDU222222")
    dest = Destination(name="WTN No Dest Destination")
    db_session.add_all([customer, haulier, dest])
    db_session.flush()

    ticket = Ticket(
        ticket_no="T-WTN-NO-DEST-SEND",
        datetime=datetime(2026, 2, 20, 10, 40, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        customer_id=customer.id,
        haulier_id=haulier.id,
        destination_id=dest.id,
        ewc_code_display="17 09 04",
        ewc_description="Waste no destination send",
        net_kg=Decimal("3000.000"),
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    db_session.execute(delete(PrintDestination))
    db_session.commit()

    response = client.post(f"/tickets/{ticket.id}/wtn/send", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/tickets/{ticket.id}?")
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query.get("wtn_failed", [""])[0] == "1"
    assert "Sending is not set up yet. Ask an admin." in query.get(
        "wtn_error_detail", [""]
    )[0]

    job = db_session.execute(
        select(PrintJob)
        .where(
            PrintJob.ticket_id == ticket.id,
            PrintJob.document_type == "WTN",
        )
        .order_by(PrintJob.id.desc())
    ).scalars().first()
    assert job is None


def test_ticket_edit_shows_wtn_buttons_only_for_complete_waste(client, db_session):
    _create_wtn_template_and_destination(db_session)

    waste_ticket = Ticket(
        ticket_no="T-WTN-UI-WASTE",
        datetime=datetime(2026, 2, 20, 10, 45, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        ewc_code_display="17 09 04",
        ewc_description="Waste UI",
        net_kg=Decimal("2000.000"),
        dont_invoice=False,
        paid=False,
    )
    sale_ticket = Ticket(
        ticket_no="T-WTN-UI-SALE",
        datetime=datetime(2026, 2, 20, 10, 46, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([waste_ticket, sale_ticket])
    db_session.commit()

    waste_response = client.get(f"/tickets/{waste_ticket.id}")
    sale_response = client.get(f"/tickets/{sale_ticket.id}")

    assert waste_response.status_code == 200
    assert "Documents" in waste_response.text
    assert "documents-panel" in waste_response.text
    assert "documents-panel--header" in waste_response.text
    assert "page-header__aside--documents" in waste_response.text
    assert "ticket-header-actions" not in waste_response.text
    assert "Waste Transfer Note" in waste_response.text
    assert "Download PDF" in waste_response.text
    assert "Preview WTN" not in waste_response.text
    assert "Print locally (browser)" not in waste_response.text
    assert (
        "Browser printing may add URL/date/time headers/footers depending on your browser settings."
        not in waste_response.text
    )
    assert waste_response.text.count("Preview") >= 2
    assert waste_response.text.count("Print") >= 2

    assert sale_response.status_code == 200
    assert "Documents" in sale_response.text
    assert "documents-panel--header" in sale_response.text
    assert "Waste Transfer Note" not in sale_response.text


def test_wtn_send_succeeds_and_creates_job_when_compliant(client, db_session):
    _, destination = _create_wtn_template_and_destination(db_session)

    customer = Customer(account_code="WTN-CUST-2", name="WTN Send Customer")
    haulier = Haulier(name="WTN Send Haulier", carrier_licence_number="CBDU654321")
    dest = Destination(name="WTN Send Destination")
    db_session.add_all([customer, haulier, dest])
    db_session.flush()

    ticket = Ticket(
        ticket_no="T-WTN-SEND-1",
        datetime=datetime(2026, 2, 20, 11, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        customer_id=customer.id,
        haulier_id=haulier.id,
        destination_id=dest.id,
        ewc_code_display="17 09 04",
        ewc_description="Waste send",
        net_kg=Decimal("2500.000"),
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.post(f"/tickets/{ticket.id}/wtn/send", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/tickets/{ticket.id}/wtn/preview?")

    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query.get("wtn_sent", [""])[0] == "1"

    job = db_session.execute(
        select(PrintJob)
        .where(
            PrintJob.ticket_id == ticket.id,
            PrintJob.document_type == "WTN",
        )
        .order_by(PrintJob.id.desc())
    ).scalars().first()

    assert job is not None
    assert job.status == "SENT"
    assert job.destination_id == destination.id
