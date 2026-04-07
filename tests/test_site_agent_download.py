from app.config import settings
from app.services import site_agent_download


def test_site_agent_download_state_is_available_with_bucket_config(monkeypatch):
    monkeypatch.setattr(settings, "site_agent_download_url", "")
    monkeypatch.setattr(settings, "site_agent_download_bucket", "agent-downloads")
    monkeypatch.setattr(
        settings,
        "site_agent_download_object_key",
        "downloads/WeighbridgeSiteAgent-0.22-Setup.exe",
    )
    monkeypatch.setattr(settings, "site_agent_download_s3_endpoint", "https://bucket.example.test")
    monkeypatch.setattr(settings, "site_agent_download_access_key_id", "bucket-key")
    monkeypatch.setattr(settings, "site_agent_download_secret_access_key", "bucket-secret")

    state = site_agent_download.site_agent_download_state()

    assert state["available"] is True
    assert state["filename"] == "WeighbridgeSiteAgent-0.22-Setup.exe"
    assert state["source"] == "presigned_bucket"


def test_resolve_site_agent_download_url_generates_presigned_bucket_url(monkeypatch):
    monkeypatch.setattr(settings, "site_agent_download_url", "")
    monkeypatch.setattr(settings, "site_agent_download_bucket", "agent-downloads")
    monkeypatch.setattr(
        settings,
        "site_agent_download_object_key",
        "downloads/WeighbridgeSiteAgent-0.22-Setup.exe",
    )
    monkeypatch.setattr(settings, "site_agent_download_s3_endpoint", "https://bucket.example.test")
    monkeypatch.setattr(settings, "site_agent_download_s3_region", "us-west-1")
    monkeypatch.setattr(settings, "site_agent_download_access_key_id", "bucket-key")
    monkeypatch.setattr(settings, "site_agent_download_secret_access_key", "bucket-secret")
    monkeypatch.setattr(settings, "site_agent_download_presign_ttl_seconds", 1200)

    captured: dict[str, object] = {}

    class _FakeClient:
        def generate_presigned_url(self, ClientMethod, Params, ExpiresIn):
            captured["client_method"] = ClientMethod
            captured["params"] = dict(Params)
            captured["expires_in"] = ExpiresIn
            return "https://signed.example.test/download"

    class _FakeSession:
        def client(self, service_name, **kwargs):
            captured["service_name"] = service_name
            captured["client_kwargs"] = dict(kwargs)
            return _FakeClient()

    monkeypatch.setattr(site_agent_download.boto3.session, "Session", lambda: _FakeSession())

    url = site_agent_download.resolve_site_agent_download_url()

    assert url == "https://signed.example.test/download"
    assert captured["service_name"] == "s3"
    assert captured["client_method"] == "get_object"
    assert captured["params"] == {
        "Bucket": "agent-downloads",
        "Key": "downloads/WeighbridgeSiteAgent-0.22-Setup.exe",
    }
    assert captured["expires_in"] == 1200
