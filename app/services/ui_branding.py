from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re

from ..config import settings
from ..models import CompanySetting
from .uploads import resolve_company_logo_web_path

DEFAULT_NAVBAR_COLOR_HEX = "#14213D"
DEFAULT_PRIMARY_COLOR_HEX = "#FCA311"
DEFAULT_NAV_LOGO_HEIGHT_PX = 34
MIN_NAV_LOGO_HEIGHT_PX = 20
MAX_NAV_LOGO_HEIGHT_PX = 80
DEFAULT_COMPANY_LOGO_WEB_PATH = "/static/img/default-company-logo.svg"
_UPLOAD_LOGO_PREFIX = "/static/uploads/company/"

_HEX_COLOR_RE = re.compile(r"^#?[0-9A-Fa-f]{6}$")


def company_logo_url(company: CompanySetting | None) -> str:
    if company is None:
        return DEFAULT_COMPANY_LOGO_WEB_PATH

    current = str(company.company_logo_path or "").strip()
    if current:
        resolved = resolve_company_logo_web_path(current)
        if _logo_web_path_exists(resolved):
            return resolved
    return DEFAULT_COMPANY_LOGO_WEB_PATH


def _logo_web_path_exists(path: str) -> bool:
    normalized = str(path or "").strip()
    if not normalized:
        return False
    if not normalized.startswith(_UPLOAD_LOGO_PREFIX):
        return True
    filename = Path(normalized).name
    if not filename:
        return False
    upload_root = Path(
        str(settings.effective_company_logo_upload_dir or "").strip()
    ).resolve()
    try:
        candidate = (upload_root / filename).resolve()
    except OSError:
        return False
    return candidate.is_file()


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


def primary_soft_rgba(hex_color: str, *, alpha: float = 0.16) -> str:
    red, green, blue = _hex_to_rgb(hex_color)
    clamped_alpha = max(0.0, min(1.0, alpha))
    return f"rgba({red}, {green}, {blue}, {clamped_alpha:.2f})"


def logo_url_with_version(url: str, updated_at: datetime | None) -> str:
    clean_url = str(url or "").strip()
    if not clean_url:
        return ""
    if not updated_at:
        return clean_url
    stamp = int(updated_at.timestamp())
    joiner = "&" if "?" in clean_url else "?"
    return f"{clean_url}{joiner}v={stamp}"


def build_ui_branding(company: CompanySetting | None) -> dict[str, object]:
    nav_logo_url = company_logo_url(company)
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
    logo_updated_at = getattr(company, "company_logo_updated_at", None)
    logo_versioned = nav_logo_url
    if nav_logo_url != DEFAULT_COMPANY_LOGO_WEB_PATH:
        logo_versioned = logo_url_with_version(nav_logo_url, logo_updated_at)
    brand_name = str(getattr(company, "name", "") or "").strip() or "Weighbridge Web"
    show_nav_logo = getattr(company, "show_nav_logo", None)
    show_nav_title = getattr(company, "show_nav_title", None)
    return {
        "brand_name": brand_name,
        "nav_logo_url": logo_versioned,
        "favicon_url": logo_versioned,
        "nav_logo_height_px": logo_height,
        "show_nav_logo": True if show_nav_logo is None else bool(show_nav_logo),
        "show_nav_title": True if show_nav_title is None else bool(show_nav_title),
        "navbar_color_hex": navbar_color,
        "primary_color_hex": primary_color,
        "primary_contrast_hex": primary_contrast_color(primary_color),
        "primary_soft_rgba": primary_soft_rgba(primary_color),
    }
