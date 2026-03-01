from fastapi.testclient import TestClient

from app.config import settings
from app.main import create_app


def test_routes_endpoint_is_not_available_when_dev_mode_disabled(monkeypatch):
    monkeypatch.setattr(settings, "app_secret_key", "prod-surface-test-secret")
    monkeypatch.setattr(settings, "secret_key", "")
    app = create_app(dev_mode=False)
    with TestClient(app) as client:
        response = client.get("/__routes")
    assert response.status_code == 404


def test_no_dev_or_debug_routes_when_dev_mode_disabled(monkeypatch):
    monkeypatch.setattr(settings, "app_secret_key", "prod-surface-test-secret")
    monkeypatch.setattr(settings, "secret_key", "")
    app = create_app(dev_mode=False)
    route_paths = [getattr(route, "path", "").lower() for route in app.routes]

    for path in route_paths:
        assert "debug" not in path
        assert "__" not in path
        assert "dev" not in path
