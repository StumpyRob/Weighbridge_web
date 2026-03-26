from __future__ import annotations

import secrets

from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

CSRF_COOKIE_NAME = "wb_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"
CSRF_FORM_FIELD = "csrf_token"

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
_WEAK_SECRET_VALUES = {
    "change-me",
    "changeme",
    "default",
    "password",
    "secret",
    "test-secret",
}
_QZ_TRAY_CONNECT_SOURCES = (
    "wss://localhost:8181",
    "wss://localhost:8282",
    "wss://localhost:8383",
    "wss://localhost:8484",
    "wss://localhost.qz.io:8181",
    "wss://localhost.qz.io:8282",
    "wss://localhost.qz.io:8383",
    "wss://localhost.qz.io:8484",
)
_CSP_CONNECT_SRC = (
    "connect-src 'self' https://www.google-analytics.com "
    + " ".join(_QZ_TRAY_CONNECT_SOURCES)
    + "; "
)


def is_state_changing_method(method: str | None) -> bool:
    return str(method or "").upper() not in _SAFE_METHODS


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def is_https_request(request: Request) -> bool:
    forwarded_proto = (
        str(request.headers.get("x-forwarded-proto", "")).split(",")[0].strip().lower()
    )
    if forwarded_proto == "https":
        return True
    return str(request.url.scheme).lower() == "https"


def set_csrf_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=token,
        path="/",
        httponly=True,
        secure=is_https_request(request),
        samesite="lax",
    )


def csrf_forbidden_response(request: Request) -> Response:
    accept = str(request.headers.get("accept", "")).lower()
    if "application/json" in accept:
        return JSONResponse({"detail": "CSRF validation failed."}, status_code=403)
    return PlainTextResponse("CSRF validation failed.", status_code=403)


def apply_security_headers(response: Response) -> None:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Permissions-Policy",
        "geolocation=(), camera=(), microphone=()",
    )
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault(
        "Content-Security-Policy",
        (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://unpkg.com; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "font-src 'self' data:; "
            f"{_CSP_CONNECT_SRC}"
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'"
        ),
    )


def validate_production_secret(
    *,
    dev_mode: bool,
    secret_key: str | None,
) -> None:
    if dev_mode:
        return
    candidate = str(secret_key or "").strip()
    if not candidate:
        raise RuntimeError(
            "APP_SECRET_KEY (or SECRET_KEY) must be configured when DEV_MODE is disabled."
        )
    if candidate.lower() in _WEAK_SECRET_VALUES:
        raise RuntimeError(
            "APP_SECRET_KEY (or SECRET_KEY) is using a weak default value."
        )

