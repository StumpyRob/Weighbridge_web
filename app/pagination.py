from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlencode

from fastapi import Request
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


def normalize_page_number(value: int | None) -> int:
    try:
        page = int(value or 1)
    except (TypeError, ValueError):
        page = 1
    return max(page, 1)


def normalize_page_size(
    value: int | None,
    *,
    default_page_size: int = DEFAULT_PAGE_SIZE,
    max_page_size: int = MAX_PAGE_SIZE,
) -> int:
    try:
        page_size = int(value or default_page_size)
    except (TypeError, ValueError):
        page_size = default_page_size
    return min(max(page_size, 1), max_page_size)


def count_rows(db: Session, stmt: Select) -> int:
    return int(
        db.execute(select(func.count()).select_from(stmt.order_by(None).subquery())).scalar()
        or 0
    )


def build_pagination_context(
    request: Request,
    *,
    page: int,
    page_size: int,
    total_count: int,
    singular_label: str = "record",
    plural_label: str = "records",
) -> dict[str, object]:
    resolved_page = normalize_page_number(page)
    resolved_page_size = normalize_page_size(page_size)
    resolved_total_count = max(int(total_count or 0), 0)
    total_pages = max(
        (resolved_total_count + resolved_page_size - 1) // resolved_page_size,
        1,
    )
    resolved_page = min(resolved_page, total_pages)
    start_index = 0 if resolved_total_count == 0 else ((resolved_page - 1) * resolved_page_size) + 1
    end_index = 0 if resolved_total_count == 0 else min(
        resolved_page * resolved_page_size,
        resolved_total_count,
    )
    query_pairs = [
        (key, value)
        for key, value in request.query_params.multi_items()
        if key != "page"
    ]
    select_id = _pagination_select_id(str(request.url.path))

    return {
        "page": resolved_page,
        "page_size": resolved_page_size,
        "total_pages": total_pages,
        "total_count": resolved_total_count,
        "start_index": start_index,
        "end_index": end_index,
        "has_multiple_pages": total_pages > 1,
        "label": singular_label if resolved_total_count == 1 else plural_label,
        "query_pairs": query_pairs,
        "form_action": str(request.url.path),
        "select_id": select_id,
        "page_options": list(range(1, total_pages + 1)),
        "previous_url": build_page_url(
            str(request.url.path),
            query_pairs,
            resolved_page - 1,
        )
        if resolved_page > 1
        else "",
        "next_url": build_page_url(
            str(request.url.path),
            query_pairs,
            resolved_page + 1,
        )
        if resolved_page < total_pages
        else "",
    }


def slice_page_items(
    items: list[object] | tuple[object, ...],
    pagination: dict[str, object],
) -> list[object]:
    page = int(pagination["page"])
    page_size = int(pagination["page_size"])
    start = (page - 1) * page_size
    end = start + page_size
    return list(items[start:end])


def build_page_url(
    path: str,
    query_pairs: Iterable[tuple[str, str]],
    page: int,
) -> str:
    params = list(query_pairs)
    params.append(("page", str(normalize_page_number(page))))
    encoded = urlencode(params, doseq=True)
    return f"{path}?{encoded}" if encoded else path


def _pagination_select_id(path: str) -> str:
    cleaned = path.strip("/").replace("/", "-") or "root"
    return f"page-jump-{cleaned}"
