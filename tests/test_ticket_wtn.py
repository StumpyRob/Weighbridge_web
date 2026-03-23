from datetime import datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

from sqlalchemy import delete, select

import app.routes.tickets as tickets_routes
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

SIGNATURE_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAABCAYAAAD0In+KAAAAD0lEQVR4nGP4DwQMDAz/ARruBPywhCTXAAAAAElFTkSuQmCC"
)
BLANK_SIGNATURE_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAABCAYAAAD0In+KAAAAC0lEQVR4nGP4DwUAI+UH+Yo0eLMAAAAASUVORK5CYII="
)


def _create_wtn_template_and_destination(db_session) -> tuple[PrintTemplate, PrintDestination]:
    template = PrintTemplate(
        code="WTN_TEST_TEMPLATE",
        description="WTN test template",
        document_type="WTN",
        format="HTML",
        content=(
            "<html><body>WTN_PREVIEW {{ payload.wtn_no }} "
            "{{ payload.wtn_signature_data_uri|default('', true) }}</body></html>"
        ),
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
        final_disposal=True,
        used_on_site=False,
        ewc_code_display="17 09 04",
        ewc_description="Mixed construction waste",
        net_kg=Decimal("12340.000"),
        waste_producer_name="WTN Producer",
        waste_producer_address="WTN Producer Address",
        wtn_signature_data_uri=SIGNATURE_DATA_URL,
        wtn_signature_signed_at=datetime(2026, 2, 20, 9, 20, 0),
        wtn_signature_signer_name="Inspector",
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
    assert payload["final_disposal"] is True
    assert payload["used_on_site"] is False
    assert payload["wtn_signature_data_uri"] == SIGNATURE_DATA_URL
    assert payload["wtn_signature_signed_at"] == "20/02/2026 09:20"
    assert payload["wtn_signature_signer_name"] == "Inspector"
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
    signed_waste_ticket = Ticket(
        ticket_no="T-WTN-UI-WASTE-SIGNED",
        datetime=datetime(2026, 2, 20, 10, 47, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        ewc_code_display="17 09 04",
        ewc_description="Waste UI signed",
        net_kg=Decimal("2100.000"),
        wtn_signature_data_uri=SIGNATURE_DATA_URL,
        wtn_signature_signed_at=datetime(2026, 2, 20, 10, 47, 30),
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([waste_ticket, sale_ticket, signed_waste_ticket])
    db_session.commit()

    waste_response = client.get(f"/tickets/{waste_ticket.id}")
    sale_response = client.get(f"/tickets/{sale_ticket.id}")
    signed_waste_response = client.get(f"/tickets/{signed_waste_ticket.id}")

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
    assert "wtn-signature-tools" in waste_response.text
    assert "WTN Signature" in waste_response.text
    assert "Not signed" in waste_response.text
    assert "⚠ WTN not signed" in waste_response.text
    assert (
        waste_response.text.index("This ticket is complete and cannot be edited.")
        < waste_response.text.index("⚠ WTN not signed")
        < waste_response.text.index("wtn-signature-tools")
        < waste_response.text.index("Ticket Info")
    )
    assert waste_response.text.index("page-header__aside--documents") < waste_response.text.index(
        "wtn-signature-tools"
    )

    assert sale_response.status_code == 200
    assert "Documents" in sale_response.text
    assert "documents-panel--header" in sale_response.text
    assert "Waste Transfer Note" not in sale_response.text
    assert "wtn-signature-tools" not in sale_response.text
    assert "⚠ WTN not signed" not in sale_response.text

    assert signed_waste_response.status_code == 200
    assert "wtn-signature-tools" in signed_waste_response.text
    assert "WTN Signature" in signed_waste_response.text
    assert "Signed" in signed_waste_response.text
    assert "⚠ WTN not signed" not in signed_waste_response.text


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


def test_wtn_signature_save_persists_signature_data(client, db_session):
    _create_wtn_template_and_destination(db_session)

    ticket = Ticket(
        ticket_no="T-WTN-SIGN-1",
        datetime=datetime(2026, 3, 22, 11, 10, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        ewc_code_display="17 09 04",
        ewc_description="Waste signed",
        net_kg=Decimal("1500.000"),
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}/wtn/signature",
        data={
            "signature_data_url": SIGNATURE_DATA_URL,
            "blank_signature_data_url": BLANK_SIGNATURE_DATA_URL,
            "signer_name": "John Smith",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/tickets/{ticket.id}?wtn_signature_saved=1"

    db_session.refresh(ticket)
    assert ticket.wtn_signature_data_uri == SIGNATURE_DATA_URL
    assert ticket.wtn_signature_signed_at is not None
    assert ticket.wtn_signature_signer_name == "John Smith"


def test_wtn_signature_save_rejects_blank_signature(client, db_session):
    _create_wtn_template_and_destination(db_session)

    ticket = Ticket(
        ticket_no="T-WTN-SIGN-BLANK",
        datetime=datetime(2026, 3, 22, 11, 20, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        ewc_code_display="17 09 04",
        ewc_description="Waste unsigned",
        net_kg=Decimal("1600.000"),
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}/wtn/signature",
        data={
            "signature_data_url": BLANK_SIGNATURE_DATA_URL,
            "blank_signature_data_url": SIGNATURE_DATA_URL,
            "signer_name": "John Smith",
        },
    )

    assert response.status_code == 400
    assert "Signature cannot be blank." in response.text

    db_session.refresh(ticket)
    assert ticket.wtn_signature_data_uri in (None, "")
    assert ticket.wtn_signature_signed_at is None


def test_wtn_preview_route_includes_saved_signature(client, db_session):
    _create_wtn_template_and_destination(db_session)

    ticket = Ticket(
        ticket_no="T-WTN-SIGN-PREVIEW",
        datetime=datetime(2026, 3, 22, 11, 25, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        ewc_code_display="17 09 04",
        ewc_description="Waste preview signature",
        net_kg=Decimal("1700.000"),
        wtn_signature_data_uri=SIGNATURE_DATA_URL,
        wtn_signature_signed_at=datetime(2026, 3, 22, 11, 24, 0),
        wtn_signature_signer_name="Alex",
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.get(f"/tickets/{ticket.id}/wtn/preview")

    assert response.status_code == 200
    assert SIGNATURE_DATA_URL in response.text


def test_wtn_pdf_route_includes_saved_signature(client, db_session, monkeypatch):
    _create_wtn_template_and_destination(db_session)

    ticket = Ticket(
        ticket_no="T-WTN-SIGN-PDF",
        datetime=datetime(2026, 3, 22, 11, 30, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        ewc_code_display="17 09 04",
        ewc_description="Waste PDF signature",
        net_kg=Decimal("1800.000"),
        wtn_signature_data_uri=SIGNATURE_DATA_URL,
        wtn_signature_signed_at=datetime(2026, 3, 22, 11, 29, 0),
        wtn_signature_signer_name="Alex",
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    captured: dict[str, str] = {}

    def _fake_render_html_pdf_bytes(rendered_html, **kwargs):
        captured["html"] = str(rendered_html)
        return b"%PDF-1.4\n%wtn-signature\n"

    monkeypatch.setattr(
        tickets_routes,
        "render_html_pdf_bytes",
        _fake_render_html_pdf_bytes,
    )

    response = client.get(f"/tickets/{ticket.id}/wtn/pdf")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/pdf")
    assert SIGNATURE_DATA_URL in captured.get("html", "")
