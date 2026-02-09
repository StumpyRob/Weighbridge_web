from app.models import Driver


def test_lookup_list_shows_saved_banner_when_query_flag_present(client, db_session):
    driver = Driver(name="Saved Banner Driver", is_active=True)
    db_session.add(driver)
    db_session.commit()

    response = client.get("/lookups/drivers?saved=1")

    assert response.status_code == 200
    assert "Saved." in response.text


def test_lookup_list_hides_saved_banner_without_query_flag(client, db_session):
    driver = Driver(name="No Banner Driver", is_active=True)
    db_session.add(driver)
    db_session.commit()

    response = client.get("/lookups/drivers")

    assert response.status_code == 200
    assert "Saved." not in response.text
