from app.templating import templates


def test_admin_page_shows_dev_mode_toggle(client):
    response = client.get("/admin")
    assert response.status_code == 200
    assert "Turn DEV Mode" in response.text


def test_admin_dev_mode_toggle_updates_runtime_flag(client):
    original = templates.env.globals.get("DEV_MODE", False)
    try:
        templates.env.globals["DEV_MODE"] = False
        enable = client.post(
            "/admin/dev-mode",
            data={"enabled": "1"},
            follow_redirects=False,
        )
        assert enable.status_code == 303
        assert "dev_mode_updated=1" in enable.headers["location"]
        assert templates.env.globals.get("DEV_MODE") is True

        disable = client.post(
            "/admin/dev-mode",
            data={"enabled": "0"},
            follow_redirects=False,
        )
        assert disable.status_code == 303
        assert templates.env.globals.get("DEV_MODE") is False
    finally:
        templates.env.globals["DEV_MODE"] = original
