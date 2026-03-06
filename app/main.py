import logging
from contextlib import contextmanager
from pathlib import Path
import secrets
from typing import Iterator

from fastapi import Depends, FastAPI, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .auth import (
    ROLE_SUPERADMIN,
    SESSION_PLATFORM_MODE_KEY,
    SESSION_ROLE_KEY,
    SESSION_TENANT_ID_KEY,
    SESSION_USER_ID_KEY,
    ensure_user_role,
    is_superadmin_user,
    require_user,
)
from .config import settings
from .db import get_db
from .models import Tenant, User
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
from .services.tenants import ensure_default_tenant, get_tenant_by_subdomain
from .services.uploads import company_logo_upload_dir
from .services.pdf import check_invoice_pdf_renderer
from .services.ui_branding import get_branding, nav_foreground_color, normalize_hex_color
from .tenancy import (
    host_without_port,
    prefix_tenant_route_target,
    reset_request_tenant_context,
    resolve_subdomain,
    set_request_tenant_context,
    split_tenant_route_path,
    tenant_route_prefix,
)
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
    "/platform",
)
_LOGIN_EXEMPT_PATHS = (
    "/bootstrap",
    "/platform/bootstrap",
)
_UPLOADS_STATIC_PREFIX = "/static/uploads/"
_TENANT_ONLY_PREFIXES = (
    "/tickets",
    "/customers",
    "/vehicles",
    "/products",
    "/invoices",
    "/lookups",
    "/setup",
    "/admin/company",
    "/admin/printing",
)
_PLATFORM_ONLY_PREFIXES = (
    "/platform",
    "/admin/tenants",
    "/admin/ewc-codes",
    "/bootstrap",
)
_LEGACY_SINGLE_HOSTS = {
    "localhost",
    "127.0.0.1",
    "testserver",
}


class _StaticFilesWithoutSharedUploads(StaticFiles):
    async def get_response(self, path: str, scope) -> Response:
        normalized = str(path or "").replace("\\", "/").lstrip("/").lower()
        if normalized == "uploads/company" or normalized.startswith("uploads/company/"):
            return PlainTextResponse("Not Found", status_code=404)
        return await super().get_response(path, scope)


def _strip_non_production_routes(app: FastAPI) -> None:
    filtered_routes = []
    for route in app.router.routes:
        path = str(getattr(route, "path", "")).lower()
        if "debug" in path or "__" in path or "dev" in path:
            continue
        filtered_routes.append(route)
    app.router.routes = filtered_routes


def _is_exact_base_domain(host_name: str) -> bool:
    base_domain = settings.effective_base_domain
    return bool(base_domain and host_name == base_domain)


def _request_scope_path(request: Request) -> str:
    return str(request.scope.get("path", "") or "")


def _apply_tenant_route_redirect_prefix(request: Request, response: Response) -> Response:
    route_prefix = str(getattr(getattr(request, "state", None), "tenant_route_prefix", "") or "").strip()
    if not route_prefix:
        return response
    location = str(response.headers.get("location", "") or "").strip()
    if not location:
        return response
    scoped_location = prefix_tenant_route_target(route_prefix, location)
    if scoped_location and scoped_location != location:
        response.headers["location"] = scoped_location
    return response


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

    def _tenant_company_logo_file(request: Request, filename: str) -> Response:
        if bool(getattr(request.state, "platform_mode", False)):
            return PlainTextResponse("Not Found", status_code=404)
        tenant_id = getattr(request.state, "tenant_id", None)
        if tenant_id is None:
            return PlainTextResponse("Not Found", status_code=404)

        raw_name = str(filename or "").strip()
        if not raw_name:
            return PlainTextResponse("Not Found", status_code=404)
        if "/" in raw_name or "\\" in raw_name:
            return PlainTextResponse("Not Found", status_code=404)
        safe_name = Path(raw_name).name
        if not safe_name or safe_name in {".", ".."} or safe_name != raw_name:
            return PlainTextResponse("Not Found", status_code=404)

        tenant_logo_dir = company_logo_upload_dir(int(tenant_id), create=False).resolve()
        logo_path = (tenant_logo_dir / safe_name).resolve()
        try:
            logo_path.relative_to(tenant_logo_dir)
        except ValueError:
            return PlainTextResponse("Not Found", status_code=404)
        if not logo_path.is_file():
            return PlainTextResponse("Not Found", status_code=404)
        return FileResponse(str(logo_path))

    app.add_api_route(
        "/static/uploads/company/{filename:path}",
        _tenant_company_logo_file,
        methods=["GET"],
        include_in_schema=False,
    )

    def _ensure_upload_dirs() -> Path:
        uploads_root = Path(str(settings.effective_uploads_dir or "").strip()).resolve()
        uploads_root.mkdir(parents=True, exist_ok=True)
        return uploads_root

    _ensure_upload_dirs()
    media_dir = Path(settings.media_root).resolve()
    media_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=str(media_dir)), name="media")
    app.mount("/static", _StaticFilesWithoutSharedUploads(directory="app/static"), name="static")

    def _path_needs_system_guard(path: str) -> bool:
        target = str(path or "")
        return any(target.startswith(prefix) for prefix in _SYSTEM_GUARD_PREFIXES)

    def _path_requires_login(path: str) -> bool:
        target = str(path or "")
        if target in _LOGIN_EXEMPT_PATHS:
            return False
        return any(target.startswith(prefix) for prefix in _LOGIN_REQUIRED_PREFIXES)

    def _apply_cache_control_headers(path: str, response: Response) -> None:
        request_path = str(path or "")
        if request_path.startswith(_UPLOADS_STATIC_PREFIX):
            response.headers["Cache-Control"] = "public, max-age=86400"
            return

        content_type = str(response.headers.get("content-type", "")).lower()
        if "text/html" in content_type:
            response.headers["Cache-Control"] = "no-store"

    def _request_host_value(request: Request) -> str:
        host_value = str(request.headers.get("host", "") or request.url.hostname or "")
        if settings.effective_trust_forwarded_host:
            forwarded_host = str(request.headers.get("x-forwarded-host", "") or "").strip()
            if forwarded_host:
                host_value = forwarded_host.split(",", 1)[0].strip()
        return host_value

    def _apex_platform_path_mode(request: Request, host_name: str) -> bool:
        if not _is_exact_base_domain(host_name):
            return False

        request_path = _request_scope_path(request)
        if request_path.startswith("/platform"):
            request.session[SESSION_PLATFORM_MODE_KEY] = True
            return True

        if request_path != "/login":
            return False

        if request.method == "GET":
            next_hint = str(request.query_params.get("next", "") or "").strip()
            if next_hint.startswith("/platform"):
                request.session[SESSION_PLATFORM_MODE_KEY] = True
                return True
            request.session.pop(SESSION_PLATFORM_MODE_KEY, None)
            return False

        if request.method == "POST":
            return bool(request.session.get(SESSION_PLATFORM_MODE_KEY))

        return False

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

        platform_mode = bool(getattr(request.state, "platform_mode", False))
        legacy_single_host = bool(getattr(request.state, "legacy_single_host", False))

        user = db.get(User, parsed_user_id)
        if user is None and (not platform_mode) and legacy_single_host:
            user = (
                db.execute(
                    select(User)
                    .execution_options(skip_tenant_scope=True)
                    .where(User.id == parsed_user_id)
                )
                .scalars()
                .first()
            )
        if user is None or not bool(user.is_active):
            request.session.pop(SESSION_USER_ID_KEY, None)
            return None

        role = ensure_user_role(db, user, allow_bootstrap=True)
        request_tenant_id = getattr(request.state, "tenant_id", None)
        session_tenant_id = request.session.get(SESSION_TENANT_ID_KEY)
        if session_tenant_id in ("", None):
            session_tenant_id = None
        elif isinstance(session_tenant_id, str) and session_tenant_id.isdigit():
            session_tenant_id = int(session_tenant_id)

        if platform_mode:
            if role != ROLE_SUPERADMIN or getattr(user, "tenant_id", None) is not None:
                request.session.clear()
                return None
            request.session[SESSION_PLATFORM_MODE_KEY] = True
            request.session[SESSION_TENANT_ID_KEY] = None
            request.session[SESSION_ROLE_KEY] = role
            return user

        if request_tenant_id is None:
            request.session.clear()
            return None
        if role == ROLE_SUPERADMIN:
            if not legacy_single_host:
                request.session.clear()
                return None
            request.session[SESSION_PLATFORM_MODE_KEY] = False
            request.session[SESSION_TENANT_ID_KEY] = int(request_tenant_id)
            request.session[SESSION_ROLE_KEY] = role
            return user

        if session_tenant_id is not None and int(session_tenant_id) != int(request_tenant_id):
            request.session.clear()
            return None

        if getattr(user, "tenant_id", None) is None:
            if not legacy_single_host:
                request.session.clear()
                return None
            user.tenant_id = int(request_tenant_id)
            db.commit()

        if int(getattr(user, "tenant_id", 0) or 0) != int(request_tenant_id):
            request.session.clear()
            return None

        request.session[SESSION_PLATFORM_MODE_KEY] = False
        request.session[SESSION_TENANT_ID_KEY] = int(request_tenant_id)
        request.session[SESSION_ROLE_KEY] = role
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
        request.state.tenant = None
        request.state.tenant_id = None
        request.state.platform_mode = False
        request.state.request_subdomain = ""
        request.state.tenant_route_prefix = ""
        request.state.legacy_single_host = False

        original_request_path = _request_scope_path(request)
        tenant_path_match = split_tenant_route_path(original_request_path)
        tenant_path_subdomain = ""
        if tenant_path_match is not None:
            tenant_path_subdomain, stripped_path = tenant_path_match
            request.state.tenant_route_prefix = tenant_route_prefix(tenant_path_subdomain)
            request.scope["path"] = stripped_path
            request.scope["raw_path"] = stripped_path.encode("utf-8")

        request_path = _request_scope_path(request)
        tenant_context_tokens = set_request_tenant_context(
            tenant_id=None,
            platform_mode=False,
        )

        def _finalize_response(response: Response, *, force_set_csrf: bool = False) -> Response:
            response = _apply_tenant_route_redirect_prefix(request, response)
            _apply_cache_control_headers(request_path, response)
            if should_set_csrf_cookie or force_set_csrf:
                set_csrf_cookie(response, request, csrf_cookie)
            apply_security_headers(response)
            return response

        def _plain_error(message: str, status_code: int) -> Response:
            return _finalize_response(PlainTextResponse(message, status_code=status_code))

        def _switch_tenant_context(*, tenant_id: int | None, platform_mode: bool) -> None:
            nonlocal tenant_context_tokens
            reset_request_tenant_context(tenant_context_tokens)
            tenant_context_tokens = set_request_tenant_context(
                tenant_id=tenant_id,
                platform_mode=platform_mode,
            )

        try:
            # 1) Resolve host + tenant/platform mode.
            with _request_db(request) as db:
                host_value = _request_host_value(request)
                host_name = host_without_port(host_value)
                request.state.legacy_single_host = host_name in _LEGACY_SINGLE_HOSTS
                allowed_hosts = settings.effective_allowed_hosts
                if allowed_hosts:
                    host_allowed = host_name in allowed_hosts or any(
                        host_name.endswith(f".{allowed}") for allowed in allowed_hosts
                    )
                    if not host_allowed:
                        return _plain_error("Unknown tenant", 404)
                if tenant_path_subdomain:
                    request.state.request_subdomain = tenant_path_subdomain
                    tenant = get_tenant_by_subdomain(db, tenant_path_subdomain)
                    if tenant is None:
                        return _plain_error("Unknown tenant", 404)
                    if not bool(tenant.is_active):
                        return _plain_error("Tenant disabled", 403)
                    request.state.tenant = tenant
                    request.state.tenant_id = int(tenant.id)
                    _switch_tenant_context(tenant_id=int(tenant.id), platform_mode=False)
                else:
                    subdomain = resolve_subdomain(host_value)
                    request.state.request_subdomain = subdomain

                    if _apex_platform_path_mode(request, host_name):
                        request.state.platform_mode = True
                        request.state.request_subdomain = settings.effective_platform_subdomain
                        _switch_tenant_context(tenant_id=None, platform_mode=True)
                    elif subdomain == settings.effective_platform_subdomain:
                        request.state.platform_mode = True
                        _switch_tenant_context(tenant_id=None, platform_mode=True)
                    else:
                        tenant = get_tenant_by_subdomain(db, subdomain)
                        if tenant is None and subdomain == settings.effective_default_tenant_subdomain:
                            tenant_count = int(
                                db.execute(select(func.count(Tenant.id))).scalar_one_or_none() or 0
                            )
                            if tenant_count == 0:
                                tenant = ensure_default_tenant(db)
                                db.commit()

                        if tenant is None:
                            return _plain_error("Unknown tenant", 404)
                        if not bool(tenant.is_active):
                            return _plain_error("Tenant disabled", 403)
                        request.state.tenant = tenant
                        request.state.tenant_id = int(tenant.id)
                        _switch_tenant_context(tenant_id=int(tenant.id), platform_mode=False)

            # 2) Enforce mode-specific route access.
            if request.state.platform_mode and any(
                request_path.startswith(prefix) for prefix in _TENANT_ONLY_PREFIXES
            ):
                return _plain_error("Unknown tenant", 404)

            platform_only = any(request_path.startswith(prefix) for prefix in _PLATFORM_ONLY_PREFIXES)
            allow_legacy_bootstrap = bool(
                request.state.legacy_single_host
                and (
                    request_path.startswith("/bootstrap")
                    or request_path.startswith("/platform/bootstrap")
                )
            )
            if (not request.state.platform_mode) and platform_only and (not allow_legacy_bootstrap):
                return _plain_error("Not Found", 404)

            # 3) Load session user once.
            with _request_db(request) as db:
                request.state.current_user = _load_session_user(request, db)

            # 4) Enforce login once.
            if _path_requires_login(request_path):
                authenticated = require_user(request)
                if not isinstance(authenticated, User):
                    return _finalize_response(authenticated)

            # 5) Enforce CSRF once for mutating requests.
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
                        submitted_token = (
                            str(form.get(CSRF_FORM_FIELD, "")).strip() if form else ""
                        )
                        request._receive = _receive_with_body(body)
                if not submitted_token or not csrf_cookie or not secrets.compare_digest(
                    submitted_token, csrf_cookie
                ):
                    return _finalize_response(csrf_forbidden_response(request), force_set_csrf=True)

            # 6) Enforce setup guard once.
            if _path_needs_system_guard(request_path):
                with _request_db(request) as db:
                    company = get_company_setting(db)
                    if not bool(company and getattr(company, "is_initialized", False)):
                        return _finalize_response(
                            _uninitialized_response(
                                request,
                                superadmin=is_superadmin_user(
                                    db, getattr(request.state, "current_user", None)
                                ),
                            )
                        )

            # 7) Execute request and finalize response.
            downstream = await call_next(request)
            downstream = _maybe_brand_plain_error_response(request, downstream)
            return _finalize_response(downstream)
        finally:
            reset_request_tenant_context(tenant_context_tokens)

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
        branding = get_branding(db)
        nav_color = normalize_hex_color(branding.get("nav_color", ""), default="#14213D")
        nav_foreground = nav_foreground_color(nav_color)
        primary_color = str(branding.get("primary_color", "") or "#FCA311")
        primary_contrast = str(branding.get("primary_contrast_hex", "") or "#111827")
        primary_soft = str(branding.get("primary_soft_rgba", "") or "rgba(252, 163, 17, 0.16)")
        logo_url = str(branding.get("logo_url", "") or "").replace("'", "\\'")
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
            f"  --theme-logo-url: url('{logo_url}');\n"
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
        if bool(getattr(request.state, "platform_mode", False)):
            return RedirectResponse(url="/platform/tenants", status_code=303)
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
        if bool(getattr(request.state, "platform_mode", False)):
            return RedirectResponse(url="/platform/tenants", status_code=303)
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
