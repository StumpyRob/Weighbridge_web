from __future__ import annotations

import base64
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import mimetypes
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import CompanySetting
from .uploads import resolve_company_logo_web_path


def _company_setting(db: Session | None) -> CompanySetting | None:
    if db is None:
        return None
    return (
        db.execute(select(CompanySetting).order_by(CompanySetting.id.asc()).limit(1))
        .scalars()
        .first()
    )


def _company_name(company: CompanySetting | None) -> str:
    if company and str(company.name or "").strip():
        return str(company.name or "").strip()
    return "Your Company Name"


def _company_lines(company: CompanySetting | None) -> list[str]:
    if company is None:
        return [
            "Company Address Line 1",
            "Company Address Line 2",
            "Company Town, POSTCODE",
        ]

    lines: list[str] = []
    for value in (company.address_line1, company.address_line2):
        text = str(value or "").strip()
        if text:
            lines.append(text)

    city = str(company.city or "").strip()
    postcode = str(company.postcode or "").strip()
    if city and postcode:
        lines.append(f"{city}, {postcode}")
    elif city:
        lines.append(city)
    elif postcode:
        lines.append(postcode)

    country = str(company.country or "").strip()
    if country:
        lines.append(country)
    return lines


def _company_logo_path(company: CompanySetting | None) -> str:
    if company is None:
        return ""
    current = str(company.company_logo_path or "").strip()
    if current:
        return resolve_company_logo_web_path(current)
    return ""


def _company_logo_data_uri(logo_path: str) -> str:
    source = _logo_file_from_logo_path(logo_path)
    if source is None or not source.is_file():
        return ""
    try:
        payload = source.read_bytes()
    except OSError:
        return ""
    if not payload:
        return ""
    mime = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _logo_file_from_logo_path(logo_path: str) -> Path | None:
    normalized = str(logo_path or "").strip()
    if not normalized:
        return None

    if normalized.startswith("/static/uploads/company/"):
        filename = Path(normalized).name
        if not filename:
            return None
        for upload_root in _logo_upload_root_candidates():
            candidate = (upload_root / filename).resolve()
            if candidate.is_file():
                return candidate
        return None

    if normalized.startswith("/media/"):
        media_root = Path(str(settings.media_root or "").strip() or "app/media")
        relative = normalized.removeprefix("/media/").strip().lstrip("/\\")
        if not relative:
            return None
        candidate = (media_root.resolve() / relative).resolve()
        return candidate if candidate.is_file() else None

    absolute = Path(normalized)
    if absolute.is_absolute() and absolute.is_file():
        return absolute.resolve()
    return None


def _logo_upload_root_candidates() -> tuple[Path, ...]:
    configured = Path(
        str(settings.effective_company_logo_upload_dir or "").strip()
    )
    uploads_default = Path(str(settings.effective_uploads_dir or "").strip()) / "company"
    package_default = (
        Path(__file__).resolve().parents[1] / "static" / "uploads" / "company"
    )

    candidates: list[Path] = []
    for root in (configured, uploads_default, package_default):
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved in candidates:
            continue
        candidates.append(resolved)
    return tuple(candidates)


def _company_logo_url(company: CompanySetting | None) -> str:
    logo_path = _company_logo_path(company)
    if not logo_path:
        return ""
    if logo_path.startswith("data:"):
        return logo_path

    data_uri = _company_logo_data_uri(logo_path)
    if data_uri:
        return data_uri

    if logo_path.startswith(("http://", "https://")):
        return logo_path
    if logo_path.startswith("/"):
        base = str(settings.app_public_base_url or "").strip()
        if base:
            return urljoin(base, logo_path.lstrip("/"))
    return logo_path


def format_money(value: Any) -> str:
    if value is None:
        return "-"
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return "-"
    return f"{amount:,.2f}"


def format_date(value: Any, fmt: str = "%d/%m/%Y") -> str:
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return value.strftime(fmt)
    if isinstance(value, date):
        return value.strftime(fmt)
    text = str(value).strip()
    return text or "-"


def build_print_base_context(db: Session | None) -> dict[str, Any]:
    company_setting = _company_setting(db)
    company_name = _company_name(company_setting)
    company_lines = _company_lines(company_setting)
    company_logo_path = _company_logo_path(company_setting)
    company_logo_url = _company_logo_url(company_setting)

    company = {
        "name": company_name,
        "lines": company_lines,
        "logo_path": company_logo_path,
        "logo_url": company_logo_url,
    }

    return {
        "company": company,
        "company_name": company_name,
        "company_lines": company_lines,
        "company_logo_url": company_logo_url,
        "money": format_money,
        "fmt_money": format_money,
        "fmt_date": format_date,
    }


def default_print_base_context() -> dict[str, Any]:
    return build_print_base_context(db=None)
