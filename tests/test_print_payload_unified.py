from datetime import date, datetime
from decimal import Decimal

import pytest

from app.models import (
    Customer,
    DirectionEnum,
    Invoice,
    InvoiceLine,
    Product,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
    Unit,
)
from app.services.print_payload import (
    PRINT_PAYLOAD_KEYS,
    build_print_payload,
    print_payload_variable_docs,
)
from app.services.printing import (
    DOCUMENT_TYPE_INVOICE,
    DOCUMENT_TYPE_TICKET,
    DOCUMENT_TYPE_WTN,
)


@pytest.mark.parametrize(
    "document_type",
    [DOCUMENT_TYPE_TICKET, DOCUMENT_TYPE_INVOICE, DOCUMENT_TYPE_WTN],
)
def test_unified_payload_sample_contains_full_key_set(document_type: str):
    payload = build_print_payload(None, document_type, source_id=None)
    assert set(PRINT_PAYLOAD_KEYS).issubset(set(payload.keys()))
    assert payload["document_type"] == document_type


def test_unified_payload_ticket_invoice_wtn_sources(db_session):
    customer = Customer(account_code="C-PAYLOAD", name="Payload Customer")
    unit = Unit(name="Each", unit_type="COUNT", is_active=True)
    product = Product(
        code="P-PAYLOAD",
        description="Payload Product",
        unit=unit,
        unit_price=Decimal("10.00"),
    )
    db_session.add_all([customer, unit, product])
    db_session.flush()

    ticket_sale = Ticket(
        ticket_no="T-PAYLOAD-SALE",
        datetime=datetime(2026, 2, 24, 9, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        customer_id=customer.id,
        product_id=product.id,
        final_disposal=False,
        used_on_site=True,
        qty=Decimal("2.000"),
        unit_price=Decimal("10.00"),
        total=Decimal("20.00"),
        gross_kg=Decimal("3000.000"),
        tare_kg=Decimal("500.000"),
        net_kg=Decimal("2500.000"),
        dont_invoice=False,
        paid=False,
    )
    ticket_waste = Ticket(
        ticket_no="T-PAYLOAD-WASTE",
        datetime=datetime(2026, 2, 24, 9, 30, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        customer_id=customer.id,
        product_id=product.id,
        final_disposal=True,
        used_on_site=False,
        ewc_code_display="17 09 04",
        ewc_description="Mixed construction waste",
        gross_kg=Decimal("4000.000"),
        tare_kg=Decimal("1000.000"),
        net_kg=Decimal("3000.000"),
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([ticket_sale, ticket_waste])
    db_session.flush()

    invoice = Invoice(
        invoice_no="INV-PAYLOAD-1",
        customer_id=customer.id,
        invoice_date=date(2026, 2, 24),
        due_date=date(2026, 3, 24),
        status="DRAFT",
        net_total=Decimal("20.00"),
        vat_total=Decimal("4.00"),
        gross_total=Decimal("24.00"),
    )
    db_session.add(invoice)
    db_session.flush()
    db_session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            ticket_id=ticket_sale.id,
            description="Payload line",
            quantity=Decimal("2.000"),
            unit_price=Decimal("10.00"),
            net=Decimal("20.00"),
            vat=Decimal("4.00"),
            gross=Decimal("24.00"),
        )
    )
    db_session.commit()

    ticket_payload = build_print_payload(
        db_session, DOCUMENT_TYPE_TICKET, source_id=ticket_sale.id
    )
    invoice_payload = build_print_payload(
        db_session, DOCUMENT_TYPE_INVOICE, source_id=invoice.id
    )
    wtn_payload = build_print_payload(db_session, DOCUMENT_TYPE_WTN, source_id=ticket_waste.id)

    assert ticket_payload["ticket_no"] == "T-PAYLOAD-SALE"
    assert ticket_payload["total"] == 20.0
    assert ticket_payload["final_disposal"] is False
    assert ticket_payload["used_on_site"] is True
    assert invoice_payload["invoice_no"] == "INV-PAYLOAD-1"
    assert invoice_payload["gross_total"] == 24.0
    assert isinstance(invoice_payload["line_items"], list)
    assert wtn_payload["wtn_no"].startswith("WTN-T-PAYLOAD-WASTE")
    assert wtn_payload["final_disposal"] is True
    assert wtn_payload["used_on_site"] is False
    assert isinstance(wtn_payload["send_ready"], bool)
    assert isinstance(wtn_payload["send_blockers"], list)


def test_template_variables_page_lists_payload_variables(client, db_session):
    _ = db_session
    response = client.get("/admin/help/template-variables")
    assert response.status_code == 200
    assert "Template Variables" in response.text
    assert "payload.document_type" in response.text
    assert "payload.vehicle_reg" in response.text


def test_print_payload_variable_docs_prefer_role_based_wtn_signature_fields():
    rows = {row["name"]: row for row in print_payload_variable_docs()}

    assert rows["payload.producer_signature_data_uri"]["example"] == "data:image/png;base64,..."
    assert rows["payload.producer_signature_data_uri"]["description"].startswith(
        "Preferred WTN variable:"
    )
    assert rows["payload.carrier_signature_signed_at_iso"]["example"] == "2026-02-01T10:16:00"
    assert rows["payload.receiver_signature_signer_name"]["example"] == "Sample Receiver Signer"
    assert rows["payload.wtn_signature_data_uri"]["description"].startswith(
        "Legacy receiver alias"
    )
    assert rows["payload.wtn_signature_signed_at"]["example"] == "01/02/2026 10:17"
