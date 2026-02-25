from sqlalchemy import select

from app.models import PrintDestination, PrintJob, PrintTemplate


def _create_template(
    db_session,
    *,
    code: str,
    document_type: str = "INVOICE",
    template_format: str = "HTML",
    content: str = "<html><body>{{ invoice.invoice_no }}</body></html>",
    is_system: bool = False,
) -> PrintTemplate:
    template = PrintTemplate(
        code=code,
        description=code,
        document_type=document_type,
        format=template_format,
        content=content,
        is_system=is_system,
        is_active=True,
    )
    db_session.add(template)
    db_session.commit()
    db_session.refresh(template)
    return template


def test_admin_printing_nav_shows_destinations_templates_jobs(client):
    response = client.get("/admin/printing/destinations")

    assert response.status_code == 200
    assert ">Destinations<" in response.text
    assert ">Templates<" in response.text
    assert ">Jobs<" in response.text
    assert ">Printers<" not in response.text


def test_admin_printing_root_url_redirects_to_destinations(client):
    response = client.get("/admin/printing", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/printing/destinations"


def test_destination_create_requires_matching_document_type_template(client, db_session):
    ticket_template = _create_template(
        db_session,
        code="ADMIN_TICKET_TEMPLATE",
        document_type="TICKET",
        template_format="TEXT",
        content="{{ payload.ticket_no }}",
    )

    response = client.post(
        "/admin/printing/destinations/new",
        data={
            "name": "Bad Invoice Destination",
            "description": "Mismatch",
            "document_type": "INVOICE",
            "template_id": str(ticket_template.id),
            "delivery_type": "PRINT_LOCAL_BROWSER",
            "is_default": "1",
            "is_active": "1",
        },
    )

    assert response.status_code == 400
    assert "Template document type must match destination document type." in response.text


def test_destination_create_and_list_shows_delivery_and_document_type(client, db_session):
    invoice_template = _create_template(db_session, code="ADMIN_INVOICE_TEMPLATE")

    create = client.post(
        "/admin/printing/destinations/new",
        data={
            "name": "Invoice Email",
            "description": "Email send",
            "document_type": "INVOICE",
            "template_id": str(invoice_template.id),
            "delivery_type": "EMAIL_PDF",
            "email_to": "accounts@example.com",
            "is_default": "1",
            "is_active": "1",
        },
        follow_redirects=False,
    )

    assert create.status_code == 303

    page = client.get("/admin/printing/destinations")
    assert page.status_code == 200
    assert "Invoice Email" in page.text
    assert "INVOICE" in page.text
    assert "EMAIL_PDF" in page.text

    destination = db_session.execute(
        select(PrintDestination).where(PrintDestination.name == "Invoice Email")
    ).scalars().first()
    assert destination is not None
    assert destination.document_type == "INVOICE"
    assert destination.delivery_type == "EMAIL_PDF"


def test_template_new_page_shows_template_variables_help_panel(client):
    response = client.get("/admin/printing/templates/new")

    assert response.status_code == 200
    assert "<summary class=\"frame-header\">Template variables</summary>" in response.text
    assert "Available in all templates" in response.text
    assert "company_name" in response.text
    assert "Document fields live under" in response.text
    assert "Recommended safe patterns" in response.text


def test_template_edit_page_shows_template_variables_help_panel(client, db_session):
    template = _create_template(db_session, code="HELP_PANEL_TEMPLATE")

    response = client.get(f"/admin/printing/templates/{template.id}/edit")

    assert response.status_code == 200
    assert "<summary class=\"frame-header\">Template variables</summary>" in response.text
    assert "Preview uses sample data unless opened from a real document." in response.text


def test_system_template_row_is_read_only_and_uses_duplicate_action_in_list(
    client,
    db_session,
):
    system_template = _create_template(
        db_session,
        code="SYSTEM_TEMPLATE_LIST",
        is_system=True,
    )
    _create_template(
        db_session,
        code="USER_TEMPLATE_LIST",
        is_system=False,
    )

    response = client.get("/admin/printing/templates")

    assert response.status_code == 200
    assert 'class="lookup-row--system-default"' in response.text
    assert "template-system-badge" not in response.text
    assert f'action="/admin/printing/templates/{system_template.id}/duplicate"' in response.text
    assert f'href="/admin/printing/templates/{system_template.id}/edit"' not in response.text
    assert f'data-row-link="/admin/printing/templates/{system_template.id}/edit"' not in response.text
    assert f'action="/admin/printing/templates/{system_template.id}/delete"' not in response.text


def test_destination_delete_removes_unused_non_default_destination(client, db_session):
    template = _create_template(
        db_session,
        code="DEST_DELETE_TEMPLATE",
        document_type="INVOICE",
    )
    destination = PrintDestination(
        name="Delete Me Destination",
        description="Temporary destination",
        document_type="INVOICE",
        template_id=int(template.id),
        delivery_type="PRINT_LOCAL_BROWSER",
        delivery_config={},
        is_default=False,
        is_active=True,
    )
    db_session.add(destination)
    db_session.commit()
    destination_id = int(destination.id)
    db_session.query(PrintJob).filter(PrintJob.destination_id == destination_id).delete()
    db_session.commit()

    response = client.post(
        f"/admin/printing/destinations/{destination_id}/delete",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert db_session.get(PrintDestination, destination_id) is None


def test_destination_delete_blocks_default_active_destination(client, db_session):
    template = _create_template(
        db_session,
        code="DEST_BLOCK_DEFAULT_TEMPLATE",
        document_type="TICKET",
    )
    destination = PrintDestination(
        name="Default Active Destination",
        description="Cannot delete while default + active",
        document_type="TICKET",
        template_id=int(template.id),
        delivery_type="PRINT_LOCAL_BROWSER",
        delivery_config={},
        is_default=True,
        is_active=True,
    )
    db_session.add(destination)
    db_session.commit()
    destination_id = int(destination.id)

    response = client.post(
        f"/admin/printing/destinations/{destination_id}/delete",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "error=Default+active+destinations+cannot+be+deleted." in response.headers["location"]
    assert db_session.get(PrintDestination, destination_id) is not None


def test_destination_delete_blocks_destination_with_jobs(client, db_session):
    template = _create_template(
        db_session,
        code="DEST_BLOCK_JOBS_TEMPLATE",
        document_type="WTN",
    )
    destination = PrintDestination(
        name="In Use Destination",
        description="Referenced by job",
        document_type="WTN",
        template_id=int(template.id),
        delivery_type="PRINT_LOCAL_BROWSER",
        delivery_config={},
        is_default=False,
        is_active=True,
    )
    db_session.add(destination)
    db_session.flush()
    db_session.add(
        PrintJob(
            document_type="WTN",
            destination_id=int(destination.id),
            template_id=int(template.id),
            delivery_type="PRINT_LOCAL_BROWSER",
            delivery_config_json={},
            status="QUEUED",
        )
    )
    db_session.commit()
    destination_id = int(destination.id)

    response = client.post(
        f"/admin/printing/destinations/{destination_id}/delete",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert (
        "error=Destination+is+in+use+by+one+or+more+jobs+and+cannot+be+deleted."
        in response.headers["location"]
    )
    assert db_session.get(PrintDestination, destination_id) is not None


def test_system_template_edit_page_is_read_only_with_duplicate_message(client, db_session):
    template = _create_template(
        db_session,
        code="SYSTEM_TEMPLATE_EDIT",
        is_system=True,
    )

    response = client.get(f"/admin/printing/templates/{template.id}/edit")

    assert response.status_code == 200
    assert "This is a system template. Duplicate it to customise." in response.text
    assert "Duplicate Template" in response.text
    assert "Save changes" not in response.text
    assert 'id="content" name="content"' in response.text
    assert "readonly" in response.text


def test_system_template_update_is_rejected_server_side(client, db_session):
    template = _create_template(
        db_session,
        code="SYSTEM_TEMPLATE_UPDATE",
        content="ORIGINAL",
        is_system=True,
    )

    response = client.post(
        f"/admin/printing/templates/{template.id}/edit",
        data={
            "code": template.code,
            "description": "Attempted update",
            "document_type": template.document_type,
            "format": template.format,
            "content": "CHANGED",
            "is_active": "1",
        },
    )

    assert response.status_code == 403
    assert "System templates cannot be edited. Duplicate to customise." in response.text
    db_session.refresh(template)
    assert template.content == "ORIGINAL"


def test_system_template_delete_is_rejected_server_side(client, db_session):
    template = _create_template(
        db_session,
        code="SYSTEM_TEMPLATE_DELETE",
        is_system=True,
    )

    response = client.post(f"/admin/printing/templates/{template.id}/delete")

    assert response.status_code == 403
    assert "System templates cannot be edited. Duplicate to customise." in response.text
    assert db_session.get(PrintTemplate, template.id) is not None


def test_duplicate_template_creates_editable_copy(client, db_session):
    source = _create_template(
        db_session,
        code="SYSTEM_TEMPLATE_DUPLICATE",
        document_type="WTN",
        template_format="HTML",
        content="<html><body>WTN DUP</body></html>",
        is_system=True,
    )

    response = client.post(
        f"/admin/printing/templates/{source.id}/duplicate",
        follow_redirects=False,
    )

    assert response.status_code == 303
    location = response.headers["location"]
    assert "/admin/printing/templates/" in location
    assert location.endswith("/edit?saved=1")

    copies = db_session.execute(
        select(PrintTemplate).where(
            PrintTemplate.document_type == source.document_type,
            PrintTemplate.content == source.content,
            PrintTemplate.id != source.id,
        )
    ).scalars().all()
    assert copies

    duplicate = copies[-1]
    assert duplicate.is_system is False
    assert duplicate.code is None
    assert str(duplicate.description or "").startswith("Copy of ")
    assert duplicate.format == source.format
    assert duplicate.document_type == source.document_type
    assert duplicate.content == source.content


def test_user_template_edit_remains_editable(client, db_session):
    template = _create_template(
        db_session,
        code="USER_TEMPLATE_EDITABLE",
        content="BEFORE",
        is_system=False,
    )

    page = client.get(f"/admin/printing/templates/{template.id}/edit")
    assert page.status_code == 200
    assert "Save changes" in page.text

    update = client.post(
        f"/admin/printing/templates/{template.id}/edit",
        data={
            "code": template.code,
            "description": template.description,
            "document_type": template.document_type,
            "format": template.format,
            "content": "AFTER",
            "is_active": "1",
        },
        follow_redirects=False,
    )

    assert update.status_code == 303
    db_session.refresh(template)
    assert template.content == "AFTER"


def test_destination_template_selector_shows_clean_template_names(client, db_session):
    _create_template(
        db_session,
        code="DEST_SYSTEM_TEMPLATE",
        is_system=True,
    )
    _create_template(
        db_session,
        code="DEST_CUSTOM_TEMPLATE",
        is_system=False,
    )

    response = client.get("/admin/printing/destinations/new")

    assert response.status_code == 200
    assert "DEST_SYSTEM_TEMPLATE" in response.text
    assert "DEST_CUSTOM_TEMPLATE" in response.text
    assert "(System)" not in response.text
    assert "(Custom)" not in response.text


def test_templates_list_places_system_templates_after_custom_templates(client, db_session):
    _create_template(
        db_session,
        code="A_SYSTEM_ORDER",
        document_type="TICKET",
        is_system=True,
    )
    _create_template(
        db_session,
        code="A_CUSTOM_ORDER",
        document_type="TICKET",
        is_system=False,
    )

    response = client.get("/admin/printing/templates?document_type=TICKET")

    assert response.status_code == 200
    custom_pos = response.text.find("A_CUSTOM_ORDER")
    system_pos = response.text.find("A_SYSTEM_ORDER")
    assert custom_pos >= 0
    assert system_pos >= 0
    assert custom_pos < system_pos
