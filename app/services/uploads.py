from __future__ import annotations

from pathlib import Path

from ..config import settings

LOGO_WEB_PATH_PREFIX = "/static/uploads/company/"


def _logo_upload_root_candidates() -> tuple[Path, ...]:
    configured = Path(str(settings.effective_company_logo_upload_dir or "").strip())
    service_default = Path("app/static/uploads/company")
    package_default = (
        Path(__file__).resolve().parents[1] / "static" / "uploads" / "company"
    )
    docker_alt = Path("/app/static/uploads/company")
    docker_repo = Path("/app/app/static/uploads/company")

    candidates: list[Path] = []
    for root in (configured, service_default, package_default, docker_alt, docker_repo):
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved in candidates:
            continue
        candidates.append(resolved)
    return tuple(candidates)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_company_logo_web_path(path: str | None) -> str:
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
        for root in _logo_upload_root_candidates():
            if _is_within(resolved, root):
                return f"{LOGO_WEB_PATH_PREFIX}{filename}"
        return f"{LOGO_WEB_PATH_PREFIX}{filename}"

    lowered = normalized.replace("/", "\\").lower()
    if "static\\uploads\\company\\" in lowered or "uploads\\company\\" in lowered:
        return f"{LOGO_WEB_PATH_PREFIX}{filename}"
    if normalized.startswith("/"):
        return normalized
    return f"{LOGO_WEB_PATH_PREFIX}{filename}"
