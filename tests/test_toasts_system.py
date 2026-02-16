from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from app.models import (
    Customer,
    DirectionEnum,
    Invoice,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
)


def test_base_layout_includes_toasts_script(client):
    response = client.get("/invoices")

    assert response.status_code == 200
    assert '<script src="/static/js/toasts.js" defer></script>' in response.text


def test_invoice_flash_toasts_render_outside_main_container(client, db_session):
    customer = Customer(account_code="C-TOAST-1", name="Toast Customer")
    db_session.add(customer)
    db_session.flush()
    invoice = Invoice(
        invoice_no="INV-TOAST-1",
        customer_id=customer.id,
        invoice_date=date(2026, 2, 14),
        status="DRAFT",
        net_total=Decimal("0.00"),
        vat_total=Decimal("0.00"),
        gross_total=Decimal("0.00"),
    )
    db_session.add(invoice)
    db_session.commit()

    response = client.get(f"/invoices/{invoice.id}?created=1")

    assert response.status_code == 200
    assert 'id="flash-toasts"' in response.text
    assert '<script src="/static/js/toasts.js" defer></script>' in response.text
    assert response.text.index('id="flash-toasts"') < response.text.index(
        '<main class="container">'
    )


def test_non_invoice_page_renders_without_flash_toasts_container(client, db_session):
    ticket = Ticket(
        ticket_no="T-TOAST-1",
        datetime=datetime(2026, 2, 14, 9, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.get(f"/tickets/{ticket.id}")

    assert response.status_code == 200
    assert 'id="flash-toasts"' not in response.text
    assert '<script src="/static/js/toasts.js" defer></script>' in response.text


def test_customers_saved_flag_renders_toast_container_outside_main(client, db_session):
    customer = Customer(account_code="C-TOAST-2", name="Toast Customer 2")
    db_session.add(customer)
    db_session.commit()

    response = client.get("/customers?saved=1")

    assert response.status_code == 200
    assert 'id="flash-toasts"' in response.text
    assert 'class="flash-toast flash-toast--success"' in response.text
    assert 'data-flash-success="1"' in response.text
    assert response.text.index('id="flash-toasts"') < response.text.index(
        '<main class="container">'
    )


def test_toasts_script_clears_success_query_flags():
    script = Path("app/static/js/toasts.js").read_text(encoding="utf-8")

    assert "history.replaceState" in script
    assert "SUCCESS_QUERY_FLAGS" in script
    assert '"saved"' in script
    assert '"created"' in script
    assert '"completed"' in script
    assert '"paid"' in script
    assert '"voided"' in script
