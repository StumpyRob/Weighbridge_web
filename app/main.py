import logging
from contextlib import contextmanager
from pathlib import Path
import secrets
from typing import Iterator

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .auth import SESSION_USER_ID_KEY, is_superadmin_user
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
from .services.system_setup import get_company_setting
from .services.pdf import check_invoice_pdf_renderer
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
    media_dir = Path(settings.media_root).resolve()
    media_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=str(media_dir)), name="media")
    app.mount("/static", StaticFiles(directory="app/static"), name="static")

    def _path_needs_system_guard(path: str) -> bool:
        target = str(path or "")
        return any(target.startswith(prefix) for prefix in _SYSTEM_GUARD_PREFIXES)

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
                    if should_set_csrf_cookie:
                        set_csrf_cookie(response, request, csrf_cookie)
                    apply_security_headers(response)
                    return response

        response = await call_next(request)
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
        check_invoice_pdf_renderer()

    @app.get("/health", tags=["health"])
    def health_check() -> dict:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "index.html", {"request": request})

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
