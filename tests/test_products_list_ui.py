import re
from decimal import Decimal

from app.models import Product, ProductGroup, TaxRate, Unit


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
