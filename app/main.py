import logging
from contextlib import contextmanager
from pathlib import Path
import secrets
from typing import Iterator

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .auth import (
    SESSION_USER_ID_KEY,
    is_superadmin_user,
    require_user,
)
from .config import settings
from .db import get_db
from .models import User
from .routes import api_router
from .routers.lookups import router as lookups_router
from .security_hardening import (
    CSRF_COOKIE_NAME,
    CSRF_FORM_FIELD,
    CSRF_HEADER_NAME,
    apply_security_headers,
    csrf_forbidden_response,
    generate_csrf_token,
    is_state_changing_method,
    set_csrf_cookie,
    validate_production_secret,
)
from .services.system_setup import get_company_setting, missing_required_lookup_messages
from .services.pdf import check_invoice_pdf_renderer
from .services.ui_branding import get_branding, normalize_hex_color
from .templating import templates

logger = logging.getLogger(__name__)

_SYSTEM_GUARD_PREFIXES = (
    "/tickets",
    "/customers",
    "/vehicles",
    "/products",
    "/invoices",
    "/lookups",
)
_LOGIN_REQUIRED_PREFIXES = (
    "/tickets",
    "/customers",
    "/vehicles",
    "/products",
    "/invoices",
    "/lookups",
    "/admin",
)
_UPLOADS_STATIC_PREFIX = "/static/uploads/"


def _strip_non_production_routes(app: FastAPI) -> None:
    filtered_routes = []
    for route in app.router.routes:
        path = str(getattr(route, "path", "")).lower()
        if "debug" in path or "__" in path or "dev" in path:
            continue
        filtered_routes.append(route)
    app.router.routes = filtered_routes


def create_app(dev_mode: bool | None = None) -> FastAPI:
    effective_dev_mode = settings.dev_mode if dev_mode is None else dev_mode
    validate_production_secret(
        dev_mode=bool(effective_dev_mode),
        secret_key=settings.effective_secret_key,
    )

    app = FastAPI(title="weighbridge_web")

    app.include_router(api_router)
    app.include_router(lookups_router)

    if effective_dev_mode:
        from .routes.dev import router as dev_router

        app.include_router(dev_router)

    def _ensure_upload_dirs() -> tuple[Path, Path]:
        uploads_root = Path(str(settings.effective_uploads_dir or "").strip()).resolve()
        uploads_root.mkdir(parents=True, exist_ok=True)
        company_dir = (uploads_root / "company").resolve()
        company_dir.mkdir(parents=True, exist_ok=True)
        return uploads_root, company_dir

    uploads_root, _ = _ensure_upload_dirs()
    media_dir = Path(settings.media_root).resolve()
    media_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=str(media_dir)), name="media")
    app.mount("/static/uploads", StaticFiles(directory=str(uploads_root)), name="uploads")
    app.mount("/static", StaticFiles(directory="app/static"), name="static")

    def _path_needs_system_guard(path: str) -> bool:
        target = str(path or "")
        return any(target.startswith(prefix) for prefix in _SYSTEM_GUARD_PREFIXES)

    def _path_requires_login(path: str) -> bool:
        target = str(path or "")
        return any(target.startswith(prefix) for prefix in _LOGIN_REQUIRED_PREFIXES)

    def _apply_cache_control_headers(path: str, response: Response) -> None:
        request_path = str(path or "")
        if request_path.startswith(_UPLOADS_STATIC_PREFIX):
            response.headers["Cache-Control"] = "public, max-age=86400"
            return

        content_type = str(response.headers.get("content-type", "")).lower()
        if "text/html" in content_type:
            response.headers["Cache-Control"] = "no-store"

    def _maybe_brand_plain_error_response(request: Request, response: Response) -> Response:
        if request.method not in {"GET", "HEAD"}:
            return response

        status_code = int(response.status_code or 0)
        template_name = {
            403: "errors/403.html",
            404: "errors/404.html",
            500: "errors/500.html",
        }.get(status_code)
        if template_name is None:
            return response

        content_type = str(response.headers.get("content-type", "")).lower()
        if "text/plain" not in content_type and "text/html" not in content_type:
            return response

        body = getattr(response, "body", b"")
        if not isinstance(body, (bytes, bytearray)):
            return response
        normalized = str(body.decode("utf-8", errors="ignore") or "").strip().lower()
        plain_error_payloads = {
            "forbidden",
            "not found",
            "internal server error",
            "<h1>internal server error</h1>",
        }
        if normalized not in plain_error_payloads:
            return response

        return templates.TemplateResponse(
            request,
            template_name,
            {"request": request},
            status_code=status_code,
        )

    @contextmanager
    def _request_db(request: Request) -> Iterator:
        dep = request.app.dependency_overrides.get(get_db, get_db)
        db_gen = dep()
        db = next(db_gen)
        try:
            yield db
        finally:
            try:
                next(db_gen)
            except StopIteration:
                pass

    def _load_session_user(request: Request, db) -> User | None:
        user_id = request.session.get(SESSION_USER_ID_KEY)
        if user_id is None:
            return None
        try:
            parsed_user_id = int(user_id)
        except (TypeError, ValueError):
            request.session.pop(SESSION_USER_ID_KEY, None)
            return None

        user = db.get(User, parsed_user_id)
        if user is None or not bool(user.is_active):
            request.session.pop(SESSION_USER_ID_KEY, None)
            return None
        return user

    def _uninitialized_response(request: Request, *, superadmin: bool) -> HTMLResponse:
        message = "System not initialized. Please contact your administrator."
        if superadmin:
            message = "System not initialized. Visit /setup (superadmin)."
        return templates.TemplateResponse(
            request,
            "system/uninitialized.html",
            {
                "request": request,
                "is_superadmin": superadmin,
                "message": message,
            },
            status_code=503,
        )

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        csrf_cookie = request.cookies.get(CSRF_COOKIE_NAME)
        should_set_csrf_cookie = False
        if not csrf_cookie:
            csrf_cookie = generate_csrf_token()
            should_set_csrf_cookie = True
        request.state.csrf_token = csrf_cookie
        request.state.current_user = None

        request_path = str(request.url.path or "")
        with _request_db(request) as db:
            request.state.current_user = _load_session_user(request, db)

        if _path_requires_login(request_path):
            authenticated = require_user(request)
            if not isinstance(authenticated, User):
                response = authenticated
                _apply_cache_control_headers(request_path, response)
                if should_set_csrf_cookie:
                    set_csrf_cookie(response, request, csrf_cookie)
                apply_security_headers(response)
                return response

        if is_state_changing_method(request.method):
            submitted_token = str(request.headers.get(CSRF_HEADER_NAME, "")).strip()
            if not submitted_token:
                content_type = str(request.headers.get("content-type", "")).lower()
                if content_type.startswith(
                    "application/x-www-form-urlencoded"
                ) or content_type.startswith("multipart/form-data"):
                    body = await request.body()

                    def _receive_with_body(payload: bytes):
                        sent = False

                        async def _inner():
                            nonlocal sent
                            if sent:
                                return {
                                    "type": "http.request",
                                    "body": b"",
                                    "more_body": False,
                                }
                            sent = True
                            return {
                                "type": "http.request",
                                "body": payload,
                                "more_body": False,
                            }

                        return _inner

                    form = None
                    try:
                        form_request = Request(request.scope, _receive_with_body(body))
                        form = await form_request.form()
                    except Exception:
                        form = None
                    submitted_token = str(form.get(CSRF_FORM_FIELD, "")).strip() if form else ""
                    request._receive = _receive_with_body(body)
            if not submitted_token or not csrf_cookie or not secrets.compare_digest(
                submitted_token, csrf_cookie
            ):
                response = csrf_forbidden_response(request)
                _apply_cache_control_headers(request_path, response)
                set_csrf_cookie(response, request, csrf_cookie)
                apply_security_headers(response)
                return response

        if _path_needs_system_guard(request_path):
            with _request_db(request) as db:
                company = get_company_setting(db)
                if not bool(company and getattr(company, "is_initialized", False)):
                    response = _uninitialized_response(
                        request,
                        superadmin=is_superadmin_user(
                            db, getattr(request.state, "current_user", None)
                        ),
                    )
                    _apply_cache_control_headers(request_path, response)
                    if should_set_csrf_cookie:
                        set_csrf_cookie(response, request, csrf_cookie)
                    apply_security_headers(response)
                    return response

        response = await call_next(request)
        response = _maybe_brand_plain_error_response(request, response)
        _apply_cache_control_headers(request_path, response)
        if should_set_csrf_cookie:
            set_csrf_cookie(response, request, csrf_cookie)
        apply_security_headers(response)
        return response

    session_secret = str(settings.effective_secret_key or "").strip() or "dev-session-secret"
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        same_site="lax",
        https_only=not bool(effective_dev_mode),
    )

    @app.on_event("startup")
    def startup_printing_bootstrap() -> None:
        _ensure_upload_dirs()
        check_invoice_pdf_renderer()

    @app.get("/health", tags=["health"])
    def health_check() -> dict:
        return {"status": "ok"}

    @app.get("/branding.css", include_in_schema=False)
    def branding_css(db: Session = Depends(get_db)) -> PlainTextResponse:
        def _nav_foreground_color(value: str) -> str:
            normalized = normalize_hex_color(value, default="#14213D")
            rgb_hex = normalized.lstrip("#")
            try:
                red = int(rgb_hex[0:2], 16)
                green = int(rgb_hex[2:4], 16)
                blue = int(rgb_hex[4:6], 16)
            except (TypeError, ValueError):
                return "#FFFFFF"
            luminance = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255
            return "#FFFFFF" if luminance < 0.58 else "#14213D"

        branding = get_branding(db)
        nav_color = normalize_hex_color(branding.get("nav_color", ""), default="#14213D")
        nav_foreground = _nav_foreground_color(nav_color)
        primary_color = str(branding.get("primary_color", "") or "#FCA311")
        primary_contrast = str(branding.get("primary_contrast_hex", "") or "#111827")
        primary_soft = str(branding.get("primary_soft_rgba", "") or "rgba(252, 163, 17, 0.16)")
        try:
            nav_logo_height = int(branding.get("nav_logo_height_px", 34) or 34)
        except (TypeError, ValueError):
            nav_logo_height = 34
        nav_logo_height = max(20, min(80, nav_logo_height))

        css = (
            ":root {\n"
            f"  --theme-navbar-bg: {nav_color};\n"
            f"  --theme-primary: {primary_color};\n"
            f"  --theme-primary-contrast: {primary_contrast};\n"
            f"  --theme-primary-soft: {primary_soft};\n"
            f"  --theme-nav-logo-height: {nav_logo_height}px;\n"
            f"  --nav-bg: {nav_color};\n"
            f"  --nav-fg: {nav_foreground};\n"
            f"  --primary: {primary_color};\n"
            "}\n"
        )
        response = PlainTextResponse(css, media_type="text/css")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
        company = get_company_setting(db)
        initialized = bool(company and getattr(company, "is_initialized", False))
        missing_required = missing_required_lookup_messages(db) if initialized else []
        user_count = int(db.execute(select(func.count(User.id))).scalar_one_or_none() or 0)
        setup_ready = initialized and len(missing_required) == 0
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "request": request,
                "show_first_time_setup": not setup_ready,
                "setup_ready": setup_ready,
                "setup_initialized": initialized,
                "missing_required_lookups": missing_required,
                "needs_first_admin": user_count == 0,
            },
        )

    @app.get("/reports", response_class=HTMLResponse)
    def reports(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "reports.html", {"request": request})

    @app.get("/admin", response_class=HTMLResponse)
    def admin(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "admin.html", {"request": request})

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.exception("Unhandled server error", exc_info=exc)
        accept = str(request.headers.get("accept", "")).lower()
        if "application/json" in accept:
            return JSONResponse(
                {"detail": "Internal Server Error"},
                status_code=500,
            )
        return HTMLResponse("<h1>Internal Server Error</h1>", status_code=500)

    if dev_mode is False:
        _strip_non_production_routes(app)

    return app

app = create_app()
