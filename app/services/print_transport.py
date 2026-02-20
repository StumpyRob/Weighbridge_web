from __future__ import annotations

import socket
import subprocess
from typing import Literal

from ..config import settings

PrintMode = Literal["usb", "network", "cups"]


def _send_network(job: bytes, config: dict) -> None:
    if not settings.print_network_enabled:
        raise RuntimeError("Network printing is disabled by configuration.")

    host = str(config.get("host", "")).strip()
    if not host:
        raise ValueError("Network print host is required.")

    port = int(config.get("port", 9100))
    timeout_seconds = float(config.get("timeout_seconds", 5))

    with socket.create_connection((host, port), timeout=timeout_seconds) as conn:
        conn.sendall(job)


def _send_cups(job: bytes, config: dict) -> None:
    printer_name = str(config.get("printer_name", "")).strip()
    if not printer_name:
        raise ValueError("CUPS printer_name is required.")

    title = str(config.get("job_name", "Weighbridge Ticket")).strip() or "Print Job"
    command = ["lp", "-d", printer_name, "-t", title]

    options = config.get("options")
    if isinstance(options, dict):
        for key, value in options.items():
            option_name = str(key).strip()
            option_value = str(value).strip()
            if option_name and option_value:
                command.extend(["-o", f"{option_name}={option_value}"])

    try:
        result = subprocess.run(
            command,
            input=job,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("CUPS print command 'lp' is not available on this host.") from exc

    if result.returncode != 0:
        stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(stderr or "CUPS print failed.")


def send(job: bytes, mode: PrintMode, config: dict) -> None:
    if mode == "network":
        _send_network(job, config)
        return
    if mode in {"cups", "usb"}:
        _send_cups(job, config)
        return
    raise ValueError(f"Unsupported print mode: {mode}")
