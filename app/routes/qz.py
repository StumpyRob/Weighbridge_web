from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from ..services.qz_signing import (
    QzSigningConfigurationError,
    load_qz_certificate_text,
    sign_qz_message,
)

router = APIRouter()


class QzSignRequest(BaseModel):
    request: str


@router.get("/qz/certificate", response_class=PlainTextResponse)
def qz_certificate() -> PlainTextResponse:
    try:
        certificate_text = load_qz_certificate_text()
    except QzSigningConfigurationError as exc:
        return PlainTextResponse(str(exc), status_code=503)

    return PlainTextResponse(
        certificate_text,
        headers={"Cache-Control": "no-store"},
    )


@router.post("/qz/sign", response_class=PlainTextResponse)
def qz_sign(payload: QzSignRequest) -> PlainTextResponse:
    try:
        signature = sign_qz_message(payload.request)
    except ValueError as exc:
        return PlainTextResponse(str(exc), status_code=400)
    except QzSigningConfigurationError as exc:
        return PlainTextResponse(str(exc), status_code=503)

    return PlainTextResponse(
        signature,
        headers={"Cache-Control": "no-store"},
    )
