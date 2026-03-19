import re
from decimal import Decimal

from sqlalchemy import select

from app.models import Product, TaxRate, Unit


def _tax_rate() -> TaxRate:
    return TaxRate(
        code="TYPE-TEST VAT",
        description="Product type test VAT",
        rate_percent=Decimal("0.20"),
        is_active=True,
    )


def test_product_create_persists_sale_product_type(client, db_session):
    tax_rate = _tax_rate()
    db_session.add(tax_rate)
    db_session.commit()

    response = client.post(
        "/products/new",
        data={
            "code": "P-TYPE-SALE-1",
            "description": "Sale Type Product",
            "sale_type": "WEIGHT",
            "product_type": "sale",
            "tax_rate_id": str(tax_rate.id),
            "unit_price": "14.50",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    product = db_session.execute(
        select(Product).where(Product.code == "P-TYPE-SALE-1")
    ).scalar_one()
    assert product.product_type == "sale"


def test_product_create_persists_waste_product_type(client, db_session):
    tax_rate = TaxRate(
        code="TYPE-TEST VAT 2",
        description="Product type test VAT 2",
        rate_percent=Decimal("0.20"),
        is_active=True,
    )
    db_session.add(tax_rate)
    db_session.commit()

    response = client.post(
        "/products/new",
        data={
            "code": "P-TYPE-WASTE-1",
            "description": "Waste Type Product",
            "sale_type": "WEIGHT",
            "product_type": "waste",
            "tax_rate_id": str(tax_rate.id),
            "unit_price": "95.00",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    product = db_session.execute(
        select(Product).where(Product.code == "P-TYPE-WASTE-1")
    ).scalar_one()
    assert product.product_type == "waste"


def test_product_edit_legacy_null_product_type_prefills_derived_value(
    client, db_session
):
    unit = Unit(name="Legacy Type Unit", unit_type="COUNT", is_active=True)
    product = Product(
        code="P-LEGACY-TYPE-1",
        description="Legacy Product Type",
        product_type=None,
        sales_only=False,
        final_disposal_wip=True,
        unit=unit,
        unit_price=Decimal("12.00"),
    )
    db_session.add_all([unit, product])
    db_session.commit()

    response = client.get(f"/products/{product.id}")

    assert response.status_code == 200
    assert 'id="product_type"' in response.text
    assert re.search(
        r'<select[^>]*id="product_type"[^>]*>.*?<option value="waste" selected',
        response.text,
        flags=re.DOTALL,
    )


def test_product_save_requires_valid_single_product_type_value(client, db_session):
    tax_rate = TaxRate(
        code="TYPE-TEST VAT 3",
        description="Product type test VAT 3",
        rate_percent=Decimal("0.20"),
        is_active=True,
    )
    db_session.add(tax_rate)
    db_session.commit()

    missing = client.post(
        "/products/new",
        data={
            "code": "P-TYPE-REQ-1",
            "description": "Missing Product Type",
            "sale_type": "WEIGHT",
            "tax_rate_id": str(tax_rate.id),
            "unit_price": "10.00",
        },
    )
    assert missing.status_code == 400
    assert "Product type is required." in missing.text

    invalid = client.post(
        "/products/new",
        data={
            "code": "P-TYPE-REQ-2",
            "description": "Invalid Product Type",
            "sale_type": "WEIGHT",
            "product_type": "sale,waste",
            "tax_rate_id": str(tax_rate.id),
            "unit_price": "11.00",
        },
    )
    assert invalid.status_code == 400
    assert "Product type must be sale or waste." in invalid.text

    assert (
        db_session.execute(
            select(Product).where(Product.code.in_(["P-TYPE-REQ-1", "P-TYPE-REQ-2"]))
        )
        .scalars()
        .first()
        is None
    )


def test_product_create_persists_operational_flags(client, db_session):
    tax_rate = TaxRate(
        code="TYPE-TEST VAT 4",
        description="Product type test VAT 4",
        rate_percent=Decimal("0.20"),
        is_active=True,
    )
    db_session.add(tax_rate)
    db_session.commit()

    response = client.post(
        "/products/new",
        data={
            "code": "P-TYPE-FLAGS-1",
            "description": "Final Disposal Product",
            "sale_type": "WEIGHT",
            "product_type": "waste",
            "tax_rate_id": str(tax_rate.id),
            "unit_price": "75.00",
            "final_disposal": "on",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    product = db_session.execute(
        select(Product).where(Product.code == "P-TYPE-FLAGS-1")
    ).scalar_one()
    assert product.final_disposal is True
    assert product.used_on_site is False
    # Keep legacy snapshot fields aligned for compatibility.
    assert product.final_disposal_wip is True
    assert product.used_on_site_wip is False


def test_product_edit_updates_operational_flags(client, db_session):
    tax_rate = TaxRate(
        code="TYPE-TEST VAT 5",
        description="Product type test VAT 5",
        rate_percent=Decimal("0.20"),
        is_active=True,
    )
    product = Product(
        code="P-TYPE-FLAGS-2",
        description="Operational Flag Product",
        product_type="waste",
        final_disposal=True,
        used_on_site=False,
        final_disposal_wip=True,
        used_on_site_wip=False,
        unit_price=Decimal("60.00"),
    )
    db_session.add_all([tax_rate, product])
    db_session.commit()

    response = client.post(
        f"/products/{product.id}",
        data={
            "code": "P-TYPE-FLAGS-2",
            "description": "Operational Flag Product Updated",
            "sale_type": "WEIGHT",
            "product_type": "waste",
            "tax_rate_id": str(tax_rate.id),
            "unit_price": "60.00",
            "used_on_site": "on",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    db_session.refresh(product)
    assert product.final_disposal is False
    assert product.used_on_site is True
    assert product.final_disposal_wip is False
    assert product.used_on_site_wip is True
