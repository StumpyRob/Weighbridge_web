from datetime import datetime
from decimal import Decimal

import app.routes.invoices as invoice_routes
from sqlalchemy import select

from app.models import (
    Customer,
    DirectionEnum,
    Invoice,
    InvoiceSequence,
    Product,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
    Unit,
)


def _make_invoiceable_ticket(db_session, *, customer_id: int, ticket_no: str, dt: datetime):
    unit = Unit(name=f"Invoice Date Unit {ticket_no}", unit_type="COUNT", is_active=True)
    product = Product(
        code=f"P-INV-DATE-{ticket_no}",
        description=f"Invoice Date Product {ticket_no}",
        unit=unit,
        unit_price=Decimal("10.00"),
    )
    ticket = Ticket(
        ticket_no=ticket_no,
        datetime=dt,
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        customer_id=customer_id,
        product=product,
        qty=1,
        unit_price=Decimal("10.00"),
        total=Decimal("10.00"),
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([unit, product, ticket])
    db_session.commit()
    return ticket


def test_invoice_generate_renders_quick_range_buttons(client):
    response = client.get("/invoices/generate")

    assert response.status_code == 200
    assert 'data-quick-range="this-week"' in response.text
    assert "This week (Mon-Sun)" in response.text
    assert 'data-quick-range="last-week"' in response.text
    assert 'data-quick-range="this-month"' in response.text
    assert 'data-quick-range="last-month"' in response.text
    assert 'data-quick-range="clear"' in response.text


def test_invoice_generate_preview_accepts_single_uk_date(client, db_session):
    customer = Customer(account_code="C-INV-DATE-1", name="Invoice Date Customer 1")
    db_session.add(customer)
    db_session.commit()
    ticket = _make_invoiceable_ticket(
        db_session,
        customer_id=customer.id,
        ticket_no="T-INV-DATE-1",
        dt=datetime(2026, 2, 12, 10, 0, 0),
    )

    response = client.post(
        "/invoices/generate",
        data={
            "customer_id": str(customer.id),
            "date_from": "12/02/2026",
            "date_to": "12/02/2026",
        },
    )

    assert response.status_code == 200
    assert "must be valid" not in response.text
    assert "is required." not in response.text
    assert ticket.ticket_no in response.text


def test_invoice_generate_preview_accepts_uk_date_range(client, db_session):
    customer = Customer(account_code="C-INV-DATE-2", name="Invoice Date Customer 2")
    db_session.add(customer)
    db_session.commit()
    ticket = _make_invoiceable_ticket(
        db_session,
        customer_id=customer.id,
        ticket_no="T-INV-DATE-2",
        dt=datetime(2026, 2, 15, 9, 0, 0),
    )

    response = client.post(
        "/invoices/generate",
        data={
            "customer_id": str(customer.id),
            "date_from": "01/02/2026",
            "date_to": "28/02/2026",
        },
    )

    assert response.status_code == 200
    assert "Date from must be valid (dd/mm/yyyy)." not in response.text
    assert "Date to must be valid (dd/mm/yyyy)." not in response.text
    assert ticket.ticket_no in response.text


def test_invoice_generate_preview_accepts_iso_date_range(client, db_session):
    customer = Customer(account_code="C-INV-DATE-ISO", name="Invoice Date Customer ISO")
    db_session.add(customer)
    db_session.commit()
    ticket = _make_invoiceable_ticket(
        db_session,
        customer_id=customer.id,
        ticket_no="T-INV-DATE-ISO",
        dt=datetime(2026, 2, 15, 9, 0, 0),
    )

    response = client.post(
        "/invoices/generate",
        data={
            "customer_id": str(customer.id),
            "date_from": "2026-02-01",
            "date_to": "2026-02-28",
        },
    )

    assert response.status_code == 200
    assert "Date from must be valid (dd/mm/yyyy)." not in response.text
    assert "Date to must be valid (dd/mm/yyyy)." not in response.text
    assert ticket.ticket_no in response.text


def test_invoice_generate_preview_rejects_invalid_uk_date(client, db_session):
    customer = Customer(account_code="C-INV-DATE-3", name="Invoice Date Customer 3")
    db_session.add(customer)
    db_session.commit()

    response = client.post(
        "/invoices/generate",
        data={
            "customer_id": str(customer.id),
            "date_from": "31/02/2026",
            "date_to": "31/02/2026",
        },
    )

    assert response.status_code == 200
    assert "Date from must be valid (dd/mm/yyyy)." in response.text
    assert "Date to must be valid (dd/mm/yyyy)." in response.text


def test_invoice_generate_preview_rejects_from_after_to(client, db_session):
    customer = Customer(account_code="C-INV-DATE-4", name="Invoice Date Customer 4")
    db_session.add(customer)
    db_session.commit()

    response = client.post(
        "/invoices/generate",
        data={
            "customer_id": str(customer.id),
            "date_from": "28/02/2026",
            "date_to": "01/02/2026",
        },
    )

    assert response.status_code == 200
    assert "Date from must be on or before date to." in response.text


def test_invoice_generate_confirm_resyncs_stale_invoice_sequence(
    client, db_session, monkeypatch
):
    fixed_now = datetime(2026, 4, 15, 12, 0, 0)
    monkeypatch.setattr(invoice_routes, "utcnow", lambda: fixed_now)

    customer = Customer(
        account_code="C-INV-SEQ-1",
        name="Invoice Sequence Customer",
    )
    db_session.add(customer)
    db_session.flush()

    # Simulate a demo/imported dataset where invoice rows already exist but the
    # sequence table was left behind.
    db_session.add_all(
        [
            Invoice(
                invoice_no="INV-26-00001",
                customer_id=customer.id,
                invoice_date=fixed_now.date(),
                status="PAID",
                net_total=Decimal("10.00"),
                vat_total=Decimal("2.00"),
                gross_total=Decimal("12.00"),
            ),
            Invoice(
                invoice_no="INV-26-00007",
                customer_id=customer.id,
                invoice_date=fixed_now.date(),
                status="PAID",
                net_total=Decimal("10.00"),
                vat_total=Decimal("2.00"),
                gross_total=Decimal("12.00"),
            ),
            InvoiceSequence(
                year=2026,
                last_number=0,
                updated_at=fixed_now,
            ),
        ]
    )
    db_session.commit()

    _make_invoiceable_ticket(
        db_session,
        customer_id=customer.id,
        ticket_no="T-INV-SEQ-1",
        dt=datetime(2026, 4, 14, 10, 0, 0),
    )

    response = client.post(
        "/invoices/generate/confirm",
        data={
            "customer_id": str(customer.id),
            "date_from": "01/04/2026",
            "date_to": "30/04/2026",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    invoice = db_session.execute(
        select(Invoice).order_by(Invoice.id.desc()).limit(1)
    ).scalar_one()
    assert invoice.invoice_no == "INV-26-00008"

    sequence = db_session.get(InvoiceSequence, 2026)
    assert sequence is not None
    assert sequence.last_number == 8


def test_generate_invoice_no_uses_postgres_safe_insert(monkeypatch):
    fixed_now = datetime(2026, 4, 15, 12, 0, 0)
    monkeypatch.setattr(invoice_routes, "utcnow", lambda: fixed_now)

    class _FakeDialect:
        name = "postgresql"

    class _FakeBind:
        dialect = _FakeDialect()

    class _FakeResult:
        def __init__(self, *, rows=None, scalar=None):
            self._rows = rows or []
            self._scalar = scalar

        def scalars(self):
            return self._rows

        def scalar_one(self):
            return self._scalar

    class _FakeSession:
        def __init__(self):
            self.calls = []

        def get_bind(self):
            return _FakeBind()

        def execute(self, statement, params=None):
            sql = str(statement)
            self.calls.append((sql, params))
            if "SELECT invoices.invoice_no" in sql:
                return _FakeResult(rows=[])
            if "SELECT last_number FROM invoice_sequences" in sql:
                return _FakeResult(scalar=1)
            return _FakeResult()

    fake_db = _FakeSession()

    invoice_no = invoice_routes._generate_invoice_no(fake_db)

    assert invoice_no == "INV-26-00001"
    assert any(
        "ON CONFLICT (year) DO NOTHING" in sql for sql, _params in fake_db.calls
    )
    assert not any("INSERT OR IGNORE" in sql for sql, _params in fake_db.calls)
