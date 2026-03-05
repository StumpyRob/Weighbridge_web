from __future__ import annotations

import io
import logging
import csv
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from starlette.datastructures import UploadFile

from ..auth import is_superadmin_user
from ..db import get_db
from ..models import EwcCode, EwcImportLog, User
from ..services.ewc_import import import_ewc_codes, parse_import_errors_json
from ..tenancy import request_platform_mode
from ..templating import templates

router = APIRouter()
logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def _require_platform_superadmin(request: Request, db: Session) -> None:
    if not request_platform_mode(request):
        raise HTTPException(status_code=404, detail="Not Found")
    current_user = getattr(getattr(request, "state", None), "current_user", None)
    if not isinstance(current_user, User):
        raise HTTPException(status_code=403, detail="Forbidden")
    if not is_superadmin_user(db, current_user):
        raise HTTPException(status_code=403, detail="Forbidden")


def _build_page_context(
    request: Request,
    db: Session,
    *,
    error: str = "",
) -> dict:
    total_codes = (
        db.execute(select(func.count(EwcCode.id))).scalar_one_or_none() or 0
    )
    active_codes = (
        db.execute(
            select(func.count(EwcCode.id)).where(EwcCode.active.is_(True))
        ).scalar_one_or_none()
        or 0
    )
    inactive_codes = int(total_codes) - int(active_codes)
    try:
        last_log = (
            db.execute(
                select(EwcImportLog).order_by(
                    EwcImportLog.imported_at.desc(),
                    EwcImportLog.id.desc(),
                )
            )
            .scalars()
            .first()
        )
    except SQLAlchemyError:
        last_log = None

    return {
        "request": request,
        "error": error,
        "total_codes": int(total_codes),
        "active_codes": int(active_codes),
        "inactive_codes": int(inactive_codes),
        "last_log": last_log,
        "last_log_errors": parse_import_errors_json(last_log.errors_json if last_log else "[]"),
        "saved": request.query_params.get("imported") == "1",
    }


@router.get("/admin/ewc-codes", response_class=HTMLResponse)
def admin_ewc_codes(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    _require_platform_superadmin(request, db)
    return templates.TemplateResponse(
        request,
        "admin/ewc_codes.html",
        _build_page_context(request, db),
    )


@router.post("/admin/ewc-codes", response_class=HTMLResponse)
async def admin_ewc_codes_import(
    request: Request,
    db: Session = Depends(get_db),
):
    _require_platform_superadmin(request, db)
    form = await request.form()
    file_obj = form.get("csv_file")
    replace_existing = str(form.get("replace_existing", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    if file_obj is None or not isinstance(file_obj, UploadFile):
        return templates.TemplateResponse(
            request,
            "admin/ewc_codes.html",
            _build_page_context(request, db, error="Please choose a CSV file to upload."),
            status_code=400,
        )

    filename = str(getattr(file_obj, "filename", "") or "").strip()
    if not filename.lower().endswith(".csv"):
        return templates.TemplateResponse(
            request,
            "admin/ewc_codes.html",
            _build_page_context(request, db, error="Only .csv files are supported."),
            status_code=400,
        )

    payload = await file_obj.read()
    if len(payload) > MAX_UPLOAD_BYTES:
        return templates.TemplateResponse(
            request,
            "admin/ewc_codes.html",
            _build_page_context(
                request,
                db,
                error=f"File is too large. Maximum allowed size is {MAX_UPLOAD_BYTES // (1024 * 1024)}MB.",
            ),
            status_code=400,
        )

    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        return templates.TemplateResponse(
            request,
            "admin/ewc_codes.html",
            _build_page_context(
                request,
                db,
                error="CSV must be UTF-8 encoded.",
            ),
            status_code=400,
        )

    imported_by = "admin"
    if request.client and request.client.host:
        imported_by = f"admin:{request.client.host}"

    result = import_ewc_codes(
        io.StringIO(text),
        replace=replace_existing,
        db=db,
        imported_by=imported_by,
        source_name=filename,
    )
    if result.fatal_error:
        logger.warning(
            "EWC import failed: %s (file=%s replace=%s)",
            result.fatal_error,
            filename,
            replace_existing,
        )
        return templates.TemplateResponse(
            request,
            "admin/ewc_codes.html",
            _build_page_context(request, db, error=result.fatal_error),
            status_code=400,
        )

    logger.info(
        "EWC import complete file=%s inserted=%s updated=%s unchanged=%s skipped=%s deactivated=%s errors=%s replace=%s",
        filename,
        result.inserted,
        result.updated,
        result.unchanged,
        result.skipped,
        result.deactivated,
        result.error_count,
        replace_existing,
    )
    return RedirectResponse(url="/admin/ewc-codes?imported=1", status_code=303)


@router.get("/admin/ewc-codes/sample.csv")
def admin_ewc_codes_sample_csv(
    request: Request,
    db: Session = Depends(get_db),
) -> PlainTextResponse:
    _require_platform_superadmin(request, db)
    rows = list(
        db.execute(select(EwcCode).order_by(EwcCode.code_6.asc())).scalars()
    )
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["code", "description", "hazardous", "active"])
    for row in rows:
        writer.writerow(
            [
                str(row.code_display or row.code_6 or ""),
                str(row.description or ""),
                "true" if bool(row.hazardous) else "false",
                "true" if bool(row.active) else "false",
            ]
        )
    response = PlainTextResponse(output.getvalue(), media_type="text/csv; charset=utf-8")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    response.headers["Content-Disposition"] = (
        f'attachment; filename="ewc_codes_{timestamp}.csv"'
    )
    return response
