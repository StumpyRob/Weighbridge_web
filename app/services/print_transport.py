from __future__ import annotations

import socket
import subprocess
from typing import Literal

import httpx

from ..config import settings

PrintMode = Literal["usb", "network", "cups", "local_browser", "local_node_http"]


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


def _send_local_node_http(
    config: dict,
    *,
    purpose: str,
    rendered_content: str,
    content_type: str,
    job_id: int,
) -> None:
    url = str(config.get("url", "")).strip()
    if not url:
        raise ValueError("LOCAL_NODE_HTTP url is required.")

    timeout_ms_raw = config.get("timeout_ms", 5000)
    try:
        timeout_ms = int(timeout_ms_raw)
    except (TypeError, ValueError):
        raise ValueError("LOCAL_NODE_HTTP timeout_ms must be an integer.")
    timeout_seconds = max(timeout_ms, 1) / 1000.0

    api_key = str(config.get("api_key", "")).strip()
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key

    payload = {
        "purpose": purpose,
        "rendered_content": rendered_content,
        "content_type": content_type,
        "job_id": job_id,
    }
    try:
        response = httpx.post(url, json=payload, headers=headers, timeout=timeout_seconds)
    except httpx.TimeoutException as exc:
        raise RuntimeError(f"Print node timeout after {timeout_ms}ms: {url}") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Print node request failed: {url} ({exc})") from exc

    if response.status_code >= 400:
        body = response.text.strip()
        message = body[:500] if body else f"HTTP {response.status_code}"
        raise RuntimeError(f"Print node rejected job: {message}")


def send(
    job: bytes,
    mode: PrintMode,
    config: dict,
    *,
    purpose: str = "",
    rendered_content: str = "",
    content_type: str = "TEXT",
    job_id: int = 0,
) -> bytes | None:
    if mode == "network":
        _send_network(job, config)
        return None
    if mode in {"cups", "usb"}:
        _send_cups(job, config)
        return None
    if mode == "local_browser":
        return job
    if mode == "local_node_http":
        _send_local_node_http(
            config,
            purpose=purpose,
            rendered_content=rendered_content,
            content_type=content_type,
            job_id=job_id,
        )
        return None
    raise ValueError(f"Unsupported print mode: {mode}")
