import re
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.models import Customer, Invoice, PaymentMethod
from app.seed import seed_payment_methods


def _make_invoice(db_session, *, status: str = "DRAFT") -> Invoice:
    customer = Customer(account_code=f"C-PAID-{status}", name=f"Paid Flow {status}")
    db_session.add(customer)
    db_session.flush()
    invoice = Invoice(
        invoice_no=f"INV-PAID-{status}",
        customer_id=customer.id,
        invoice_date=date(2026, 1, 10),
        status=status,
        net_total=Decimal("0.00"),
        vat_total=Decimal("0.00"),
        gross_total=Decimal("0.00"),
    )
    db_session.add(invoice)
    db_session.commit()
    return invoice


def test_invoice_detail_shows_payment_method_options(client, db_session):
    seed_payment_methods(db_session)
    invoice = _make_invoice(db_session)

    response = client.get(f"/invoices/{invoice.id}")

    assert response.status_code == 200
    assert "BACS" in response.text


def test_mark_paid_succeeds_with_valid_method_and_datetime(client, db_session):
    seed_payment_methods(db_session)
    payment_method = db_session.execute(
        select(PaymentMethod).where(PaymentMethod.is_active.is_(True)).limit(1)
    ).scalar_one()
    invoice = _make_invoice(db_session)

    response = client.post(
        f"/invoices/{invoice.id}/paid",
        data={"payment_method_id": str(payment_method.id), "paid_at": "2026-01-11T14:30"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith(f"/invoices/{invoice.id}?paid=1")
    db_session.refresh(invoice)
    assert invoice.status == "PAID"
    assert invoice.payment_method_id == payment_method.id
    assert invoice.paid_at is not None


def test_mark_paid_succeeds_with_valid_method_and_iso_date_only(client, db_session):
    seed_payment_methods(db_session)
    payment_method = db_session.execute(
        select(PaymentMethod).where(PaymentMethod.is_active.is_(True)).limit(1)
    ).scalar_one()
    invoice = _make_invoice(db_session)

    response = client.post(
        f"/invoices/{invoice.id}/paid",
        data={"payment_method_id": str(payment_method.id), "paid_at": "2026-01-11"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith(f"/invoices/{invoice.id}?paid=1")
    db_session.refresh(invoice)
    assert invoice.status == "PAID"
    assert invoice.payment_method_id == payment_method.id
    assert invoice.paid_at is not None
    assert invoice.paid_at.date() == date(2026, 1, 11)


def test_mark_paid_succeeds_with_valid_method_and_uk_date_only(client, db_session):
    seed_payment_methods(db_session)
    payment_method = db_session.execute(
        select(PaymentMethod).where(PaymentMethod.is_active.is_(True)).limit(1)
    ).scalar_one()
    invoice = _make_invoice(db_session)

    response = client.post(
        f"/invoices/{invoice.id}/paid",
        data={"payment_method_id": str(payment_method.id), "paid_at": "11/01/2026"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith(f"/invoices/{invoice.id}?paid=1")
    db_session.refresh(invoice)
    assert invoice.status == "PAID"
    assert invoice.payment_method_id == payment_method.id
    assert invoice.paid_at is not None
    assert invoice.paid_at.date() == date(2026, 1, 11)


def test_mark_paid_fails_without_method(client, db_session):
    seed_payment_methods(db_session)
    invoice = _make_invoice(db_session)

    response = client.post(
        f"/invoices/{invoice.id}/paid",
        data={"paid_at": "2026-01-11T14:30"},
    )

    assert response.status_code == 400
    assert "Payment method is required." in response.text


def test_mark_paid_fails_without_date(client, db_session):
    seed_payment_methods(db_session)
    payment_method = db_session.execute(
        select(PaymentMethod).where(PaymentMethod.is_active.is_(True)).limit(1)
    ).scalar_one()
    invoice = _make_invoice(db_session)

    response = client.post(
        f"/invoices/{invoice.id}/paid",
        data={"payment_method_id": str(payment_method.id)},
    )

    assert response.status_code == 400
    assert "Paid date is required." in response.text


def test_mark_paid_fails_with_invalid_date(client, db_session):
    seed_payment_methods(db_session)
    payment_method = db_session.execute(
        select(PaymentMethod).where(PaymentMethod.is_active.is_(True)).limit(1)
    ).scalar_one()
    invoice = _make_invoice(db_session)

    response = client.post(
        f"/invoices/{invoice.id}/paid",
        data={"payment_method_id": str(payment_method.id), "paid_at": "not-a-date"},
    )

    assert response.status_code == 400
    assert "Paid date must be valid." in response.text


def test_mark_paid_fails_when_invoice_is_void(client, db_session):
    seed_payment_methods(db_session)
    payment_method = db_session.execute(
        select(PaymentMethod).where(PaymentMethod.is_active.is_(True)).limit(1)
    ).scalar_one()
    invoice = _make_invoice(db_session, status="VOID")

    response = client.post(
        f"/invoices/{invoice.id}/paid",
        data={"payment_method_id": str(payment_method.id), "paid_at": "2026-01-11T14:30"},
    )

    assert response.status_code == 400
    assert "Invoice is VOID and cannot be modified." in response.text


def test_mark_paid_fails_when_invoice_is_already_paid(client, db_session):
    seed_payment_methods(db_session)
    payment_method = db_session.execute(
        select(PaymentMethod).where(PaymentMethod.is_active.is_(True)).limit(1)
    ).scalar_one()
    invoice = _make_invoice(db_session, status="PAID")

    response = client.post(
        f"/invoices/{invoice.id}/paid",
        data={"payment_method_id": str(payment_method.id), "paid_at": "2026-01-11T14:30"},
    )

    assert response.status_code == 400
    assert "Already paid." in response.text


def test_invoice_detail_hides_void_form_when_paid(client, db_session):
    invoice = _make_invoice(db_session, status="PAID")

    response = client.get(f"/invoices/{invoice.id}")

    assert response.status_code == 200
    assert f'action="/invoices/{invoice.id}/paid"' not in response.text
    assert f'action="/invoices/{invoice.id}/void"' not in response.text
    assert "Already paid." in response.text
    assert "Cannot void a paid invoice." in response.text


def test_invoice_detail_hides_paid_and_void_forms_when_void(client, db_session):
    invoice = _make_invoice(db_session, status="VOID")

    response = client.get(f"/invoices/{invoice.id}")

    assert response.status_code == 200
    assert f'action="/invoices/{invoice.id}/paid"' not in response.text
    assert f'action="/invoices/{invoice.id}/void"' not in response.text
    assert "Invoice is VOID and cannot be modified." in response.text


def test_mark_paid_followup_shows_paid_summary_and_blocks_repeat_pay(client, db_session):
    seed_payment_methods(db_session)
    payment_method = db_session.execute(
        select(PaymentMethod).where(PaymentMethod.is_active.is_(True)).limit(1)
    ).scalar_one()
    invoice = _make_invoice(db_session)

    response = client.post(
        f"/invoices/{invoice.id}/paid",
        data={"payment_method_id": str(payment_method.id), "paid_at": "2026-01-11"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    follow = client.get(response.headers["location"])

    assert follow.status_code == 200
    assert payment_method.code in follow.text
    assert re.search(r"\b11/01/(26|2026)\b", follow.text)
    assert 'id="mark-paid-form"' not in follow.text
    assert "Already paid." in follow.text


def test_invoice_detail_shows_void_heading_and_mark_paid_labels(client, db_session):
    seed_payment_methods(db_session)
    invoice = _make_invoice(db_session, status="DRAFT")

    response = client.get(f"/invoices/{invoice.id}")

    assert response.status_code == 200
    assert '<summary class="card-header">Void Invoice</summary>' in response.text
    assert 'id="mark-paid-form"' in response.text
    assert f'action="/invoices/{invoice.id}/paid"' in response.text
    assert '<label for="payment_method_id">Payment method</label>' in response.text
    assert '<label for="paid_at">Paid date</label>' in response.text
