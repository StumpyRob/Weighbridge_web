from pathlib import Path

from app import build_info


def test_load_version_prefers_version_file_over_env(monkeypatch, tmp_path):
    version_file = tmp_path / "VERSION"
    version_file.write_text("0.19.5\n", encoding="utf-8")
    monkeypatch.setattr(build_info, "_VERSION_FILE", Path(version_file))
    monkeypatch.setenv("APP_VERSION", "0.19.1")

    assert build_info._load_version() == "0.19.5"


def test_load_version_falls_back_to_env_when_version_file_missing(monkeypatch, tmp_path):
    version_file = tmp_path / "MISSING_VERSION"
    monkeypatch.setattr(build_info, "_VERSION_FILE", Path(version_file))
    monkeypatch.setenv("APP_VERSION", "0.19.5")

    assert build_info._load_version() == "0.19.5"
