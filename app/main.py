import logging
from pathlib import Path
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect

from .config import settings
from .db import SessionLocal
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
from .seed import force_refresh_system_print_templates
from .services.pdf import check_invoice_pdf_renderer
from .templating import templates

logger = logging.getLogger(__name__)


def _strip_non_production_routes(app: FastAPI) -> None:
    filtered_routes = []
    for route in app.router.routes:
        path = str(getattr(route, "path", "")).lower()
        if "debug" in path or "__" in path or "dev" in path:
            continue
        filtered_routes.append(route)
    app.router.routes = filtered_routes


def _log_alembic_revision_status() -> None:
    try:
        config = Config("alembic.ini")
        script = ScriptDirectory.from_config(config)
        heads = list(script.get_heads())
    except Exception:
        logger.warning("Could not read Alembic heads from alembic.ini.", exc_info=True)
        heads = []

    current_revision = None
    try:
        with SessionLocal() as db:
            bind = db.get_bind()
            with bind.connect() as connection:
                context = MigrationContext.configure(connection)
                current_revision = context.get_current_revision()
    except Exception:
        logger.warning("Could not read current Alembic revision from database.", exc_info=True)

    logger.info(
        "Alembic revision status: current=%s, heads=%s",
        current_revision,
        heads,
    )


def _printing_schema_ready_for_bootstrap() -> bool:
    try:
        with SessionLocal() as db:
            bind = db.get_bind()
            inspector = inspect(bind)
            table_names = set(inspector.get_table_names())
            if "print_templates" not in table_names:
                return False
            template_columns = {
                str(column["name"])
                for column in inspector.get_columns("print_templates")
            }
            required_columns = {
                "code",
                "description",
                "document_type",
                "format",
                "content",
                "is_system",
                "is_active",
            }
            return required_columns.issubset(template_columns)
    except Exception:
        logger.warning(
            "Skipping system template bootstrap; printing schema inspection failed.",
            exc_info=True,
        )
        return False


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

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        csrf_cookie = request.cookies.get(CSRF_COOKIE_NAME)
        should_set_csrf_cookie = False
        if not csrf_cookie:
            csrf_cookie = generate_csrf_token()
            should_set_csrf_cookie = True
        request.state.csrf_token = csrf_cookie

        if is_state_changing_method(request.method):
            submitted_token = str(request.headers.get(CSRF_HEADER_NAME, "")).strip()
            if not submitted_token:
                content_type = str(request.headers.get("content-type", "")).lower()
                if content_type.startswith(
                    "application/x-www-form-urlencoded"
                ) or content_type.startswith("multipart/form-data"):
                    try:
                        form = await request.form()
                    except Exception:
                        form = None
                    submitted_token = (
                        str(form.get(CSRF_FORM_FIELD, "")).strip() if form else ""
                    )
            if not submitted_token or not csrf_cookie or not secrets.compare_digest(
                submitted_token, csrf_cookie
            ):
                response = csrf_forbidden_response(request)
                set_csrf_cookie(response, request, csrf_cookie)
                apply_security_headers(response)
                return response

        response = await call_next(request)
        if should_set_csrf_cookie:
            set_csrf_cookie(response, request, csrf_cookie)
        apply_security_headers(response)
        return response

    @app.on_event("startup")
    def startup_printing_bootstrap() -> None:
        check_invoice_pdf_renderer()
        if not _printing_schema_ready_for_bootstrap():
            logger.info(
                "Skipping system template bootstrap until printing migrations are applied."
            )
            return
        with SessionLocal() as db:
            try:
                force_refresh_system_print_templates(db)
            except Exception:
                logger.exception("System template bootstrap failed on startup.")

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

templates = Jinja2Templates(directory="app/templates")


app = create_app()
