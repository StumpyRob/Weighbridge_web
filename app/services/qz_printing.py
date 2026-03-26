from __future__ import annotations


def qz_printer_name_from_delivery_config(config: dict | None) -> str:
    if not isinstance(config, dict):
        return ""
    for key in ("qz_printer_name", "printer_name", "printer"):
        value = str(config.get(key, "") or "").strip()
        if value:
            return value
    return ""


def qz_direct_print_enabled_from_destination(
    *,
    delivery_type: str | None,
    delivery_config: dict | None,
    local_browser_delivery_type: str,
) -> bool:
    normalized_delivery_type = str(delivery_type or "").strip().upper()
    if normalized_delivery_type != str(local_browser_delivery_type or "").strip().upper():
        return False
    config = dict(delivery_config or {}) if isinstance(delivery_config, dict) else {}
    if "qz_direct_print_enabled" in config:
        return str(config.get("qz_direct_print_enabled", "") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    return bool(qz_printer_name_from_delivery_config(config))
