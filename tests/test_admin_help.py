def test_admin_help_root_redirects_to_getting_started(client):
    response = client.get("/admin/help", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/help/getting-started"


def test_admin_help_pages_render(client):
    getting_started = client.get("/admin/help/getting-started")
    assert getting_started.status_code == 200
    assert "Quick Start Checklist" in getting_started.text
    assert "Customer, Vehicle, and Product Controls" in getting_started.text
    assert "Print Agents, Destinations, Templates, and Jobs" in getting_started.text
    assert "Audit, Support, and UK Time" in getting_started.text
    assert "totals and the live clock use UK time" in getting_started.text

    template_vars = client.get("/admin/help/template-variables")
    assert template_vars.status_code == 200
    assert "Template Variables" in template_vars.text
    assert "payload.document_type" in template_vars.text
    assert "company_logo_url" in template_vars.text
    assert "fmt_date(value, format)" in template_vars.text
    assert "payload.producer_signature_data_uri" in template_vars.text
    assert "payload.carrier_signature_signed_at_iso" in template_vars.text
    assert "payload.receiver_signature_signer_name" in template_vars.text
    assert "payload.wtn_signature_data_uri" in template_vars.text
    assert "Variable Name Quick Reference" in template_vars.text
    assert "Legacy receiver alias" in template_vars.text


def test_legacy_printing_template_variables_route_redirects(client):
    response = client.get("/admin/printing/template-variables", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/help/template-variables"
