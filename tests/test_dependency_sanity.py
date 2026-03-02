from __future__ import annotations

import os
import subprocess
import sys


def test_app_main_import_succeeds_with_runtime_deps(tmp_path) -> None:
    db_path = tmp_path / "import-sanity.db"
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db_path}"
    env["DEV_MODE"] = "1"

    result = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
