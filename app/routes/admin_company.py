from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from ..config import settings
from ..constants import ADDRESS_LINE_MAX, NAME_MAX, POSTCODE_MAX
from ..db import get_db
from ..models import CompanySetting
from ..templating import templates

router = APIRouter()

MAX_LOGO_SIZE_BYTES = 2 * 1024 * 1024
LOGO_WEB_PATH_PREFIX = "/static/uploads/company/"
ALLOWED_LOGO_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
}


def _logo_upload_dir() -> Path:
    target = str(settings.company_logo_upload_dir or "").strip()
    if not target:
        target = "app/static/uploads/company"
    upload_dir = Path(target).resolve()
    upload_dir.mkdir(parents=True, exist_ok=True)
    return upload_dir


def _company_logo_url(company: CompanySetting | None) -> str:
    if company is None:
        return ""

    current = str(company.company_logo_path or "").strip()
    if current:
        return current

    # Backward compatibility for existing rows created before company_logo_path.
    legacy_remote = str(company.logo_url or "").strip()
    if legacy_remote:
        return legacy_remote
    legacy_file = str(company.logo_file_path or "").strip().lstrip("/")
    if legacy_file:
        return f"/media/{legacy_file}"
    return ""


def _logo_file_from_web_path(path: str | None) -> Path | None:
    normalized = str(path or "").strip()
    if not normalized.startswith(LOGO_WEB_PATH_PREFIX):
        return None
    filename = Path(normalized).name
    if not filename:
        return None
    return (_logo_upload_dir() / filename).resolve()


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


def _trim(value: object) -> str:
    return str(value or "").strip()


@router.get("/admin/company", response_class=HTMLResponse)
def admin_company_settings(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    setting = _get_or_create_company_setting(db)
    db.commit()
    return templates.TemplateResponse(
        request,
        "admin/company_settings.html",
        {
            "request": request,
            "setting": setting,
            "logo_preview_url": _company_logo_url(setting),
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

    has_upload = isinstance(logo_file, UploadFile) and bool(_trim(logo_file.filename))
    if require_upload and not has_upload:
        errors.append("Choose a PNG or JPG logo file to upload.")

    uploaded_web_path: str | None = None
    uploaded_disk_path: Path | None = None
    if has_upload and not remove_logo:
        assert isinstance(logo_file, UploadFile)
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
                upload_dir = _logo_upload_dir()
                if extension == ".jpg":
                    source_extension = Path(str(logo_file.filename or "")).suffix.lower()
                    if source_extension == ".jpeg":
                        extension = ".jpeg"
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
            logo_url=setting.logo_url,
            logo_file_path=setting.logo_file_path,
        )
        return templates.TemplateResponse(
            request,
            "admin/company_settings.html",
            {
                "request": request,
                "setting": form_like,
                "logo_preview_url": _company_logo_url(form_like),
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

    if remove_logo:
        setting.company_logo_path = None
        setting.company_logo_updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        setting.logo_url = None
        setting.logo_file_path = None
    elif uploaded_web_path:
        setting.company_logo_path = uploaded_web_path
        setting.company_logo_updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        setting.logo_url = None
        setting.logo_file_path = None

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
