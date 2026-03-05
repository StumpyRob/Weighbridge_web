from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
import mimetypes

from sqlalchemy import text
from sqlalchemy.engine.url import URL, make_url
from sqlalchemy.orm import Session

from ..config import settings
from ..models import CompanySetting, PrintDestination
from .pdf import (
    check_invoice_pdf_renderer,
    resolve_default_template_for_document_type,
)
from .uploads import company_logo_storage_layout, logo_file_from_web_path, uploads_root

HEALTH_TEMPLATE_DOCUMENT_TYPES: tuple[str, ...] = (
    "TICKET",
    "INVOICE",
    "WTN",
)


@dataclass(slots=True, frozen=True)
class HealthCheckResult:
    status: str
    summary: str
    details: dict[str, Any]


def collect_system_health(db: Session) -> dict[str, HealthCheckResult]:
    return {
        "renderer": check_renderer(),
        "templates": check_templates(db),
        "logo": check_logo(db),
        "uploads": check_uploads(),
        "pdf_storage": check_pdf_storage(),
        "print_jobs": check_print_jobs(db),
        "db": check_db_size(db),
    }


def check_renderer() -> HealthCheckResult:
    renderer_status = check_invoice_pdf_renderer()
    version = ""
    import_error = ""
    try:
        import weasyprint  # type: ignore

        version = str(getattr(weasyprint, "__version__", "") or "")
    except Exception as exc:
        import_error = str(exc).strip() or exc.__class__.__name__

    if renderer_status.available:
        summary = "OK"
        if version:
            summary = f"OK (WeasyPrint {version})"
        return HealthCheckResult(
            status="ok",
            summary=summary,
            details={
                "available": True,
                "fallback_mode_active": False,
                "version": version or None,
                "error": None,
            },
        )

    error_text = renderer_status.detail or import_error or "Unknown renderer error."
    summary = "FALLBACK"
    if error_text:
        summary = f"FALLBACK ({error_text})"
    return HealthCheckResult(
        status="error",
        summary=summary,
        details={
            "available": False,
            "fallback_mode_active": True,
            "version": version or None,
            "error": error_text,
        },
    )


def check_templates(db: Session) -> HealthCheckResult:
    details_by_document_type: dict[str, dict[str, Any]] = {}
    has_warning = False
    ok_count = 0

    for document_type in HEALTH_TEMPLATE_DOCUMENT_TYPES:
        default_destinations = list(
            db.query(PrintDestination)
            .filter(
                PrintDestination.document_type == document_type,
                PrintDestination.is_default.is_(True),
                PrintDestination.is_active.is_(True),
            )
            .order_by(PrintDestination.id.asc())
            .all()
        )
        resolved_default = resolve_default_template_for_document_type(
            db,
            document_type=document_type,
            require_active=True,
        )
        warnings: list[str] = []
        if len(default_destinations) != 1:
            warnings.append(
                f"Expected exactly one active default destination; found {len(default_destinations)}."
            )
        if resolved_default is None:
            warnings.append("No active template resolved from the default destination.")
        elif not bool(resolved_default.is_active):
            warnings.append("Resolved template is not active.")

        status = "ok" if not warnings else "warning"
        if status == "warning":
            has_warning = True
        else:
            ok_count += 1

        default_destination = default_destinations[0] if default_destinations else None
        details_by_document_type[document_type] = {
            "status": status,
            "default_destination_name": (
                str(default_destination.name) if default_destination else None
            ),
            "default_destination_count": len(default_destinations),
            "resolved_template_id": int(resolved_default.id) if resolved_default else None,
            "resolved_template_code": str(resolved_default.code) if resolved_default else None,
            "resolved_template_active": bool(resolved_default.is_active) if resolved_default else False,
            "warnings": warnings,
        }

    overall_status = "warning" if has_warning else "ok"
    summary = (
        f"OK ({ok_count}/{len(HEALTH_TEMPLATE_DOCUMENT_TYPES)} document types healthy)"
        if not has_warning
        else "WARNING (template default issues found)"
    )
    return HealthCheckResult(
        status=overall_status,
        summary=summary,
        details={
            "document_types": details_by_document_type,
        },
    )


def check_logo(db: Session) -> HealthCheckResult:
    company = (
        db.query(CompanySetting).order_by(CompanySetting.id.asc()).limit(1).first()
    )
    logo_path = str(getattr(company, "company_logo_path", "") or "").strip()
    if not logo_path:
        return HealthCheckResult(
            status="warning",
            summary="NOT SET",
            details={
                "logo_path": None,
                "exists": False,
                "size_bytes": 0,
                "mime_type": None,
            },
        )

    resolved = _resolve_logo_file_path(logo_path)
    if resolved is None or not resolved.is_file():
        return HealthCheckResult(
            status="error",
            summary="MISSING FILE",
            details={
                "logo_path": logo_path,
                "resolved_path": str(resolved) if resolved else None,
                "exists": False,
                "size_bytes": 0,
                "mime_type": mimetypes.guess_type(logo_path)[0],
            },
        )

    mime_type = mimetypes.guess_type(resolved.name)[0]
    size_bytes = resolved.stat().st_size
    return HealthCheckResult(
        status="ok",
        summary=f"OK ({resolved.name}, {_format_size_megabytes(size_bytes):.2f} MB)",
        details={
            "logo_path": logo_path,
            "resolved_path": str(resolved),
            "exists": True,
            "size_bytes": size_bytes,
            "mime_type": mime_type,
        },
    )


def check_uploads() -> HealthCheckResult:
    upload_dir = uploads_root()
    exists = upload_dir.is_dir()
    writable = _is_dir_writable(upload_dir) if exists else False
    file_count, total_size_bytes = _scan_directory_file_stats(upload_dir) if exists else (0, 0)

    if exists and writable:
        status = "ok"
        summary = f"OK ({file_count} files, {_format_size_megabytes(total_size_bytes):.2f} MB)"
    elif exists and not writable:
        status = "warning"
        summary = "WARNING (directory not writable)"
    else:
        status = "error"
        summary = "MISSING DIRECTORY"

    return HealthCheckResult(
        status=status,
        summary=summary,
        details={
            "path": str(upload_dir),
            "layout": company_logo_storage_layout(upload_dir),
            "exists": exists,
            "writable": writable,
            "file_count": file_count,
            "size_bytes": total_size_bytes,
        },
    )


def check_pdf_storage() -> HealthCheckResult:
    roots = (
        Path("uploads"),
        Path("static"),
        Path("app/static"),
        Path("tmp"),
        Path("app/tmp"),
    )
    found: list[str] = []
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        for path in root.rglob("*.pdf"):
            found.append(str(path))

    has_pdf_files = bool(found)
    status = "warning" if has_pdf_files else "ok"
    summary = "WARNING (PDF files found on disk)" if has_pdf_files else "OK (no PDFs found)"
    return HealthCheckResult(
        status=status,
        summary=summary,
        details={
            "pdfs_on_disk": has_pdf_files,
            "count": len(found),
            "sample_paths": found[:10],
            "scan_roots": [str(root) for root in roots],
        },
    )


def check_print_jobs(db: Session) -> HealthCheckResult:
    row = db.execute(
        text(
            "SELECT COUNT(*) AS count, MIN(created_at) AS oldest, MAX(created_at) AS newest "
            "FROM print_jobs"
        )
    ).first()
    total_count = int(row.count) if row and row.count is not None else 0
    oldest = row.oldest if row else None
    newest = row.newest if row else None

    summary = f"{total_count} rows"
    if oldest and newest:
        summary = f"{total_count} rows ({oldest} to {newest})"
    return HealthCheckResult(
        status="ok",
        summary=summary,
        details={
            "count": total_count,
            "oldest": str(oldest) if oldest is not None else None,
            "newest": str(newest) if newest is not None else None,
        },
    )


def check_db_size(db: Session) -> HealthCheckResult:
    bind = db.get_bind()
    url_obj = getattr(bind, "url", None)
    if url_obj is None:
        database_url = str(settings.database_url or "").strip()
        url_obj = make_url(database_url)
    driver = str(url_obj.drivername or "").lower()

    if driver.startswith("sqlite"):
        sqlite_size = _sqlite_db_size(url_obj)
        return HealthCheckResult(
            status="ok",
            summary=f"{_format_size_megabytes(sqlite_size['size_bytes']):.2f} MB",
            details=sqlite_size,
        )

    if "postgres" in driver:
        row = db.execute(text("SELECT pg_database_size(current_database())")).first()
        size_bytes = int(row[0]) if row and row[0] is not None else 0
        return HealthCheckResult(
            status="ok",
            summary=f"{_format_size_megabytes(size_bytes):.2f} MB",
            details={
                "engine": driver,
                "size_bytes": size_bytes,
            },
        )

    return HealthCheckResult(
        status="warning",
        summary=f"Unsupported engine ({driver})",
        details={
            "engine": driver,
            "size_bytes": None,
        },
    )


def _sqlite_db_size(database_url: URL) -> dict[str, Any]:
    db_name = str(database_url.database or "").strip()
    if db_name in {"", ":memory:"}:
        return {
            "engine": "sqlite",
            "path": db_name or ":memory:",
            "exists": False,
            "size_bytes": 0,
        }

    db_path = Path(db_name)
    if not db_path.is_absolute():
        db_path = (Path.cwd() / db_path).resolve()
    else:
        db_path = db_path.resolve()

    size_bytes = db_path.stat().st_size if db_path.is_file() else 0
    return {
        "engine": "sqlite",
        "path": str(db_path),
        "exists": db_path.is_file(),
        "size_bytes": size_bytes,
    }


def _resolve_logo_file_path(logo_path: str) -> Path | None:
    normalized = str(logo_path or "").strip()
    if not normalized:
        return None
    if normalized.startswith("/static/uploads/company/"):
        return logo_file_from_web_path(normalized)
    if normalized.startswith("/media/"):
        media_root = Path(str(settings.media_root or "").strip() or "app/media").resolve()
        relative = normalized.removeprefix("/media/").strip().lstrip("/\\")
        if not relative:
            return None
        candidate = (media_root / relative).resolve()
        return candidate if candidate.is_file() else None
    maybe_path = Path(normalized)
    if maybe_path.is_absolute() and maybe_path.is_file():
        return maybe_path.resolve()
    return None


def _is_dir_writable(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    try:
        with NamedTemporaryFile(dir=directory, delete=True):
            return True
    except OSError:
        return False


def _scan_directory_file_stats(directory: Path) -> tuple[int, int]:
    count = 0
    size_bytes = 0
    for item in directory.rglob("*"):
        if not item.is_file():
            continue
        count += 1
        try:
            size_bytes += item.stat().st_size
        except OSError:
            continue
    return count, size_bytes


def _format_size_megabytes(size_bytes: int) -> float:
    if size_bytes <= 0:
        return 0.0
    return size_bytes / (1024 * 1024)
