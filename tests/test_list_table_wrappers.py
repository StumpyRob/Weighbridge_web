from datetime import datetime

import pytest

from app.models import DirectionEnum, Ticket, TicketStatusEnum, TransactionTypeEnum


@pytest.mark.parametrize(
    "path",
    [
        "/tickets",
        "/customers",
        "/vehicles",
        "/products",
        "/products/units",
        "/products/groups",
        "/invoices",
    ],
)
def test_list_pages_render_table_wrapper(client, path):
    response = client.get(path)

    assert response.status_code == 200
    assert 'class="table-wrap' in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/lookups/hauliers",
        "/lookups/drivers",
        "/lookups/containers",
        "/lookups/destinations",
        "/admin/printing/destinations",
    ],
)
def test_lookup_tabs_render_table_wrapper_and_actions_column(client, path):
    response = client.get(path)

    assert response.status_code == 200
    assert 'class="table-wrap' in response.text
    assert (
        '<th class="actions-col">Actions</th>' in response.text
        or 'class="actions-col destinations-col-actions">Actions</th>' in response.text
    )


def test_tickets_list_renders_status_column_markup(client, db_session):
    ticket = Ticket(
        ticket_no="T-WRAP-1",
        datetime=datetime(2026, 2, 18, 10, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.get("/tickets")

    assert response.status_code == 200
    assert 'class="table-wrap' in response.text
    assert '<th class="status-col">Status</th>' in response.text
    assert '<th class="type-col">Type</th>' in response.text
    assert '<td class="status-col">' in response.text
    assert '<td class="type-col">' in response.text
