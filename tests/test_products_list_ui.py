import re
from datetime import datetime
from decimal import Decimal

from app.models import (
    DirectionEnum,
    Product,
    ProductGroup,
    TaxRate,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
    Unit,
)


def test_products_list_renders_unit_and_tax_labels_not_raw_ids(client, db_session):
    unit = Unit(name="Each UI", unit_type="COUNT", is_active=True)
    tax_rate = TaxRate(
        code="UI VAT 20",
        description="UI VAT standard",
        rate_percent=Decimal("0.20"),
        is_active=True,
    )
    product = Product(
        code="P-UI-LIST-1",
        description="Operator view product",
        unit=unit,
        tax_rate=tax_rate,
        unit_price=Decimal("12.50"),
    )
    db_session.add_all([unit, tax_rate, product])
    db_session.commit()

    response = client.get("/products")

    assert response.status_code == 200
    assert "unit_id" not in response.text
    assert "tax_rate_id" not in response.text

    row_match = re.search(
        rf'<tr[^>]*data-row-link="/products/{product.id}".*?</tr>',
        response.text,
        re.DOTALL,
    )
    assert row_match is not None
    row_html = row_match.group(0)

    assert unit.name in row_html
    assert "20%" in row_html
    assert f">{unit.id}<" not in row_html
    assert f">{tax_rate.id}<" not in row_html


def test_units_list_actions_use_small_button_class(client, db_session):
    unit = Unit(name="UI List Count Unit", unit_type="COUNT", is_active=True)
    db_session.add(unit)
    db_session.commit()

    response = client.get("/products/units")

    assert response.status_code == 200
    assert 'class="btn btn--ghost btn--sm"' in response.text
    assert 'class="btn btn--danger btn--sm"' in response.text


def test_units_list_shows_system_weight_units_with_highlight(client, db_session):
    response = client.get("/products/units")

    assert response.status_code == 200
    assert ">KG<" in response.text
    assert ">Tonnes<" in response.text
    assert 'class="lookup-row--system-default"' in response.text


def test_units_list_places_system_weight_units_after_count_units(client, db_session):
    custom_count_unit = Unit(name="ZZZ Count Unit", unit_type="COUNT", is_active=True)
    db_session.add(custom_count_unit)
    db_session.commit()

    response = client.get("/products/units")

    assert response.status_code == 200
    custom_pos = response.text.find("ZZZ Count Unit")
    kg_pos = response.text.find(">KG<")
    tonnes_pos = response.text.find(">Tonnes<")
    assert custom_pos >= 0
    assert kg_pos >= 0
    assert tonnes_pos >= 0
    assert custom_pos < kg_pos
    assert custom_pos < tonnes_pos


def test_units_list_shows_delete_action_for_count_units(client, db_session):
    unit = Unit(name="Delete UI Unit", unit_type="COUNT", is_active=True)
    db_session.add(unit)
    db_session.commit()

    response = client.get("/products/units")

    assert response.status_code == 200
    assert f'action="/products/units/{unit.id}/delete"' in response.text


def test_units_delete_removes_unused_count_unit(client, db_session):
    unit = Unit(name="Delete Me Unit", unit_type="COUNT", is_active=True)
    db_session.add(unit)
    db_session.commit()
    unit_id = unit.id

    response = client.post(f"/products/units/{unit_id}/delete", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].endswith("/products/units?saved=1")
    db_session.expire_all()
    assert db_session.get(Unit, unit_id) is None


def test_units_delete_blocks_when_unit_is_used_by_product(client, db_session):
    unit = Unit(name="Used Delete Unit", unit_type="COUNT", is_active=True)
    tax_rate = TaxRate(
        code="UNIT-DELETE-VAT",
        description="Unit delete VAT",
        rate_percent=Decimal("0.20"),
        is_active=True,
    )
    product = Product(
        code="P-UNIT-DELETE-1",
        description="Unit delete product",
        unit=unit,
        tax_rate=tax_rate,
        unit_price=Decimal("5.00"),
    )
    db_session.add_all([unit, tax_rate, product])
    db_session.commit()

    response = client.post(f"/products/units/{unit.id}/delete", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].endswith("/products/units?error=in_use")
    assert db_session.get(Unit, unit.id) is not None


def test_product_groups_list_actions_use_small_button_class(client, db_session):
    group = ProductGroup(code="UI-GRP-1", name="UI Group", is_active=True)
    db_session.add(group)
    db_session.commit()

    response = client.get("/products/groups")

    assert response.status_code == 200
    assert 'class="btn btn--ghost btn--sm"' in response.text
    assert 'class="btn btn--danger btn--sm"' in response.text
    assert f'action="/products/groups/{group.id}/delete"' in response.text


def test_products_list_shows_delete_action(client, db_session):
    unit = Unit(name="UI Product Delete Unit", unit_type="COUNT", is_active=True)
    product = Product(
        code="P-UI-DEL-1",
        description="UI Product Delete",
        unit=unit,
        unit_price=Decimal("1.00"),
    )
    db_session.add_all([unit, product])
    db_session.commit()

    response = client.get("/products")

    assert response.status_code == 200
    assert f'action="/products/{product.id}/delete"' in response.text


def test_products_delete_removes_unused_product(client, db_session):
    unit = Unit(name="Delete Product Unit", unit_type="COUNT", is_active=True)
    product = Product(
        code="P-DEL-OK-1",
        description="Delete Product OK",
        unit=unit,
        unit_price=Decimal("1.00"),
    )
    db_session.add_all([unit, product])
    db_session.commit()
    product_id = product.id

    response = client.post(f"/products/{product_id}/delete", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].endswith("/products?saved=1")
    db_session.expire_all()
    assert db_session.get(Product, product_id) is None


def test_products_delete_blocks_when_product_is_used_by_ticket(client, db_session):
    unit = Unit(name="Delete Block Unit", unit_type="COUNT", is_active=True)
    product = Product(
        code="P-DEL-BLOCK-1",
        description="Delete Product Block",
        unit=unit,
        unit_price=Decimal("1.00"),
    )
    ticket = Ticket(
        ticket_no="T-PROD-DEL-1",
        datetime=datetime(2026, 2, 25, 10, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        product=product,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([unit, product, ticket])
    db_session.commit()

    response = client.post(f"/products/{product.id}/delete", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].endswith("/products?error=in_use")
    assert db_session.get(Product, product.id) is not None


def test_product_groups_delete_removes_unused_group(client, db_session):
    group = ProductGroup(code="UI-GRP-DEL-1", name="UI Group Delete", is_active=True)
    db_session.add(group)
    db_session.commit()
    group_id = group.id

    response = client.post(f"/products/groups/{group_id}/delete", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].endswith("/products/groups?saved=1")
    db_session.expire_all()
    assert db_session.get(ProductGroup, group_id) is None


def test_product_groups_delete_blocks_when_group_has_products(client, db_session):
    unit = Unit(name="Group Delete Block Unit", unit_type="COUNT", is_active=True)
    group = ProductGroup(code="UI-GRP-DEL-BLK", name="UI Group Delete Block", is_active=True)
    db_session.add_all([unit, group])
    db_session.flush()
    product = Product(
        code="P-GRP-DEL-BLK-1",
        description="Group Delete Block Product",
        unit=unit,
        unit_price=Decimal("1.00"),
        group_id=group.id,
    )
    db_session.add(product)
    db_session.commit()

    response = client.post(f"/products/groups/{group.id}/delete", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].endswith("/products/groups?error=in_use")
    assert db_session.get(ProductGroup, group.id) is not None


def test_product_groups_list_shows_product_count_per_group(client, db_session):
    unit = Unit(name="UI Group Count Unit", unit_type="COUNT", is_active=True)
    group = ProductGroup(code="UI-GRP-CNT-1", name="UI Group Count", is_active=True)
    product = Product(
        code="P-UI-GRP-CNT-1",
        description="UI Group Count Product",
        unit=unit,
        unit_price=Decimal("1.00"),
    )
    db_session.add_all([unit, group])
    db_session.flush()
    product.group_id = group.id
    db_session.add(product)
    db_session.commit()

    response = client.get("/products/groups")

    assert response.status_code == 200
    assert "<th>Products</th>" in response.text
    row_match = re.search(
        rf'<tr[^>]*data-row-link="/products/groups/{group.id}/edit".*?</tr>',
        response.text,
        re.DOTALL,
    )
    assert row_match is not None
    assert ">1<" in row_match.group(0)


def test_product_group_edit_page_lists_products_in_group(client, db_session):
    unit = Unit(name="UI Group Edit Unit", unit_type="COUNT", is_active=True)
    group = ProductGroup(code="UI-GRP-EDIT-1", name="UI Group Edit", is_active=True)
    db_session.add_all([unit, group])
    db_session.flush()
    first = Product(
        code="P-UI-GRP-EDIT-1",
        description="Group Edit Product One",
        unit=unit,
        unit_price=Decimal("1.00"),
        group_id=group.id,
    )
    second = Product(
        code="P-UI-GRP-EDIT-2",
        description="Group Edit Product Two",
        unit=unit,
        unit_price=Decimal("2.00"),
        group_id=group.id,
    )
    db_session.add_all([first, second])
    db_session.commit()

    response = client.get(f"/products/groups/{group.id}/edit")

    assert response.status_code == 200
    assert "Products In This Group (2)" in response.text
    assert "P-UI-GRP-EDIT-1" in response.text
    assert "Group Edit Product One" in response.text
    assert "P-UI-GRP-EDIT-2" in response.text
