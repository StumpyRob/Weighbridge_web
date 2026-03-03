from datetime import datetime
from decimal import Decimal

from app.models import EwcCode, Product
from app.seed import seed_tax_rates, seed_units


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


def test_products_search_matches_ewc_fields(client, db_session):
    ewc = _make_ewc("060105", "Mixed municipal wastes")
    product_with_ewc = Product(
        code="P-EWC-SEARCH-1",
        description="Waste stream product",
        unit_price=Decimal("12.00"),
        ewc_code=ewc,
    )
    other_product = Product(
        code="P-OTHER-SEARCH-1",
        description="Unrelated product",
        unit_price=Decimal("9.00"),
    )
    db_session.add_all([ewc, product_with_ewc, other_product])
    db_session.commit()

    by_display = client.get("/products", params={"q": "06 01 05"})
    assert by_display.status_code == 200
    assert product_with_ewc.code in by_display.text
    assert other_product.code not in by_display.text

    by_digits = client.get("/products", params={"q": "060105"})
    assert by_digits.status_code == 200
    assert product_with_ewc.code in by_digits.text
    assert other_product.code not in by_digits.text

    by_ewc_description = client.get("/products", params={"q": "municipal"})
    assert by_ewc_description.status_code == 200
    assert product_with_ewc.code in by_ewc_description.text
    assert other_product.code not in by_ewc_description.text


def test_products_search_escapes_like_wildcards(client, db_session):
    literal_wildcard_product = Product(
        code="P%WILDCARD",
        description="Product with percent in code",
        unit_price=Decimal("5.00"),
    )
    normal_product = Product(
        code="PNORMAL",
        description="Normal product",
        unit_price=Decimal("6.00"),
    )
    db_session.add_all([literal_wildcard_product, normal_product])
    db_session.commit()

    response = client.get("/products", params={"q": "%"})
    assert response.status_code == 200
    assert literal_wildcard_product.code in response.text
    assert normal_product.code not in response.text


def test_products_new_ewc_datalist_allows_description_prefix_search(client, db_session):
    seed_units(db_session)
    seed_tax_rates(db_session)
    ewc = _make_ewc("010105", "Acids and alkalis")
    db_session.add(ewc)
    db_session.commit()

    response = client.get("/products/new")

    assert response.status_code == 200
    assert 'id="ewc_code_list"' in response.text
    assert 'value="01 01 05 - Acids and alkalis"' in response.text
    assert 'value="Acids and alkalis - 01 01 05"' in response.text
    assert 'data-canonical="01 01 05 - Acids and alkalis"' in response.text
