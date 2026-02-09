from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import settings
from .routes import api_router
from .routers.lookups import router as lookups_router


def create_app(dev_mode: bool | None = None) -> FastAPI:
    app = FastAPI(title="weighbridge_web")

    app.include_router(api_router)
    app.include_router(lookups_router)
    effective_dev_mode = settings.dev_mode if dev_mode is None else dev_mode
    if effective_dev_mode:
        from .routes.dev import router as dev_router

        app.include_router(dev_router)
    app.mount("/static", StaticFiles(directory="app/static"), name="static")

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
