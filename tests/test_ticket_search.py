from datetime import datetime
from decimal import Decimal

from app.models import (
    EwcCode,
    Product,
    Ticket,
    TicketStatusEnum,
    DirectionEnum,
    TransactionTypeEnum,
)


def _make_ewc(code_6: str, description: str) -> EwcCode:
    return EwcCode(
        code_6=code_6,
        code_display=f"{code_6[0:2]} {code_6[2:4]} {code_6[4:6]}",
        description=description,
        hazardous=False,
        active=True,
        source_file="test",
        imported_at=datetime(2026, 1, 1, 0, 0, 0),
    )


def _make_ticket(*, ticket_no: str) -> Ticket:
    return Ticket(
        ticket_no=ticket_no,
        datetime=datetime(2026, 2, 14, 9, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )


def test_ticket_list_search_matches_ticket_ewc_snapshot_fields(client, db_session):
    matching = _make_ticket(ticket_no="T-EWC-SRCH-1")
    matching.ewc_code_6 = "010101"
    matching.ewc_code_display = "01 01 01"
    matching.ewc_description = "Asbestos-containing waste"

    other = _make_ticket(ticket_no="T-EWC-SRCH-2")
    other.ewc_code_6 = "020202"
    other.ewc_code_display = "02 02 02"
    other.ewc_description = "Other waste stream"

    db_session.add_all([matching, other])
    db_session.commit()

    by_code = client.get("/tickets", params={"q": "01 01 01"})
    assert by_code.status_code == 200
    assert matching.ticket_no in by_code.text
    assert other.ticket_no not in by_code.text

    by_description = client.get("/tickets", params={"q": "asbestos"})
    assert by_description.status_code == 200
    assert matching.ticket_no in by_description.text
    assert other.ticket_no not in by_description.text


def test_ticket_list_search_matches_product_default_ewc_fields(client, db_session):
    ewc = _make_ewc("170904", "Mixed construction and demolition waste")
    product = Product(
        code="P-TICKET-EWC-SRCH",
        description="Ticket EWC Search Product",
        unit_price=Decimal("10.00"),
        ewc_code=ewc,
    )
    matching = _make_ticket(ticket_no="T-PROD-EWC-SRCH-1")
    matching.product = product

    other = _make_ticket(ticket_no="T-PROD-EWC-SRCH-2")
    db_session.add_all([ewc, product, matching, other])
    db_session.commit()

    by_product_ewc = client.get("/tickets", params={"q": "17 09 04"})
    assert by_product_ewc.status_code == 200
    assert matching.ticket_no in by_product_ewc.text
    assert other.ticket_no not in by_product_ewc.text

    by_product_ewc_desc = client.get("/tickets", params={"q": "demolition"})
    assert by_product_ewc_desc.status_code == 200
    assert matching.ticket_no in by_product_ewc_desc.text
    assert other.ticket_no not in by_product_ewc_desc.text
