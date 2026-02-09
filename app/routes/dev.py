from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from .debug import router as debug_router

router = APIRouter()
router.include_router(debug_router, tags=["debug"])


@router.get("/__routes", response_class=PlainTextResponse)
def routes_catalog(request: Request) -> PlainTextResponse:
    lines: list[str] = []
    for route in request.app.routes:
        path = getattr(route, "path", "")
        methods = ",".join(sorted(getattr(route, "methods", []) or []))
        if "swap" in path or "tickets" in path:
            lines.append(f"{methods:20} {path}")
    return PlainTextResponse("\n".join(lines))
