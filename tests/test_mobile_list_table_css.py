import re


def test_mobile_list_tables_drop_fixed_min_width_and_wrap_actions(client_anonymous):
    response = client_anonymous.get("/static/css/style.css")

    assert response.status_code == 200
    normalized = re.sub(r"\s+", " ", response.text)
    compact = response.text.replace(" ", "").replace("\n", "")

    assert "min-width:600px;" not in compact
    assert ".list-table{min-width:100%;}" in compact
    assert re.search(
        r"\.data-table\s+\.actions-col\s+\.btn-group,\s*\.data-table\s+\.actions-col\s+\.actions\s*\{[^}]*flex-wrap:\s*wrap;[^}]*white-space:\s*normal;",
        normalized,
        flags=re.IGNORECASE,
    )


def test_checkbox_controls_have_touch_friendly_hit_area(client_anonymous):
    response = client_anonymous.get("/static/css/style.css")

    assert response.status_code == 200
    normalized = re.sub(r"\s+", " ", response.text)

    assert re.search(
        r"\.field input\[type=\"checkbox\"\]\s*\{[^}]*width:\s*18px;[^}]*height:\s*18px;",
        normalized,
        flags=re.IGNORECASE,
    )
    assert re.search(
        r"\.checkbox\s*\{[^}]*min-height:\s*44px;",
        normalized,
        flags=re.IGNORECASE,
    )
    assert re.search(
        r"\.checkbox label\s*\{[^}]*min-height:\s*44px;",
        normalized,
        flags=re.IGNORECASE,
    )
