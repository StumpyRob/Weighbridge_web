from decimal import Decimal

from sqlalchemy import func, select

from app.models import Product, TaxRate


def test_products_create_duplicate_code_returns_friendly_error(client, db_session):
    tax_rate = TaxRate(
        code="TEST VAT",
        description="Test VAT",
        rate_percent=Decimal("0.200"),
        is_active=True,
    )
    db_session.add(tax_rate)
    db_session.flush()
    existing = Product(
        code="TOPSOIL",
        description="Topsoil Existing",
        tax_rate_id=tax_rate.id,
        unit_price=Decimal("20.00"),
    )
    db_session.add(existing)
    db_session.commit()

    before_count = db_session.execute(select(func.count(Product.id))).scalar_one()

    response = client.post(
        "/products/new",
        data={
            "code": "topsoil",
            "description": "Topsoil Duplicate Attempt",
            "sale_type": "WEIGHT",
            "product_type": "sale",
            "tax_rate_id": str(tax_rate.id),
            "unit_price": "20.00",
            "ewc_code_id": "",
            "ewc_code_label": "",
            "default_destination_id": "",
            "is_hazardous": "",
            "sales_only": "",
        },
    )

    assert response.status_code == 400
    assert "Product code already exists." in response.text
    assert 'name="code" value="TOPSOIL"' in response.text
    assert 'name="description" value="Topsoil Duplicate Attempt"' in response.text

    after_count = db_session.execute(select(func.count(Product.id))).scalar_one()
    assert after_count == before_count


def test_products_edit_duplicate_code_returns_friendly_error(client, db_session):
    tax_rate = TaxRate(
        code="TEST VAT 2",
        description="Test VAT 2",
        rate_percent=Decimal("0.200"),
        is_active=True,
    )
    db_session.add(tax_rate)
    db_session.flush()
    topsoil = Product(
        code="TOPSOIL",
        description="Topsoil",
        tax_rate_id=tax_rate.id,
        unit_price=Decimal("20.00"),
    )
    hivis = Product(
        code="HIVIS",
        description="HiVis",
        tax_rate_id=tax_rate.id,
        unit_price=Decimal("10.00"),
    )
    db_session.add_all([topsoil, hivis])
    db_session.commit()

    response = client.post(
        f"/products/{hivis.id}",
        data={
            "code": "topsoil",
            "description": "HiVis Duplicate Attempt",
            "sale_type": "WEIGHT",
            "product_type": "sale",
            "tax_rate_id": str(tax_rate.id),
            "unit_price": "10.00",
            "ewc_code_id": "",
            "ewc_code_label": "",
            "default_destination_id": "",
            "is_hazardous": "",
            "sales_only": "",
        },
    )

    assert response.status_code == 400
    assert "Product code already exists." in response.text
    assert 'name="code" value="TOPSOIL"' in response.text
    assert 'name="description" value="HiVis Duplicate Attempt"' in response.text

    db_session.refresh(hivis)
    assert hivis.code == "HIVIS"
