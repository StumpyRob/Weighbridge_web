from __future__ import annotations

import base64
from dataclasses import dataclass
import socket
import subprocess
from typing import Any, Literal

import httpx

from ..config import settings

PrintMode = Literal["usb", "network", "cups", "local_browser", "local_node_http"]
NODE_HTTP_TEXT_MIME_TYPE = "text/plain; charset=utf-8"


@dataclass(slots=True)
class PrintTransportResult:
    provider_job_ref: str | None = None
    provider_response_json: dict[str, Any] | None = None


class PrintNodeHttpError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        provider_job_ref: str | None = None,
        provider_response_json: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.provider_job_ref = provider_job_ref
        self.provider_response_json = provider_response_json


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


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
    job: bytes,
    config: dict,
    *,
    document_type: str,
    job_id: int,
    job_name: str,
    document_filename: str,
    payload_format: str,
    payload_mime_type: str,
) -> PrintTransportResult:
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

    printer_name = str(config.get("printer_name", "")).strip()
    printer_role = str(config.get("printer_role", "")).strip()
    if not printer_name and not printer_role:
        raise ValueError("LOCAL_NODE_HTTP printer_name or printer_role is required.")

    copies_raw = config.get("copies", 1)
    try:
        copies = int(copies_raw)
    except (TypeError, ValueError):
        raise ValueError("LOCAL_NODE_HTTP copies must be an integer.")
    if copies < 1:
        raise ValueError("LOCAL_NODE_HTTP copies must be >= 1.")

    normalized_payload_format = str(payload_format or "").strip().upper()
    resolved_job_name = (
        str(job_name or "").strip() or f"{str(document_type or '').strip().upper() or 'PRINT'} print job {job_id}"
    )
    resolved_payload_mime_type = (
        str(payload_mime_type or "").strip()
        or (NODE_HTTP_TEXT_MIME_TYPE if normalized_payload_format == "TEXT" else "")
        or "application/octet-stream"
    )
    default_extension = "txt"
    if normalized_payload_format == "PDF":
        default_extension = "pdf"
    elif normalized_payload_format == "RAW":
        default_extension = "bin"
    payload = {
        "job_id": str(int(job_id or 0)),
        "document_type": document_type,
        "document_filename": str(document_filename or "").strip()
        or f"{str(document_type or '').strip().upper() or 'PRINT'}-{int(job_id or 0)}.{default_extension}",
        "job_name": resolved_job_name,
        "copies": copies,
        "payload_format": normalized_payload_format,
        "payload_mime_type": resolved_payload_mime_type,
        "payload_base64": base64.b64encode(job).decode("ascii"),
    }
    if printer_name:
        payload["printer_name"] = printer_name
    if printer_role:
        payload["printer_role"] = printer_role
    try:
        response = httpx.post(url, json=payload, headers=headers, timeout=timeout_seconds)
    except httpx.TimeoutException as exc:
        raise RuntimeError(f"Print node timeout after {timeout_ms}ms: {url}") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Print node request failed: {url} ({exc})") from exc

    provider_response_json: dict[str, Any] | None = None
    provider_job_ref: str | None = None
    try:
        parsed = response.json()
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        provider_response_json = parsed
        provider_job_ref = _optional_text(parsed.get("provider_job_ref"))

    if response.status_code < 200 or response.status_code >= 300:
        body = response.text.strip()
        message = (
            _optional_text(provider_response_json.get("message"))
            if provider_response_json is not None
            else None
        )
        if not message:
            message = body[:500] if body else f"HTTP {response.status_code}"
        raise PrintNodeHttpError(
            f"Print node rejected job: {message}",
            provider_job_ref=provider_job_ref,
            provider_response_json=provider_response_json,
        )

    if provider_response_json is None:
        raise PrintNodeHttpError("Print node returned invalid JSON response.")

    if "ok" not in provider_response_json:
        raise PrintNodeHttpError(
            "Print node response missing ok flag.",
            provider_job_ref=provider_job_ref,
            provider_response_json=provider_response_json,
        )

    if provider_response_json.get("ok") is not True:
        message = (
            _optional_text(provider_response_json.get("message"))
            or "Print node returned ok=false."
        )
        raise PrintNodeHttpError(
            f"Print node rejected job: {message}",
            provider_job_ref=provider_job_ref,
            provider_response_json=provider_response_json,
        )

    return PrintTransportResult(
        provider_job_ref=provider_job_ref,
        provider_response_json=provider_response_json,
    )


def send(
    job: bytes,
    mode: PrintMode,
    config: dict,
    *,
    document_type: str = "",
    rendered_content: str = "",
    content_type: str = "TEXT",
    job_id: int = 0,
    job_name: str = "",
    document_filename: str = "",
    payload_format: str = "",
    payload_mime_type: str = "",
) -> bytes | PrintTransportResult | None:
    if mode == "network":
        _send_network(job, config)
        return None
    if mode in {"cups", "usb"}:
        _send_cups(job, config)
        return None
    if mode == "local_browser":
        return job
    if mode == "local_node_http":
        resolved_payload_format = str(payload_format or content_type or "").strip().upper() or "TEXT"
        resolved_payload_mime_type = str(payload_mime_type or "").strip() or (
            NODE_HTTP_TEXT_MIME_TYPE if resolved_payload_format == "TEXT" else ""
        )
        return _send_local_node_http(
            job,
            config,
            document_type=document_type,
            job_id=job_id,
            job_name=job_name,
            document_filename=document_filename,
            payload_format=resolved_payload_format,
            payload_mime_type=resolved_payload_mime_type,
        )
    raise ValueError(f"Unsupported print mode: {mode}")
