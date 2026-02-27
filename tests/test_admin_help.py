def test_admin_help_root_redirects_to_getting_started(client):
    response = client.get("/admin/help", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/help/getting-started"


def test_admin_help_pages_render(client):
    getting_started = client.get("/admin/help/getting-started")
    assert getting_started.status_code == 200
    assert "Quick Start Checklist" in getting_started.text

    template_vars = client.get("/admin/help/template-variables")
    assert template_vars.status_code == 200
    assert "Template Variables" in template_vars.text
    assert "payload.document_type" in template_vars.text
    assert "company_logo_url" in template_vars.text
    assert "fmt_date(value, format)" in template_vars.text


def test_legacy_printing_template_variables_route_redirects(client):
    response = client.get("/admin/printing/template-variables", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/help/template-variables"
