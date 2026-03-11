from app.templating import templates


def test_settings_page_hides_platform_only_tools(client):
    response = client.get("/admin")
    assert response.status_code == 200
    assert "<h1>Settings</h1>" in response.text
    assert ">Settings<" in response.text
    assert "Setup & Configuration" in response.text
    assert "Operations" in response.text
    assert "Support" in response.text
    assert "Manage Company" in response.text
    assert "Manage EWC Codes" in response.text
    assert "View Audit Log" in response.text
    assert "Open Help" in response.text
    assert 'class="btn btn--secondary btn--sm" href="/admin/printing/destinations">Destinations' in response.text
    assert 'class="btn btn--secondary btn--sm" href="/admin/printing/templates">Templates' in response.text
    assert 'class="btn btn--secondary btn--sm" href="/admin/printing/jobs">Jobs' in response.text
    assert "Open Company" not in response.text
    assert "Open EWC Codes" not in response.text
    assert "Open Audit Log" not in response.text
    assert "Turn DEV Mode" not in response.text
    assert "System Status" not in response.text


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
        assert enable.headers["location"] == "/admin"
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
