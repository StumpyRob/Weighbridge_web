from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import CompanySetting
from ..tenancy import current_platform_mode
from .uploads import logo_file_from_web_path, resolve_company_logo_web_path

DEFAULT_NAVBAR_COLOR_HEX = "#14213D"
DEFAULT_PRIMARY_COLOR_HEX = "#FCA311"
DEFAULT_NAV_LOGO_HEIGHT_PX = 34
MIN_NAV_LOGO_HEIGHT_PX = 20
MAX_NAV_LOGO_HEIGHT_PX = 80
DEFAULT_COMPANY_LOGO_WEB_PATH = "/static/img/default-company-logo.svg"
DEFAULT_COMPANY_NAME = "Weighbridge Web"
_UPLOAD_LOGO_PREFIX = "/static/uploads/company/"
_STATIC_PREFIX = "/static/"
_MEDIA_PREFIX = "/media/"

_HEX_COLOR_RE = re.compile(r"^#?[0-9A-Fa-f]{6}$")


def _upload_logo_file_from_web_path(path: str | None) -> Path | None:
    normalized = str(path or "").strip()
    if not normalized.startswith(_UPLOAD_LOGO_PREFIX):
        return None
    return logo_file_from_web_path(normalized)


def _strip_query(url: str | None) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    return str(urlsplit(raw).path or "").strip()


def logo_file_exists_on_disk(path: str | None) -> bool:
    normalized = str(path or "").strip()
    if not normalized:
        return False
    clean_path = _strip_query(normalized)
    if not clean_path:
        return False
    if clean_path.startswith(_UPLOAD_LOGO_PREFIX):
        candidate = _upload_logo_file_from_web_path(clean_path)
        return bool(candidate and candidate.is_file())
    if clean_path.startswith(_STATIC_PREFIX):
        static_root = (Path(__file__).resolve().parents[1] / "static").resolve()
        relative = clean_path.removeprefix(_STATIC_PREFIX).lstrip("/\\")
        if not relative:
            return False
        try:
            candidate = (static_root / relative).resolve()
        except OSError:
            return False
        if not str(candidate).startswith(str(static_root)):
            return False
        return candidate.is_file()
    if clean_path.startswith(_MEDIA_PREFIX):
        media_root = Path(str(settings.media_root or "").strip() or "app/media").resolve()
        relative = clean_path.removeprefix(_MEDIA_PREFIX).lstrip("/\\")
        if not relative:
            return False
        try:
            candidate = (media_root / relative).resolve()
        except OSError:
            return False
        if not str(candidate).startswith(str(media_root)):
            return False
        return candidate.is_file()
    if clean_path.startswith(("http://", "https://", "data:")):
        return False
    candidate = Path(clean_path)
    if not candidate.is_absolute():
        return False
    return candidate.is_file()


def company_logo_url(company: CompanySetting | None) -> str:
    if company is None:
        return DEFAULT_COMPANY_LOGO_WEB_PATH

    current = str(company.company_logo_path or "").strip()
    if not current:
        return DEFAULT_COMPANY_LOGO_WEB_PATH

    resolved = resolve_company_logo_web_path(current)
    if resolved.startswith(("http://", "https://", "data:")):
        return resolved
    if not logo_file_exists_on_disk(resolved):
        return DEFAULT_COMPANY_LOGO_WEB_PATH
    return resolved


def normalize_hex_color(value: object, *, default: str) -> str:
    raw = str(value or "").strip()
    if not raw or not _HEX_COLOR_RE.fullmatch(raw):
        return default
    normalized = raw.upper()
    return normalized if normalized.startswith("#") else f"#{normalized}"


def parse_logo_height_px(value: object, *, default: int) -> int:
    try:
        parsed = int(str(value or "").strip())
    except (TypeError, ValueError):
        return default
    return max(MIN_NAV_LOGO_HEIGHT_PX, min(MAX_NAV_LOGO_HEIGHT_PX, parsed))


def is_valid_hex_color(value: object) -> bool:
    raw = str(value or "").strip()
    return bool(_HEX_COLOR_RE.fullmatch(raw))


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    value = normalize_hex_color(hex_color, default=DEFAULT_PRIMARY_COLOR_HEX).lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def primary_contrast_color(hex_color: str) -> str:
    red, green, blue = _hex_to_rgb(hex_color)
    luminance = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255
    return "#111827" if luminance > 0.58 else "#FFFFFF"


def nav_foreground_color(hex_color: str) -> str:
    red, green, blue = _hex_to_rgb(hex_color)
    luminance = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255
    return "#FFFFFF" if luminance < 0.58 else "#14213D"


def primary_soft_rgba(hex_color: str, *, alpha: float = 0.16) -> str:
    red, green, blue = _hex_to_rgb(hex_color)
    clamped_alpha = max(0.0, min(1.0, alpha))
    return f"rgba({red}, {green}, {blue}, {clamped_alpha:.2f})"


def _mix_hex_color(base_hex: str, target_hex: str, *, amount: float) -> str:
    base_red, base_green, base_blue = _hex_to_rgb(base_hex)
    target_red, target_green, target_blue = _hex_to_rgb(target_hex)
    clamped_amount = max(0.0, min(1.0, amount))

    def _channel(base: int, target: int) -> int:
        return int(round(base + ((target - base) * clamped_amount)))

    return "#{:02X}{:02X}{:02X}".format(
        _channel(base_red, target_red),
        _channel(base_green, target_green),
        _channel(base_blue, target_blue),
    )


def lighten_hex_color(hex_color: str, *, amount: float = 0.18) -> str:
    return _mix_hex_color(hex_color, "#FFFFFF", amount=amount)


def darken_hex_color(hex_color: str, *, amount: float = 0.18) -> str:
    return _mix_hex_color(hex_color, "#000000", amount=amount)


def logo_url_with_version(url: str, logo_version: int | None) -> str:
    clean_url = str(url or "").strip()
    if not clean_url:
        return ""
    version = int(logo_version or 0)
    if version <= 0:
        return clean_url
    parts = urlsplit(clean_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["v"] = str(version)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _logo_version(company: CompanySetting | None, logo_url: str) -> int:
    if company is not None:
        updated_at = getattr(company, "company_logo_updated_at", None)
        if isinstance(updated_at, datetime):
            try:
                return int(updated_at.timestamp())
            except (OSError, OverflowError, ValueError):
                pass
    candidate = _upload_logo_file_from_web_path(_strip_query(logo_url))
    if candidate is None or not candidate.is_file():
        return 0
    try:
        return int(candidate.stat().st_mtime)
    except OSError:
        return 0


def _default_branding() -> dict[str, object]:
    primary_color = DEFAULT_PRIMARY_COLOR_HEX
    return {
        "company_name": DEFAULT_COMPANY_NAME,
        "brand_name": DEFAULT_COMPANY_NAME,
        "logo_url": DEFAULT_COMPANY_LOGO_WEB_PATH,
        "logo_version": 0,
        "logo_exists": logo_file_exists_on_disk(DEFAULT_COMPANY_LOGO_WEB_PATH),
        "nav_logo_url": DEFAULT_COMPANY_LOGO_WEB_PATH,
        "favicon_url": DEFAULT_COMPANY_LOGO_WEB_PATH,
        "nav_logo_height_px": DEFAULT_NAV_LOGO_HEIGHT_PX,
        "show_nav_logo": True,
        "show_nav_title": True,
        "nav_color": DEFAULT_NAVBAR_COLOR_HEX,
        "primary_color": primary_color,
        "navbar_color_hex": DEFAULT_NAVBAR_COLOR_HEX,
        "primary_color_hex": primary_color,
        "primary_contrast_hex": primary_contrast_color(primary_color),
        "primary_soft_rgba": primary_soft_rgba(primary_color),
    }


def build_ui_branding(company: CompanySetting | None) -> dict[str, object]:
    try:
        nav_logo_resolved = company_logo_url(company)
        logo_version = _logo_version(company, nav_logo_resolved)
        logo_url = logo_url_with_version(nav_logo_resolved, logo_version)
        navbar_color = normalize_hex_color(
            getattr(company, "navbar_color_hex", None),
            default=DEFAULT_NAVBAR_COLOR_HEX,
        )
        primary_color = normalize_hex_color(
            getattr(company, "primary_color_hex", None),
            default=DEFAULT_PRIMARY_COLOR_HEX,
        )
        logo_height = parse_logo_height_px(
            getattr(company, "nav_logo_height_px", None),
            default=DEFAULT_NAV_LOGO_HEIGHT_PX,
        )
        brand_name = str(getattr(company, "name", "") or "").strip() or DEFAULT_COMPANY_NAME
        show_nav_logo = getattr(company, "show_nav_logo", None)
        show_nav_title = getattr(company, "show_nav_title", None)
        return {
            "company_name": brand_name,
            "brand_name": brand_name,
            "logo_url": logo_url,
            "logo_version": logo_version,
            "logo_exists": logo_file_exists_on_disk(nav_logo_resolved),
            "nav_logo_url": logo_url,
            "favicon_url": logo_url,
            "nav_logo_height_px": logo_height,
            "show_nav_logo": True if show_nav_logo is None else bool(show_nav_logo),
            "show_nav_title": True if show_nav_title is None else bool(show_nav_title),
            "nav_color": navbar_color,
            "primary_color": primary_color,
            "navbar_color_hex": navbar_color,
            "primary_color_hex": primary_color,
            "primary_contrast_hex": primary_contrast_color(primary_color),
            "primary_soft_rgba": primary_soft_rgba(primary_color),
        }
    except Exception:
        return _default_branding()


def get_branding(db: Session, tenant_id: str | None = None) -> dict[str, object]:
    if current_platform_mode():
        return _default_branding()
    try:
        query = select(CompanySetting).order_by(CompanySetting.id.asc()).limit(1)
        if tenant_id:
            query = query.where(CompanySetting.tenant_id == int(tenant_id))
        company = db.execute(query).scalars().first()
    except Exception:
        company = None
    try:
        return build_ui_branding(company)
    except Exception:
        return _default_branding()
