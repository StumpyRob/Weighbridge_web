from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from .build_info import get_build_info
from .config import settings
from .constants import field_limits
from .constants.help_text import HELP_TEXT
from .db import get_db
from .models import CompanySetting
from .services.ui_branding import build_ui_branding

_MISSING = object()


def _load_company_setting_for_request(request) -> CompanySetting | None:
    cached = getattr(request.state, "_company_setting_cache", _MISSING)
    if cached is not _MISSING:
        return cached

    dep = request.app.dependency_overrides.get(get_db, get_db)
    db_gen = dep()
    db = next(db_gen)
    try:
        setting = (
            db.execute(select(CompanySetting).order_by(CompanySetting.id.asc()).limit(1))
            .scalars()
            .first()
        )
        request.state._company_setting_cache = setting
        return setting
    except Exception:
        request.state._company_setting_cache = None
        return None
    finally:
        try:
            next(db_gen)
        except StopIteration:
            pass


def _ui_branding_context(request) -> dict[str, object]:
    setting = _load_company_setting_for_request(request)
    branding = build_ui_branding(setting)
    return {
        "ui_branding": branding,
        "company_settings": {
            "name": branding["brand_name"],
            "nav_color": branding["navbar_color_hex"],
            "primary_color": branding["primary_color_hex"],
            "nav_logo_height_px": branding["nav_logo_height_px"],
            "show_nav_logo": branding["show_nav_logo"],
            "show_nav_title": branding["show_nav_title"],
            "nav_logo_url": branding["nav_logo_url"],
            "primary_contrast_hex": branding["primary_contrast_hex"],
            "primary_soft_rgba": branding["primary_soft_rgba"],
        },
    }


def _csrf_context(request) -> dict[str, object]:
    token = str(getattr(request.state, "csrf_token", "") or "")
    return {"csrf_token": token}


def _auth_context(request) -> dict[str, object]:
    return {"current_user": getattr(request.state, "current_user", None)}


templates = Jinja2Templates(
    directory="app/templates",
    context_processors=[_ui_branding_context, _csrf_context, _auth_context],
)
_build_info = get_build_info()
templates.env.globals["DEV_MODE"] = bool(settings.dev_mode)
templates.env.globals["limits"] = field_limits
templates.env.globals["help_text"] = HELP_TEXT
templates.env.globals["app_version"] = _build_info["version"]
templates.env.globals["app_commit_short"] = _build_info["commit_short"]
templates.env.globals["app_build_label"] = _build_info["label"]
