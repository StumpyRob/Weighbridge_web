def test_lookup_forms_show_targeted_tooltips(client):
    haulier_response = client.get("/lookups/hauliers/new")
    assert haulier_response.status_code == 200
    assert 'id="haulier-carrier-licence-help"' in haulier_response.text
    assert '<div class="hint">' not in haulier_response.text

    destination_response = client.get("/lookups/destinations/new")
    assert destination_response.status_code == 200
    assert 'id="destination-meaning-help"' in destination_response.text
    assert '<div class="hint">' not in destination_response.text


def test_lookup_list_page_has_no_form_tooltips(client):
    response = client.get("/lookups/hauliers")
    assert response.status_code == 200
    assert 'id="haulier-carrier-licence-help"' not in response.text
    assert 'id="destination-meaning-help"' not in response.text
    assert 'id="container-usage-help"' not in response.text
    assert 'id="driver-linked-haulier-help"' not in response.text
