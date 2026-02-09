import re

_EVENT_HANDLER_RE = re.compile(r"\bon[a-z0-9_]+\s*=", re.IGNORECASE)
_JAVASCRIPT_SCHEME_RE = re.compile(r"javascript\s*:", re.IGNORECASE)


def has_unsafe_markup(value: str | None) -> bool:
    if value is None:
        return False
    text = str(value).strip()
    if not text:
        return False
    if "<" in text or ">" in text:
        return True
    if _EVENT_HANDLER_RE.search(text):
        return True
    if _JAVASCRIPT_SCHEME_RE.search(text):
        return True
    return False


def validate_no_html(value: str | None, field_label: str, errors: list[str]) -> None:
    if has_unsafe_markup(value):
        errors.append(f"{field_label}: HTML is not allowed.")


def validate_no_html_fields(
    field_values: dict[str, str | None], errors: list[str]
) -> None:
    for field_label, value in field_values.items():
        validate_no_html(value, field_label, errors)
