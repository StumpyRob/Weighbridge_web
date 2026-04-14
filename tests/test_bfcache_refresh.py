def test_bfcache_refresh_script_targets_row_version_forms(client_anonymous):
    response = client_anonymous.get("/static/js/bfcache_refresh.js")

    assert response.status_code == 200
    assert "pageshow" in response.text
    assert 'input[name="row_version"]' in response.text
    assert "back_forward" in response.text


def test_customer_edit_page_includes_bfcache_refresh_script(client, db_session):
    from app.models import Customer

    customer = Customer(account_code="C-BFCACHE-1", name="BFCache Customer")
    db_session.add(customer)
    db_session.commit()

    response = client.get(f"/customers/{customer.id}")

    assert response.status_code == 200
    assert 'name="row_version"' in response.text
    assert '/static/js/bfcache_refresh.js?v=' in response.text
