from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from ..constants import ADDRESS_LINE_MAX, NAME_MAX, POSTCODE_MAX
from ..db import get_db
from ..models import CompanySetting
from ..services.uploads import company_logo_upload_dir, logo_file_from_web_path
from ..services.ui_branding import (
    DEFAULT_NAVBAR_COLOR_HEX,
    DEFAULT_NAV_LOGO_HEIGHT_PX,
    DEFAULT_PRIMARY_COLOR_HEX,
    build_ui_branding,
    get_branding,
    is_valid_hex_color,
    normalize_hex_color,
    parse_logo_height_px,
)
from ..templating import templates

router = APIRouter()

MAX_LOGO_SIZE_BYTES = 2 * 1024 * 1024
LOGO_WEB_PATH_PREFIX = "/static/uploads/company/"
ALLOWED_LOGO_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
}


def _logo_upload_dir() -> Path:
    return company_logo_upload_dir()


def _logo_file_from_web_path(path: str | None) -> Path | None:
    return logo_file_from_web_path(path)


def _safe_unlink(path: Path | None) -> None:
    if path is None:
        return
    try:
        upload_root = _logo_upload_dir()
        if path.is_file() and str(path).startswith(str(upload_root)):
            path.unlink(missing_ok=True)
    except OSError:
        return


def _get_or_create_company_setting(db: Session) -> CompanySetting:
    setting = (
        db.execute(select(CompanySetting).order_by(CompanySetting.id.asc()).limit(1))
        .scalars()
        .first()
    )
    if setting is None:
        setting = CompanySetting()
        db.add(setting)
        db.flush()
    return setting


def _get_company_setting(db: Session) -> CompanySetting | None:
    return (
        db.execute(select(CompanySetting).order_by(CompanySetting.id.asc()).limit(1))
        .scalars()
        .first()
    )


def _trim(value: object) -> str:
    return str(value or "").strip()


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _form_checkbox(form, key: str) -> bool:
    values = list(form.getlist(key)) if hasattr(form, "getlist") else [form.get(key)]
    return any(_truthy(value) for value in values)


def _resolve_logo_extension(upload: UploadFile) -> str | None:
    content_type = str(upload.content_type or "").strip().lower()
    if content_type in ALLOWED_LOGO_TYPES:
        if content_type == "image/jpeg":
            source_extension = Path(str(upload.filename or "")).suffix.lower()
            if source_extension == ".jpeg":
                return ".jpeg"
        return ALLOWED_LOGO_TYPES[content_type]

    source_extension = Path(str(upload.filename or "")).suffix.lower()
    if source_extension in {".png", ".jpg", ".jpeg"}:
        return source_extension
    return None


@router.get("/admin/company", response_class=HTMLResponse)
def admin_company_settings(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    setting = _get_company_setting(db) or CompanySetting()
    branding = get_branding(db)
    has_logo_configured = bool(str(getattr(setting, "company_logo_path", "") or "").strip())
    return templates.TemplateResponse(
        request,
        "admin/company_settings.html",
        {
            "request": request,
            "setting": setting,
            "branding": {
                "logo_url": str(branding.get("logo_url", "") or ""),
                "logo_exists": bool(branding.get("logo_exists", False)),
            },
            "has_logo_configured": has_logo_configured,
            "saved": request.query_params.get("saved") == "1",
            "errors": [],
        },
    )


@router.post("/admin/company", response_class=HTMLResponse)
async def admin_company_settings_save(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    form = await request.form()
    setting = _get_or_create_company_setting(db)
    logo_action = _trim(form.get("logo_action")).lower()
    remove_logo = logo_action == "remove"
    require_upload = logo_action == "upload"

    name = _trim(form.get("name"))
    address_line1 = _trim(form.get("address_line1"))
    address_line2 = _trim(form.get("address_line2"))
    city = _trim(form.get("city"))
    postcode = _trim(form.get("postcode"))
    country = _trim(form.get("country"))
    navbar_color_hex = _trim(form.get("navbar_color_hex"))
    primary_color_hex = _trim(form.get("primary_color_hex"))
    nav_logo_height_raw = _trim(form.get("nav_logo_height_px"))
    show_nav_logo = _form_checkbox(form, "show_nav_logo")
    show_nav_title = _form_checkbox(form, "show_nav_title")
    logo_file = form.get("company_logo_file")

    errors: list[str] = []
    if len(name) > NAME_MAX:
        errors.append(f"Company name must be {NAME_MAX} characters or fewer.")
    if len(address_line1) > ADDRESS_LINE_MAX:
        errors.append(f"Address line 1 must be {ADDRESS_LINE_MAX} characters or fewer.")
    if len(address_line2) > ADDRESS_LINE_MAX:
        errors.append(f"Address line 2 must be {ADDRESS_LINE_MAX} characters or fewer.")
    if len(city) > ADDRESS_LINE_MAX:
        errors.append(f"City must be {ADDRESS_LINE_MAX} characters or fewer.")
    if len(postcode) > POSTCODE_MAX:
        errors.append(f"Postcode must be {POSTCODE_MAX} characters or fewer.")
    if len(country) > ADDRESS_LINE_MAX:
        errors.append(f"Country must be {ADDRESS_LINE_MAX} characters or fewer.")
    if navbar_color_hex and not is_valid_hex_color(navbar_color_hex):
        errors.append("Navbar colour must be a valid HEX colour.")
    if primary_color_hex and not is_valid_hex_color(primary_color_hex):
        errors.append("Primary colour must be a valid HEX colour.")

    has_upload = isinstance(logo_file, UploadFile) and bool(_trim(logo_file.filename))
    if require_upload and not has_upload:
        errors.append("Choose a PNG or JPG logo file to upload.")

    uploaded_web_path: str | None = None
    uploaded_disk_path: Path | None = None
    if has_upload and not remove_logo:
        assert isinstance(logo_file, UploadFile)
        extension = _resolve_logo_extension(logo_file)
        if extension is None:
            errors.append("Company logo must be a PNG or JPG file.")
        else:
            payload = await logo_file.read()
            if not payload:
                errors.append("Uploaded logo file is empty.")
            elif len(payload) > MAX_LOGO_SIZE_BYTES:
                errors.append("Company logo must be 2MB or smaller.")
            else:
                upload_dir = _logo_upload_dir()
                filename = f"logo-{uuid4().hex}{extension}"
                uploaded_disk_path = (upload_dir / filename).resolve()
                uploaded_disk_path.write_bytes(payload)
                uploaded_web_path = f"{LOGO_WEB_PATH_PREFIX}{filename}"

    form_logo_value = uploaded_web_path or (
        None if remove_logo else (setting.company_logo_path or None)
    )

    if errors:
        if uploaded_disk_path is not None and uploaded_disk_path.exists():
            _safe_unlink(uploaded_disk_path)

        form_like = CompanySetting(
            id=setting.id,
            name=name or None,
            address_line1=address_line1 or None,
            address_line2=address_line2 or None,
            city=city or None,
            postcode=postcode or None,
            country=country or None,
            company_logo_path=form_logo_value,
            company_logo_updated_at=setting.company_logo_updated_at,
            navbar_color_hex=navbar_color_hex or setting.navbar_color_hex,
            primary_color_hex=primary_color_hex or setting.primary_color_hex,
            nav_logo_height_px=parse_logo_height_px(
                nav_logo_height_raw,
                default=(
                    setting.nav_logo_height_px
                    if setting.nav_logo_height_px is not None
                    else DEFAULT_NAV_LOGO_HEIGHT_PX
                ),
            ),
            show_nav_logo=show_nav_logo,
            show_nav_title=show_nav_title,
        )
        branding = build_ui_branding(form_like)
        return templates.TemplateResponse(
            request,
            "admin/company_settings.html",
            {
                "request": request,
                "setting": form_like,
                "branding": {
                    "logo_url": str(branding.get("logo_url", "") or ""),
                    "logo_exists": bool(branding.get("logo_exists", False)),
                },
                "has_logo_configured": bool(str(form_logo_value or "").strip()),
                "saved": False,
                "errors": errors,
            },
            status_code=400,
        )

    old_logo_file = _logo_file_from_web_path(setting.company_logo_path)

    setting.name = name or None
    setting.address_line1 = address_line1 or None
    setting.address_line2 = address_line2 or None
    setting.city = city or None
    setting.postcode = postcode or None
    setting.country = country or None
    setting.navbar_color_hex = normalize_hex_color(
        navbar_color_hex,
        default=(
            str(setting.navbar_color_hex or "").strip() or DEFAULT_NAVBAR_COLOR_HEX
        ),
    )
    setting.primary_color_hex = normalize_hex_color(
        primary_color_hex,
        default=(
            str(setting.primary_color_hex or "").strip() or DEFAULT_PRIMARY_COLOR_HEX
        ),
    )
    setting.nav_logo_height_px = parse_logo_height_px(
        nav_logo_height_raw,
        default=(
            setting.nav_logo_height_px
            if setting.nav_logo_height_px is not None
            else DEFAULT_NAV_LOGO_HEIGHT_PX
        ),
    )
    setting.show_nav_logo = show_nav_logo
    setting.show_nav_title = show_nav_title

    if remove_logo:
        setting.company_logo_path = None
        setting.company_logo_updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    elif uploaded_web_path:
        setting.company_logo_path = uploaded_web_path
        setting.company_logo_updated_at = datetime.now(timezone.utc).replace(tzinfo=None)

    db.commit()

    if remove_logo:
        _safe_unlink(old_logo_file)
    elif uploaded_disk_path is not None and old_logo_file is not None:
        try:
            if old_logo_file != uploaded_disk_path.resolve():
                _safe_unlink(old_logo_file)
        except OSError:
            _safe_unlink(old_logo_file)

    return RedirectResponse(url="/admin/company?saved=1", status_code=303)
