import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import settings
from .db import SessionLocal
from .routes import api_router
from .routers.lookups import router as lookups_router
from .services.pdf import check_invoice_pdf_renderer, ensure_seed_invoice_pdf_template
from .services.printing import ensure_default_invoice_pdf_profile
from .templating import templates

logger = logging.getLogger(__name__)


def create_app(dev_mode: bool | None = None) -> FastAPI:
    app = FastAPI(title="weighbridge_web")

    app.include_router(api_router)
    app.include_router(lookups_router)
    effective_dev_mode = settings.dev_mode if dev_mode is None else dev_mode
    if effective_dev_mode:
        from .routes.dev import router as dev_router

        app.include_router(dev_router)
    media_dir = Path(settings.media_root).resolve()
    media_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=str(media_dir)), name="media")
    app.mount("/static", StaticFiles(directory="app/static"), name="static")

    @app.on_event("startup")
    def startup_invoice_pdf_renderer_check() -> None:
        check_invoice_pdf_renderer()
        with SessionLocal() as db:
            try:
                _, changed = ensure_seed_invoice_pdf_template(db)
                if changed:
                    db.commit()
                ensure_default_invoice_pdf_profile(db)
            except Exception:
                logger.exception("Invoice PDF default template bootstrap failed on startup.")

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

    return app

templates = Jinja2Templates(directory="app/templates")


app = create_app()
