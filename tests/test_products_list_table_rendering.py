import re
from decimal import Decimal

from app.models import Product, TaxRate, Unit


def test_products_list_renders_unit_and_tax_labels_not_ids(client, db_session):
    unit = Unit(name="Each Render", unit_type="COUNT", is_active=True)
    tax_rate = TaxRate(
        code="Render VAT 20",
        description="Render VAT",
        rate_percent=Decimal("0.20"),
        is_active=True,
    )
    product = Product(
        code="P-TABLE-RENDER-1",
        description="Rendered list product",
        unit=unit,
        tax_rate=tax_rate,
        unit_price=Decimal("9.99"),
    )
    db_session.add_all([unit, tax_rate, product])
    db_session.commit()

    response = client.get("/products")

    assert response.status_code == 200
    row_match = re.search(
        rf'<tr[^>]*data-row-link="/products/{product.id}".*?</tr>',
        response.text,
        re.DOTALL,
    )
    assert row_match is not None
    row_html = row_match.group(0)

    assert "Each Render" in row_html
    assert "20%" in row_html
    assert f">{unit.id}<" not in row_html
    assert f">{tax_rate.id}<" not in row_html
