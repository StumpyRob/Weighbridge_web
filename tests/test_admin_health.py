from app.config import settings
from app.templating import templates


def test_admin_health_route_returns_200_in_dev_mode(client):
    original_dev_mode = templates.env.globals.get("DEV_MODE", False)
    templates.env.globals["DEV_MODE"] = True
    try:
        response = client.get("/admin/health")
    finally:
        templates.env.globals["DEV_MODE"] = original_dev_mode

    assert response.status_code == 200
    assert "System Health" in response.text


def test_admin_health_route_hidden_when_dev_mode_disabled(client, monkeypatch):
    monkeypatch.setattr(settings, "dev_mode", False)
    monkeypatch.setattr(settings, "debug", False)
    original_dev_mode = templates.env.globals.get("DEV_MODE", False)
    templates.env.globals["DEV_MODE"] = False
    try:
        response = client.get("/admin/health")
    finally:
        templates.env.globals["DEV_MODE"] = original_dev_mode

    assert response.status_code == 404
