from fastapi.templating import Jinja2Templates

from .build_info import get_build_info
from .config import settings
from .constants import field_limits
from .constants.help_text import HELP_TEXT
from .db import get_db
from .services.ui_branding import get_branding

_MISSING = object()


def _load_branding_for_request(request) -> dict[str, object]:
    cached = getattr(request.state, "_ui_branding_cache", _MISSING)
    if cached is not _MISSING:
        return cached

    dep = request.app.dependency_overrides.get(get_db, get_db)
    db_gen = dep()
    db = next(db_gen)
    try:
        branding = get_branding(db)
        request.state._ui_branding_cache = branding
        return branding
    except Exception:
        branding = {
            "company_name": "Weighbridge Web",
            "brand_name": "Weighbridge Web",
            "nav_color": "#14213D",
            "primary_color": "#FCA311",
            "navbar_color_hex": "#14213D",
            "primary_color_hex": "#FCA311",
            "nav_logo_height_px": 34,
            "show_nav_logo": True,
            "show_nav_title": True,
            "nav_logo_url": "/static/img/default-company-logo.svg",
            "logo_url": "/static/img/default-company-logo.svg",
            "favicon_url": "/static/img/default-company-logo.svg",
            "primary_contrast_hex": "#111827",
            "primary_soft_rgba": "rgba(252, 163, 17, 0.16)",
        }
        request.state._ui_branding_cache = branding
        return branding
    finally:
        try:
            next(db_gen)
        except StopIteration:
            pass


def _ui_branding_context(request) -> dict[str, object]:
    branding = _load_branding_for_request(request)
    return {
        "ui_branding": branding,
        "company_settings": {
            "name": branding["company_name"],
            "nav_color": branding["nav_color"],
            "primary_color": branding["primary_color"],
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


def _tenant_context(request) -> dict[str, object]:
    tenant = getattr(request.state, "tenant", None)
    tenant_name = str(getattr(tenant, "name", "") or "").strip()
    tenant_subdomain = str(getattr(tenant, "subdomain", "") or "").strip()
    return {
        "current_tenant": tenant,
        "current_tenant_name": tenant_name,
        "current_tenant_subdomain": tenant_subdomain,
        "platform_mode": bool(getattr(request.state, "platform_mode", False)),
    }


templates = Jinja2Templates(
    directory="app/templates",
    context_processors=[_ui_branding_context, _csrf_context, _auth_context, _tenant_context],
)
_build_info = get_build_info()
_asset_build_stamp = f"{_build_info['version']}-{_build_info['commit_short']}"
templates.env.globals["DEV_MODE"] = bool(settings.dev_mode)
templates.env.globals["limits"] = field_limits
templates.env.globals["help_text"] = HELP_TEXT
templates.env.globals["app_version"] = _build_info["version"]
templates.env.globals["app_commit_short"] = _build_info["commit_short"]
templates.env.globals["app_build_label"] = _build_info["label"]
templates.env.globals["BUILD_STAMP"] = _asset_build_stamp
