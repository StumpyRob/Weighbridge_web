from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..services.platform_qz_settings import get_platform_qz_settings
from ..services.qz_signing import (
    QzSigningConfigurationError,
    load_qz_certificate_text,
    qz_public_route_error_message,
    sign_qz_message,
)

router = APIRouter()


class QzSignRequest(BaseModel):
    request: str


@router.get("/qz/certificate", response_class=PlainTextResponse)
def qz_certificate(db: Session = Depends(get_db)) -> PlainTextResponse:
    qz_settings = get_platform_qz_settings(db)
    try:
        if not qz_settings.qz_enabled:
            raise QzSigningConfigurationError(
                qz_public_route_error_message(enabled=False)
            )
        certificate_text = load_qz_certificate_text(db=db)
    except QzSigningConfigurationError as exc:
        detail = str(exc).strip()
        if detail not in {
            qz_public_route_error_message(enabled=False),
        }:
            detail = qz_public_route_error_message(enabled=True)
        return PlainTextResponse(detail, status_code=503)

    return PlainTextResponse(
        certificate_text,
        headers={"Cache-Control": "no-store"},
    )


@router.post("/qz/sign", response_class=PlainTextResponse)
def qz_sign(
    payload: QzSignRequest,
    db: Session = Depends(get_db),
) -> PlainTextResponse:
    qz_settings = get_platform_qz_settings(db)
    try:
        if not qz_settings.qz_enabled:
            raise QzSigningConfigurationError(
                qz_public_route_error_message(enabled=False)
            )
        signature = sign_qz_message(payload.request, db=db)
    except ValueError as exc:
        return PlainTextResponse(str(exc), status_code=400)
    except QzSigningConfigurationError as exc:
        detail = str(exc).strip()
        if detail not in {
            qz_public_route_error_message(enabled=False),
        }:
            detail = qz_public_route_error_message(enabled=True)
        return PlainTextResponse(detail, status_code=503)

    return PlainTextResponse(
        signature,
        headers={"Cache-Control": "no-store"},
    )
