from sqlalchemy import func, select

from app.models import Tenant, Ticket, TicketStatusEnum, TransactionTypeEnum, Vehicle
from app.services.demo_dataset import DEMO_SIGNATURE_DATA_URI, seed_demo_dataset
from app.services.signatures import normalize_png_data_url, png_has_visible_ink
from app.services.system_setup import DEFAULT_YARD_NAME, seed_required_reference_data, upsert_default_yard


def test_seed_demo_dataset_includes_wtn_signature_mix_and_vehicle_default_mix(db_session):
    tenant = Tenant(name="Demo Seed Tenant", subdomain="demo-seed", is_active=True, is_demo=True)
    db_session.add(tenant)
    db_session.commit()
    db_session.refresh(tenant)
    assert tenant.id == 1

    db_session.info["tenant_id"] = tenant.id
    db_session.info["platform_mode"] = False

    seed_required_reference_data(db_session)
    upsert_default_yard(db_session, yard_name=DEFAULT_YARD_NAME)
    db_session.commit()

    counts = seed_demo_dataset(db_session, tenant_id=tenant.id)
    db_session.commit()

    assert counts["tickets_waste"] == 8

    completed_waste_tickets = db_session.execute(
        select(Ticket).where(
            Ticket.tenant_id == tenant.id,
            Ticket.status == TicketStatusEnum.COMPLETE.value,
            Ticket.transaction_type.in_(
                [
                    TransactionTypeEnum.WASTEIN.value,
                    TransactionTypeEnum.WASTEOUT.value,
                ]
            ),
        )
    ).scalars().all()

    signed = [ticket for ticket in completed_waste_tickets if ticket.wtn_signature_status == "signed"]
    partial = [ticket for ticket in completed_waste_tickets if ticket.wtn_signature_status == "partial"]
    unsigned = [ticket for ticket in completed_waste_tickets if ticket.wtn_signature_status == "unsigned"]

    assert len(signed) == 2
    assert len(partial) == 3
    assert len(unsigned) == 1

    assert any(
        ticket.has_wtn_receiver_signature
        and not ticket.has_wtn_carrier_signature
        and not ticket.has_wtn_producer_signature
        for ticket in partial
    )
    assert any(
        ticket.has_wtn_receiver_signature
        and ticket.has_wtn_carrier_signature
        and not ticket.has_wtn_producer_signature
        for ticket in partial
    )
    assert any(
        ticket.has_wtn_carrier_signature
        and not ticket.has_wtn_receiver_signature
        and not ticket.has_wtn_producer_signature
        for ticket in partial
    )
    seeded_signature = normalize_png_data_url(DEMO_SIGNATURE_DATA_URI)
    assert seeded_signature is not None
    assert png_has_visible_ink(seeded_signature[1]) is True
    signed_signature_payloads = [
        str(signature_data_uri or "").strip()
        for ticket in completed_waste_tickets
        for signature_data_uri in (
            ticket.wtn_producer_signature_data_uri,
            ticket.wtn_carrier_signature_data_uri,
            ticket.wtn_receiver_signature_data_uri,
        )
        if str(signature_data_uri or "").strip()
    ]
    assert signed_signature_payloads
    assert set(signed_signature_payloads) == {DEMO_SIGNATURE_DATA_URI}

    vehicles_without_default_haulier = db_session.execute(
        select(func.count(Vehicle.id)).where(
            Vehicle.tenant_id == tenant.id,
            Vehicle.default_haulier_id.is_(None),
        )
    ).scalar_one()
    vehicles_with_default_haulier = db_session.execute(
        select(func.count(Vehicle.id)).where(
            Vehicle.tenant_id == tenant.id,
            Vehicle.default_haulier_id.is_not(None),
        )
    ).scalar_one()

    assert int(vehicles_without_default_haulier) == 6
    assert int(vehicles_with_default_haulier) == 10
