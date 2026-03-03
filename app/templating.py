from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from .build_info import get_build_info
from .config import settings
from .constants import field_limits
from .constants.help_text import HELP_TEXT
from .db import get_db
from .models import CompanySetting
from .services.ui_branding import build_ui_branding


def _load_company_setting_for_request(request) -> CompanySetting | None:
    dep = request.app.dependency_overrides.get(get_db, get_db)
    db_gen = dep()
    db = next(db_gen)
    try:
        return (
            db.execute(select(CompanySetting).order_by(CompanySetting.id.asc()).limit(1))
            .scalars()
            .first()
        )
    except Exception:
        return None
    finally:
        try:
            next(db_gen)
        except StopIteration:
            pass


def _ui_branding_context(request) -> dict[str, object]:
    setting = _load_company_setting_for_request(request)
    return {"ui_branding": build_ui_branding(setting)}


def _csrf_context(request) -> dict[str, object]:
    token = str(getattr(request.state, "csrf_token", "") or "")
    return {"csrf_token": token}


templates = Jinja2Templates(
    directory="app/templates",
    context_processors=[_ui_branding_context, _csrf_context],
)
_build_info = get_build_info()
templates.env.globals["DEV_MODE"] = bool(settings.dev_mode)
templates.env.globals["limits"] = field_limits
templates.env.globals["help_text"] = HELP_TEXT
templates.env.globals["app_version"] = _build_info["version"]
templates.env.globals["app_commit_short"] = _build_info["commit_short"]
templates.env.globals["app_build_label"] = _build_info["label"]
