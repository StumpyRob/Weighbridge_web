from datetime import date
from decimal import Decimal

from app.models import Customer, CustomerAdjustment, Invoice


def _make_customer(
    db_session,
    *,
    account_code: str,
    name: str,
    credit_limit_pence: int | None,
) -> Customer:
    customer = Customer(
        account_code=account_code,
        name=name,
        credit_limit_pence=credit_limit_pence,
    )
    db_session.add(customer)
    db_session.commit()
    return customer


def _make_invoice(
    db_session,
    *,
    customer_id: int,
    invoice_no: str,
    gross_total: Decimal,
    status: str = "OPEN",
) -> Invoice:
    invoice = Invoice(
        invoice_no=invoice_no,
        customer_id=customer_id,
        invoice_date=date(2026, 2, 26),
        status=status,
        net_total=gross_total,
        vat_total=Decimal("0.00"),
        gross_total=gross_total,
    )
    db_session.add(invoice)
    db_session.commit()
    return invoice


def test_customer_credit_summary_shows_limit_outstanding_available_with_open_invoices(
    client, db_session
):
    customer = _make_customer(
        db_session,
        account_code="C-CS-OPEN-1",
        name="Credit Summary Open",
        credit_limit_pence=10000,
    )
    _make_invoice(
        db_session,
        customer_id=customer.id,
        invoice_no="INV-CS-OPEN-1",
        gross_total=Decimal("75.00"),
        status="OPEN",
    )

    response = client.get(f"/customers/{customer.id}")

    assert response.status_code == 200
    assert "Credit Summary" in response.text
    assert "&pound;100.00" in response.text
    assert "&pound;75.00" in response.text
    assert "&pound;25.00" in response.text
    assert "Owes &pound;75.00" in response.text
    assert f'href="/invoices?customer_id={customer.id}&unpaid_only=1"' in response.text
    assert 'href="#customer-adjustments-section"' in response.text


def test_customer_credit_summary_shows_in_credit_when_adjustments_make_negative_balance(
    client, db_session
):
    customer = _make_customer(
        db_session,
        account_code="C-CS-CREDIT-1",
        name="Credit Summary In Credit",
        credit_limit_pence=10000,
    )
    db_session.add(
        CustomerAdjustment(
            customer_id=customer.id,
            amount_decimal=Decimal("-30.00"),
            reason="GOODWILL_CREDIT",
            note="Goodwill credit.",
            created_by_user_id=None,
        )
    )
    db_session.commit()

    response = client.get(f"/customers/{customer.id}")

    assert response.status_code == 200
    assert "-&pound;30.00" in response.text
    assert "In credit &pound;30.00" in response.text
    assert "&pound;130.00" in response.text


def test_customer_credit_summary_shows_dash_available_when_no_credit_limit(
    client, db_session
):
    customer = _make_customer(
        db_session,
        account_code="C-CS-NOLIMIT-1",
        name="Credit Summary No Limit",
        credit_limit_pence=None,
    )
    _make_invoice(
        db_session,
        customer_id=customer.id,
        invoice_no="INV-CS-NOLIMIT-1",
        gross_total=Decimal("20.00"),
        status="OPEN",
    )

    response = client.get(f"/customers/{customer.id}")

    assert response.status_code == 200
    assert "Credit Summary" in response.text
    assert "&mdash;" in response.text
    assert "Owes &pound;20.00" in response.text


def test_invoices_list_supports_customer_unpaid_filter(client, db_session):
    customer = _make_customer(
        db_session,
        account_code="C-CS-FILTER-1",
        name="Credit Summary Invoice Filter",
        credit_limit_pence=10000,
    )
    other_customer = _make_customer(
        db_session,
        account_code="C-CS-FILTER-2",
        name="Credit Summary Invoice Filter Other",
        credit_limit_pence=10000,
    )
    open_invoice = _make_invoice(
        db_session,
        customer_id=customer.id,
        invoice_no="INV-CS-FILTER-OPEN-1",
        gross_total=Decimal("15.00"),
        status="OPEN",
    )
    _make_invoice(
        db_session,
        customer_id=customer.id,
        invoice_no="INV-CS-FILTER-PAID-1",
        gross_total=Decimal("20.00"),
        status="PAID",
    )
    _make_invoice(
        db_session,
        customer_id=other_customer.id,
        invoice_no="INV-CS-FILTER-OPEN-OTHER-1",
        gross_total=Decimal("25.00"),
        status="OPEN",
    )

    response = client.get(f"/invoices?customer_id={customer.id}&unpaid_only=1")

    assert response.status_code == 200
    assert open_invoice.invoice_no in response.text
    assert "INV-CS-FILTER-PAID-1" not in response.text
    assert "INV-CS-FILTER-OPEN-OTHER-1" not in response.text
