from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .routes import api_router

app = FastAPI(title="weighbridge_web")

app.include_router(api_router)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

templates = Jinja2Templates(directory="app/templates")


@app.get("/health", tags=["health"])
def health_check() -> dict:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/customers", response_class=HTMLResponse)
def customers(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("customers.html", {"request": request})


@app.get("/vehicles", response_class=HTMLResponse)
def vehicles(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("vehicles.html", {"request": request})


@app.get("/products", response_class=HTMLResponse)
def products(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("products.html", {"request": request})


@app.get("/invoices", response_class=HTMLResponse)
def invoices(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("invoices.html", {"request": request})


@app.get("/lookups", response_class=HTMLResponse)
def lookups(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("lookups.html", {"request": request})


@app.get("/reports", response_class=HTMLResponse)
def reports(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("reports.html", {"request": request})


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("admin.html", {"request": request})
