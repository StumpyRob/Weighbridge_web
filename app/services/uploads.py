from __future__ import annotations

from pathlib import Path

from ..config import settings
from ..tenancy import current_tenant_id

LOGO_WEB_PATH_PREFIX = "/static/uploads/company/"
_TENANT_UPLOADS_SEGMENT = "tenants"
_COMPANY_SEGMENT = "company"


def uploads_root() -> Path:
    return Path(str(settings.effective_uploads_dir or "").strip()).resolve()


def company_logo_storage_layout(root: Path | None = None) -> str:
    base = (root or uploads_root()).resolve().as_posix().rstrip("/")
    return f"{base}/{_TENANT_UPLOADS_SEGMENT}/<tenant_id>/{_COMPANY_SEGMENT}"


def company_logo_upload_dir(
    tenant_id: int | None = None,
    *,
    create: bool = True,
) -> Path:
    resolved_tenant_id = tenant_id if tenant_id is not None else current_tenant_id()
    if resolved_tenant_id is not None:
        target = (
            uploads_root() / _TENANT_UPLOADS_SEGMENT / str(int(resolved_tenant_id)) / _COMPANY_SEGMENT
        ).resolve()
    else:
        target = Path(str(settings.effective_company_logo_upload_dir or "").strip()).resolve()
    if create:
        target.mkdir(parents=True, exist_ok=True)
    return target


def _logo_upload_root_candidates(tenant_id: int | None = None) -> tuple[Path, ...]:
    primary = company_logo_upload_dir(tenant_id=tenant_id, create=False)
    legacy_upload_default = (uploads_root() / _COMPANY_SEGMENT).resolve()
    package_default = Path(__file__).resolve().parents[1] / "static" / "uploads" / "company"

    candidates: list[Path] = []
    for root in (primary, legacy_upload_default, package_default.resolve()):
        if root in candidates:
            continue
        candidates.append(root)
    return tuple(candidates)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def logo_file_from_web_path(path: str | None, tenant_id: int | None = None) -> Path | None:
    normalized = str(path or "").strip()
    if not normalized.startswith(LOGO_WEB_PATH_PREFIX):
        return None
    filename = Path(normalized).name
    if not filename:
        return None

    for root in _logo_upload_root_candidates(tenant_id=tenant_id):
        try:
            candidate = (root / filename).resolve()
        except OSError:
            continue
        if candidate.is_file():
            return candidate

    primary = company_logo_upload_dir(tenant_id=tenant_id, create=False)
    try:
        return (primary / filename).resolve()
    except OSError:
        return None


def resolve_company_logo_web_path(
    path: str | None,
    *,
    tenant_id: int | None = None,
) -> str:
    normalized = str(path or "").strip()
    if not normalized:
        return ""
    if normalized.startswith(("http://", "https://", "data:")):
        return normalized
    if normalized.startswith("/media/"):
        return normalized
    if normalized.startswith("/static/uploads/"):
        return normalized

    source = Path(normalized)
    filename = source.name
    if not filename:
        return ""

    if source.is_absolute():
        try:
            resolved = source.resolve()
        except OSError:
            resolved = source
        for root in _logo_upload_root_candidates(tenant_id=tenant_id):
            if _is_within(resolved, root):
                return f"{LOGO_WEB_PATH_PREFIX}{filename}"
        return f"{LOGO_WEB_PATH_PREFIX}{filename}"

    lowered = normalized.replace("/", "\\").lower()
    if "static\\uploads\\company\\" in lowered or "uploads\\company\\" in lowered:
        return f"{LOGO_WEB_PATH_PREFIX}{filename}"
    if normalized.startswith("/"):
        return normalized
    return f"{LOGO_WEB_PATH_PREFIX}{filename}"
