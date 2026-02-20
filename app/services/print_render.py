from __future__ import annotations

from pathlib import Path

from ..config import settings
from ..templating import templates


def _normalize_template_name(template_name: str) -> str:
    normalized = str(template_name or "").strip()
    if normalized.startswith("print/"):
        normalized = normalized[len("print/") :]
    return normalized.lstrip("/")


def _resolve_builtin_template_path(template_name: str) -> str:
    normalized = _normalize_template_name(template_name)
    return f"print/{normalized}"


def _resolve_override_template_path(template_name: str) -> Path | None:
    override_root_raw = (settings.print_template_override_dir or "").strip()
    if not override_root_raw:
        return None

    override_root = Path(override_root_raw).expanduser().resolve()
    relative_name = _normalize_template_name(template_name)
    candidate = (override_root / relative_name).resolve()
    if candidate != override_root and override_root not in candidate.parents:
        return None
    if not candidate.is_file():
        return None
    return candidate


def _render_from_template_name(payload: dict, template_name: str) -> str:
    override_path = _resolve_override_template_path(template_name)
    if override_path is not None:
        template_text = override_path.read_text(encoding="utf-8")
        return templates.env.from_string(template_text).render(payload=payload)

    template = templates.env.get_template(_resolve_builtin_template_path(template_name))
    return template.render(payload=payload)


def render_thermal(
    payload: dict,
    template_name: str = "thermal_default.txt",
) -> str:
    return _render_from_template_name(payload, template_name)


def render_a4_html(
    payload: dict,
    template_name: str = "a4_default.html",
) -> str:
    return _render_from_template_name(payload, template_name)
