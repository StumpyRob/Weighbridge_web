from __future__ import annotations

import os
from pathlib import Path
import subprocess
from typing import TypedDict


class BuildInfo(TypedDict):
    version: str
    commit_short: str
    label: str


_REPO_ROOT = Path(__file__).resolve().parents[1]
_VERSION_FILE = _REPO_ROOT / "VERSION"
_COMMIT_ENV_KEYS = (
    "APP_COMMIT_SHA",
    "RAILWAY_GIT_COMMIT_SHA",
    "RAILWAY_GIT_COMMIT",
    "GIT_COMMIT",
    "SOURCE_VERSION",
)


def _load_version() -> str:
    try:
        file_version = _VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        file_version = ""
    if file_version:
        return file_version.lstrip("v")

    env_version = str(os.getenv("APP_VERSION", "") or "").strip()
    if env_version:
        return env_version.lstrip("v")
    return "0.0.0"


def _load_commit_short() -> str:
    for key in _COMMIT_ENV_KEYS:
        value = str(os.getenv(key, "") or "").strip()
        if value:
            return value[:7]

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            check=False,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    commit = str(result.stdout or "").strip()
    return commit[:7] if commit else "unknown"


def get_build_info() -> BuildInfo:
    version = _load_version()
    commit_short = _load_commit_short()
    return {
        "version": version,
        "commit_short": commit_short,
        "label": f"R.Hetherington Build v{version} (commit {commit_short})",
    }
