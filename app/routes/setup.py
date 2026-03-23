from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from ..auth import is_admin_user
from ..config import settings
from ..constants import NAME_MAX
from ..db import get_db
from ..models import Yard
from ..seed import (
    force_refresh_system_print_templates,
    seed_invoice_void_reasons,
    seed_payment_methods,
    seed_print_destinations,
    seed_tax_rates,
    seed_units,
    seed_vehicle_types,
    seed_void_reasons,
)
from ..services.uploads import company_logo_upload_dir
from ..services.system_setup import (
    DEFAULT_YARD_NAME,
    ensure_company_settings_row_exists,
    get_company_setting,
    seed_required_reference_data,
    upsert_default_yard,
)
from ..tenancy import request_platform_mode
from ..templating import templates

router = APIRouter()
logger = logging.getLogger(__name__)

MAX_LOGO_SIZE_BYTES = 2 * 1024 * 1024
LOGO_WEB_PATH_PREFIX = "/static/uploads/company/"
ALLOWED_LOGO_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
}


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _setup_context(
    request: Request,
    *,
    company_name: str = "",
    default_yard_name: str = DEFAULT_YARD_NAME,
    seed_demo: bool = False,
    show_demo_seed: bool = False,
    errors: list[str] | None = None,
) -> dict[str, object]:
    return {
        "request": request,
        "company_name": company_name,
        "default_yard_name": default_yard_name,
        "seed_demo": seed_demo,
        "show_demo_seed": show_demo_seed,
        "errors": errors or [],
    }


def _logo_upload_dir() -> Path:
    return company_logo_upload_dir()


def _initial_yard_name(db: Session) -> str:
    yard = db.query(Yard).order_by(Yard.id.asc()).first()
    if yard is None:
        return DEFAULT_YARD_NAME
    return str(yard.description or "").strip() or str(yard.code or "").strip() or DEFAULT_YARD_NAME


def _run_demo_seed() -> None:
    seed_units()
    seed_tax_rates()
    seed_void_reasons()
    seed_invoice_void_reasons()
    seed_payment_methods()
    seed_vehicle_types()


def _seed_printing_defaults(db: Session) -> None:
    force_refresh_system_print_templates(db)
    seed_print_destinations(db)


@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    if request_platform_mode(request):
        return HTMLResponse("Not Found", status_code=404)

    company = get_company_setting(db)
    if bool(company and company.is_initialized):
        return HTMLResponse("Not Found", status_code=404)

    current_user = getattr(request.state, "current_user", None)
    if current_user is None:
        return RedirectResponse(url="/login?next=/setup", status_code=302)
    if not is_admin_user(db, current_user):
        return HTMLResponse("Forbidden", status_code=403)
    if company is None:
        company = ensure_company_settings_row_exists(db)

    return templates.TemplateResponse(
        request,
        "auth/setup.html",
        _setup_context(
            request,
            company_name=str(company.name or "").strip(),
            default_yard_name=_initial_yard_name(db),
            show_demo_seed=bool(settings.dev_mode),
        ),
    )


@router.post("/setup", response_class=HTMLResponse)
async def setup_submit(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    if request_platform_mode(request):
        return HTMLResponse("Not Found", status_code=404)

    company = get_company_setting(db)
    if bool(company and company.is_initialized):
        return HTMLResponse("Not Found", status_code=404)

    current_user = getattr(request.state, "current_user", None)
    if current_user is None:
        return RedirectResponse(url="/login?next=/setup", status_code=302)
    if not is_admin_user(db, current_user):
        return HTMLResponse("Forbidden", status_code=403)
    if company is None:
        company = ensure_company_settings_row_exists(db)

    form = await request.form()
    company_name = str(form.get("company_name", "")).strip()
    yard_name = str(form.get("default_yard_name", "")).strip() or DEFAULT_YARD_NAME
    seed_demo = bool(settings.dev_mode) and _truthy(form.get("seed_demo"))
    logo_file = form.get("company_logo_file")

    errors: list[str] = []
    if not company_name:
        errors.append("Company name is required.")
    elif len(company_name) > NAME_MAX:
        errors.append(f"Company name must be {NAME_MAX} characters or fewer.")
    if len(yard_name) > NAME_MAX:
        errors.append(f"Default yard name must be {NAME_MAX} characters or fewer.")

    uploaded_web_path: str | None = None
    if isinstance(logo_file, UploadFile) and str(logo_file.filename or "").strip():
        content_type = str(logo_file.content_type or "").strip().lower()
        extension = ALLOWED_LOGO_TYPES.get(content_type)
        if extension is None:
            errors.append("Company logo must be a PNG or JPG file.")
        else:
            payload = await logo_file.read()
            if not payload:
                errors.append("Uploaded logo file is empty.")
            elif len(payload) > MAX_LOGO_SIZE_BYTES:
                errors.append("Company logo must be 2MB or smaller.")
            else:
                filename = f"logo-{uuid4().hex}{extension}"
                output = (_logo_upload_dir() / filename).resolve()
                output.write_bytes(payload)
                uploaded_web_path = f"{LOGO_WEB_PATH_PREFIX}{filename}"

    if errors:
        return templates.TemplateResponse(
            request,
            "auth/setup.html",
            _setup_context(
                request,
                company_name=company_name,
                default_yard_name=yard_name,
                seed_demo=seed_demo,
                show_demo_seed=bool(settings.dev_mode),
                errors=errors,
            ),
            status_code=400,
        )

    company.name = company_name
    if uploaded_web_path:
        company.company_logo_path = uploaded_web_path
        company.company_logo_updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    upsert_default_yard(db, yard_name=yard_name)
    seed_required_reference_data(db)
    _seed_printing_defaults(db)
    company.is_initialized = True
    db.commit()

    if seed_demo:
        try:
            _run_demo_seed()
        except Exception:
            logger.exception("Setup demo seed failed after initialization.")

    return RedirectResponse(url="/", status_code=303)
