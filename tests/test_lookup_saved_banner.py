from app.models import Driver


def test_lookup_list_shows_saved_toast_when_query_flag_present(client, db_session):
    driver = Driver(name="Saved Banner Driver", is_active=True)
    db_session.add(driver)
    db_session.commit()

    response = client.get("/lookups/drivers?saved=1")

    assert response.status_code == 200
    assert '<div id="flash-toasts" class="flash-toasts" aria-live="polite">' in response.text
    assert 'class="flash-toast flash-toast--success"' in response.text
    assert 'data-flash-success="1"' in response.text
    assert "<div class=\"alert alert-success\"" not in response.text


def test_lookup_list_hides_saved_toast_without_query_flag(client, db_session):
    driver = Driver(name="No Banner Driver", is_active=True)
    db_session.add(driver)
    db_session.commit()

    response = client.get("/lookups/drivers")

    assert response.status_code == 200
    assert 'id="flash-toasts"' not in response.text
    assert 'class="flash-toast flash-toast--success"' not in response.text
