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


def test_product_groups_list_actions_use_small_button_class(client, db_session):
    group = ProductGroup(code="UI-GRP-1", name="UI Group", is_active=True)
    db_session.add(group)
    db_session.commit()

    response = client.get("/products/groups")

    assert response.status_code == 200
    assert 'class="btn btn--ghost btn--sm"' in response.text
    assert 'class="btn btn--danger btn--sm"' in response.text
