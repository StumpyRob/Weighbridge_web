def normalize_unit_name(name: str) -> str:
    return " ".join(str(name or "").strip().lower().split())


WEIGHT_UNIT_CANONICAL = {"kg", "tonne", "tonnes"}
WEIGHT_UNIT_DISPLAY = {"kg": "KG", "tonne": "Tonnes", "tonnes": "Tonnes"}


def is_allowed_weight_unit(name: str) -> bool:
    return normalize_unit_name(name) in WEIGHT_UNIT_CANONICAL


def canonical_weight_unit(name: str) -> str | None:
    normalized = normalize_unit_name(name)
    return WEIGHT_UNIT_DISPLAY.get(normalized)
