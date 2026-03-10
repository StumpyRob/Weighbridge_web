from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Container,
    Customer,
    Destination,
    Driver,
    Haulier,
    Invoice,
    InvoiceLine,
    PaymentMethod,
    Product,
    ProductGroup,
    TaxRate,
    Ticket,
    TicketStatusEnum,
    Unit,
    Vehicle,
    VehicleType,
    WasteProducerSourceEnum,
    Yard,
)
from ..models.base import utcnow
from .wip_snapshots import product_wip_snapshot, ticket_wip_snapshot

MONEY_PLACES = Decimal("0.01")
WEIGHT_PLACES = Decimal("0.001")
WASTE_TYPES = {"WASTEIN", "WASTEOUT"}

CUSTOMER_NAMES = (
    "Beacon Aggregates Ltd", "Greenway Construction Ltd", "Riverside Civils Ltd",
    "Northside Recycling Ltd", "Oakfield Estates", "Premier Groundworks Ltd",
    "Silverline Waste Ltd", "Hilltop Builders Merchants", "Metro Surfacing Ltd",
    "Delta Skip Hire", "Urban Paving Contractors", "Cedar Landscaping Ltd",
    "Bluewater Demolition Ltd", "Westgate Farms", "Stonebrook Developments",
    "Apex Utilities Ltd", "Kingswell Homes", "Redbridge Plant Hire",
    "Falcon Site Services", "Meadow Industrial Park",
)
CUSTOMER_CITIES = ("Leeds", "Wakefield", "Bradford", "York", "Huddersfield", "Doncaster")
HAULIERS = (
    ("Atlas Haulage", "OB1234567"),
    ("Pennine Bulk Logistics", "OB2234567"),
    ("Swift Transport Services", "OB3234567"),
)
DRIVERS = ("Liam Carter", "Jade Foster", "Ryan Patel", "Sophie Briggs")
CONTAINERS = ("Skip 8 Yard - A", "Skip 12 Yard - B", "RORO 20 Yard - C", "RORO 35 Yard - D")
DESTINATIONS = (
    "North Yard Stockpile",
    "Riverside Landfill",
    "Polymer Recovery Plant",
    "Greenfield Batching Plant",
)
PRODUCT_GROUPS = (
    ("AGG", "Aggregate Sales", "Bulk sale materials"),
    ("WST", "Waste Receivals", "Waste intake and disposal"),
    ("HRE", "Hire and Services", "Count-based services"),
    ("REC", "Recovered Outputs", "Recovered site materials"),
)
PRODUCTS = (
    ("AGG20", "Recycled Aggregate 20mm", True, "AGG", "Tonnes", "14.50", "North Yard Stockpile", False),
    ("TOPSOIL", "Screened Topsoil", True, "REC", "Tonnes", "18.00", "Greenfield Batching Plant", False),
    ("BUILDSAND", "Building Sand", True, "AGG", "Tonnes", "12.25", "Greenfield Batching Plant", False),
    ("TYPE1", "MOT Type 1", True, "AGG", "Tonnes", "11.80", "North Yard Stockpile", False),
    ("MIXEDW", "Mixed Builders Waste", False, "WST", "Tonnes", "102.00", "Riverside Landfill", True),
    ("CLAYSOIL", "Clay and Soil Disposal", False, "WST", "Tonnes", "76.00", "Riverside Landfill", True),
    ("WOODW", "Wood Waste Disposal", False, "WST", "Tonnes", "88.00", "Polymer Recovery Plant", False),
    ("BAG50", "50kg Bagged Aggregate", True, "HRE", "Each", "4.75", "North Yard Stockpile", False),
    ("PALLET", "Pallet Collection", True, "HRE", "Each", "22.00", "Greenfield Batching Plant", False),
    ("LOADDEL", "Loose Load Delivery", True, "HRE", "Load", "68.50", "Greenfield Batching Plant", False),
    ("SKIP8", "8 Yard Skip Exchange", False, "HRE", "Each", "265.00", "Riverside Landfill", True),
    ("BALES", "Compacted Bale Removal", False, "WST", "Load", "38.00", "Polymer Recovery Plant", False),
)
TICKETS = (
    ("DMO-00001", "today", "08:05", "OPEN", "OUTWARD", "SALE", "CUST002", "BX24AAB", "AGG20", "Atlas Haulage", "Liam Carter", "North Yard Stockpile", "18120", None, None, None, None),
    ("DMO-00002", "today", "09:20", "OPEN", "OUTWARD", "SALE", "CUST005", "BX24AAC", "BAG50", "Pennine Bulk Logistics", "Jade Foster", "Greenfield Batching Plant", None, None, "12", None, None),
    ("DMO-00003", "yesterday", "15:10", "OPEN", "INWARD", "WASTEIN", "CUST003", "BX24AAD", "MIXEDW", "Swift Transport Services", "Ryan Patel", "Riverside Landfill", "16540", None, None, "RORO 20 Yard - C", ("170904", "17 09 04", "Mixed construction and demolition waste")),
    ("DMO-00004", "earlier", "07:55", "OPEN", "OUTWARD", "WASTEOUT", "CUST004", "BX24AAE", "SKIP8", "Atlas Haulage", "Sophie Briggs", "Riverside Landfill", None, None, "1", "Skip 8 Yard - A", ("150106", "15 01 06", "Mixed packaging")),
    ("DMO-00005", "today", "10:15", "COMPLETE", "OUTWARD", "SALE", "CUST001", "BX24AAF", "AGG20", "Atlas Haulage", "Liam Carter", "North Yard Stockpile", "31120", "13280", None, None, None),
    ("DMO-00006", "today", "11:05", "COMPLETE", "OUTWARD", "SALE", "CUST001", "BX24AAG", "TOPSOIL", "Pennine Bulk Logistics", "Jade Foster", "Greenfield Batching Plant", "28840", "14620", None, None, None),
    ("DMO-00007", "today", "13:20", "COMPLETE", "OUTWARD", "SALE", "CUST006", "BX24AAH", "BAG50", "Swift Transport Services", "Ryan Patel", "North Yard Stockpile", None, None, "24", None, None),
    ("DMO-00008", "yesterday", "08:40", "COMPLETE", "INWARD", "WASTEIN", "CUST007", "BX24AAI", "MIXEDW", "Atlas Haulage", "Sophie Briggs", "Riverside Landfill", "26920", "14260", None, "RORO 35 Yard - D", ("170904", "17 09 04", "Mixed construction and demolition waste")),
    ("DMO-00009", "yesterday", "09:25", "COMPLETE", "OUTWARD", "SALE", "CUST008", "BX24AAJ", "LOADDEL", "Pennine Bulk Logistics", "Liam Carter", "Greenfield Batching Plant", None, None, "1", None, None),
    ("DMO-00010", "yesterday", "14:15", "COMPLETE", "OUTWARD", "SALE", "CUST009", "BX24AAK", "BUILDSAND", "Swift Transport Services", "Jade Foster", "Greenfield Batching Plant", "29480", "13880", None, None, None),
    ("DMO-00011", "earlier", "10:10", "COMPLETE", "OUTWARD", "WASTEOUT", "CUST007", "BX24AAL", "BALES", "Atlas Haulage", "Ryan Patel", "Polymer Recovery Plant", None, None, "6", "Skip 12 Yard - B", ("150106", "15 01 06", "Mixed packaging")),
    ("DMO-00012", "earlier", "11:45", "COMPLETE", "OUTWARD", "SALE", "CUST011", "BX24AAM", "PALLET", "Pennine Bulk Logistics", "Sophie Briggs", "Greenfield Batching Plant", None, None, "8", None, None),
    ("DMO-00013", "earlier", "14:20", "COMPLETE", "OUTWARD", "SALE", "CUST009", "BX24AAN", "TYPE1", "Swift Transport Services", "Liam Carter", "North Yard Stockpile", "34160", "14280", None, None, None),
    ("DMO-00014", "earlier", "16:05", "COMPLETE", "OUTWARD", "SALE", "CUST013", "BX24AAP", "LOADDEL", "Atlas Haulage", "Jade Foster", "Greenfield Batching Plant", None, None, "3", None, None),
)
INVOICES = (
    ("INV-DEMO-001", "CUST001", ("DMO-00005", "DMO-00006"), "today", "OPEN"),
    ("INV-DEMO-002", "CUST007", ("DMO-00008", "DMO-00011"), "yesterday", "OPEN"),
    ("INV-DEMO-003", "CUST009", ("DMO-00010", "DMO-00013"), "earlier", "PAID"),
)
VEHICLE_REGISTRATIONS = tuple(f"BX24AA{suffix}" for suffix in "ABCDEFGHIJKLMNPQ")
VEHICLE_TYPE_CYCLE = ("8 Wheeler", "Artic", "6 Wheeler", "Van", "Tractor & Trailer")


def _money(value: object) -> Decimal:
    return Decimal(str(value)).quantize(MONEY_PLACES, rounding=ROUND_HALF_UP)


def seed_demo_dataset(db: Session, tenant_id: int) -> dict[str, int]:
    tenant_id = int(tenant_id)
    today = utcnow().replace(second=0, microsecond=0).date()
    yesterday = today - timedelta(days=1)
    start_of_week = today - timedelta(days=today.weekday())
    days = {
        "today": today,
        "yesterday": yesterday,
        "earlier": start_of_week if start_of_week < yesterday else today - timedelta(days=3),
    }

    yard = db.execute(select(Yard).where(Yard.tenant_id == tenant_id).order_by(Yard.id.asc()).limit(1)).scalars().first()
    if yard is None:
        raise RuntimeError("Demo dataset seed requires an existing tenant yard.")

    units = {str(unit.name or "").strip(): unit for unit in db.execute(select(Unit).where(Unit.tenant_id == tenant_id)).scalars()}
    missing_units = [name for name in ("Tonnes", "Each", "Load") if name not in units]
    if missing_units:
        raise RuntimeError(f"Demo dataset seed requires units: {', '.join(missing_units)}.")

    vehicle_types = {str(row.code or "").strip(): row for row in db.execute(select(VehicleType)).scalars()}
    missing_vehicle_types = [name for name in VEHICLE_TYPE_CYCLE if name not in vehicle_types]
    if missing_vehicle_types:
        raise RuntimeError(f"Demo dataset seed requires vehicle types: {', '.join(missing_vehicle_types)}.")

    payment_method = db.execute(select(PaymentMethod).where(PaymentMethod.code == "BACS").limit(1)).scalars().first()
    tax_rate = next(
        (row for row in db.execute(select(TaxRate)).scalars() if str(row.code or "").strip().lower().startswith("standard (20%)")),
        None,
    )
    if payment_method is None or tax_rate is None:
        raise RuntimeError("Demo dataset seed requires standard VAT and BACS reference data.")

    hauliers = {name: Haulier(tenant_id=tenant_id, name=name, carrier_licence_number=licence, is_active=True) for name, licence in HAULIERS}
    drivers = {name: Driver(tenant_id=tenant_id, name=name, is_active=True) for name in DRIVERS}
    containers = {name: Container(tenant_id=tenant_id, name=name, is_active=True) for name in CONTAINERS}
    destinations = {name: Destination(tenant_id=tenant_id, name=name, is_active=True) for name in DESTINATIONS}
    product_groups = {
        code: ProductGroup(tenant_id=tenant_id, code=code, name=name, description=description, is_active=True)
        for code, name, description in PRODUCT_GROUPS
    }
    db.add_all([*hauliers.values(), *drivers.values(), *containers.values(), *destinations.values(), *product_groups.values()])

    customers: dict[str, Customer] = {}
    for index, name in enumerate(CUSTOMER_NAMES, start=1):
        credit_limit = _money(Decimal("2500.00") + (Decimal(index) * Decimal("250.00")))
        payment_days = 14 if index % 5 == 0 else 30
        code = f"CUST{index:03d}"
        customers[code] = Customer(
            tenant_id=tenant_id,
            account_code=code,
            name=name,
            invoice_email=f"accounts{index:02d}@demo-weighbridge.test",
            phone=f"0113 555 {1000 + index:04d}",
            address_line1=f"{20 + index} Meridian Way",
            address_line2="Industrial Estate" if index % 3 == 0 else None,
            city=CUSTOMER_CITIES[(index - 1) % len(CUSTOMER_CITIES)],
            postcode=f"LS{(index % 8) + 1} {10 + index}AB",
            country="United Kingdom",
            vat_number=f"GB{900000000 + index}",
            credit_limit=credit_limit,
            credit_limit_pence=int(credit_limit * 100),
            is_cash_account=index in {10, 18},
            cash_account=index in {10, 18},
            payment_terms=f"{payment_days} Days",
            payment_terms_days=payment_days,
            must_have_po=index in {1, 9, 13},
        )
    db.add_all(customers.values())
    db.flush()

    products: dict[str, tuple[Product, Unit]] = {}
    for code, description, sales_only, group_code, unit_name, price, destination_name, final_disposal in PRODUCTS:
        unit = units[unit_name]
        product = Product(
            tenant_id=tenant_id,
            code=code,
            description=description,
            sales_only=bool(sales_only),
            group_id=product_groups[group_code].id,
            unit_id=unit.id,
            tax_rate_id=tax_rate.id,
            unit_price=_money(price),
            account_price=_money(price),
            cash_price=_money(price),
            min_price=_money(price),
            max_price=_money(price),
            final_disposal=bool(final_disposal),
            final_disposal_wip=bool(final_disposal),
            default_destination_id=destinations[destination_name].id,
        )
        products[code] = (product, unit)
    db.add_all(product for product, _ in products.values())
    db.flush()

    vehicles: dict[str, Vehicle] = {}
    for index, registration in enumerate(VEHICLE_REGISTRATIONS, start=1):
        customer = customers[f"CUST{index:03d}"]
        haulier_name = HAULIERS[(index - 1) % len(HAULIERS)][0]
        driver_name = DRIVERS[(index - 1) % len(DRIVERS)]
        vehicle_type = vehicle_types[VEHICLE_TYPE_CYCLE[(index - 1) % len(VEHICLE_TYPE_CYCLE)]]
        vehicles[registration] = Vehicle(
            tenant_id=tenant_id,
            registration=registration,
            owner_customer_id=customer.id,
            default_customer_id=customer.id,
            vehicle_type_id=vehicle_type.id,
            default_tare_kg=Decimal("12000") + Decimal(index * 180),
            overweight_threshold_kg=Decimal("28000"),
            haulier_id=hauliers[haulier_name].id,
            default_haulier_id=hauliers[haulier_name].id,
            driver_id=drivers[driver_name].id,
            default_driver_id=drivers[driver_name].id,
        )
    db.add_all(vehicles.values())
    db.flush()

    ticket_rows: dict[str, tuple[Ticket, Product, Vehicle]] = {}
    for (
        ticket_no,
        day_key,
        time_text,
        status,
        direction,
        transaction_type,
        customer_code,
        registration,
        product_code,
        haulier_name,
        driver_name,
        destination_name,
        gross_text,
        tare_text,
        qty_text,
        container_name,
        ewc_data,
    ) in TICKETS:
        customer = customers[customer_code]
        vehicle = vehicles[registration]
        product, unit = products[product_code]
        gross_kg = Decimal(gross_text) if gross_text else None
        tare_kg = Decimal(tare_text) if tare_text else None
        qty = Decimal(qty_text) if qty_text else None
        net_kg = billable_qty = total = None
        pricing_basis = None
        if status == TicketStatusEnum.COMPLETE.value:
            if unit.unit_type == "WEIGHT":
                net_kg = Decimal(gross_kg or 0) - Decimal(tare_kg or 0)
                billable_qty = (net_kg / Decimal("1000")).quantize(WEIGHT_PLACES, rounding=ROUND_HALF_UP)
                pricing_basis = "WEIGHT"
            else:
                billable_qty = Decimal(qty or 0)
                pricing_basis = "QTY"
            total = _money(billable_qty * Decimal(product.unit_price or 0))
        elif qty is not None:
            total = _money(qty * Decimal(product.unit_price or 0))

        ticket_kwargs: dict[str, object] = {}
        if transaction_type in WASTE_TYPES:
            ticket_kwargs.update(
                {
                    "waste_producer_source": WasteProducerSourceEnum.CUSTOMER.value,
                    "waste_producer_customer_id": customer.id,
                    "waste_producer_name": customer.name,
                    "waste_producer_address": " ".join(part for part in (customer.address_line1, customer.city, customer.postcode) if str(part or "").strip()),
                }
            )
            if ewc_data:
                ticket_kwargs.update(
                    {
                        "ewc_code_6": ewc_data[0],
                        "ewc_code_display": ewc_data[1],
                        "ewc_description": ewc_data[2],
                        "ewc_hazardous": False,
                    }
                )

        hour, minute = [int(part) for part in time_text.split(":", 1)]
        ticket = Ticket(
            tenant_id=tenant_id,
            ticket_no=ticket_no,
            datetime=datetime.combine(days[day_key], time(hour=hour, minute=minute)),
            status=status,
            direction=direction,
            transaction_type=transaction_type,
            customer_id=customer.id,
            vehicle_id=vehicle.id,
            vehicle_reg_text=vehicle.registration,
            product_id=product.id,
            invoice_id=None,
            haulier_id=hauliers[haulier_name].id,
            carrier_licence_number=hauliers[haulier_name].carrier_licence_number,
            driver_id=drivers[driver_name].id,
            container_id=containers[container_name].id if container_name else None,
            destination_id=destinations[destination_name].id,
            yard_id=yard.id,
            gross_kg=gross_kg,
            tare_kg=tare_kg,
            net_kg=net_kg,
            qty=qty,
            unit_id=unit.id,
            unit_price=_money(product.unit_price or 0),
            total=total,
            pricing_basis=pricing_basis,
            pricing_unit_name=unit.name,
            pricing_unit_type=unit.unit_type,
            pricing_unit_price=_money(product.unit_price or 0),
            pricing_qty_snapshot=qty if pricing_basis == "QTY" else None,
            pricing_net_kg_snapshot=net_kg if pricing_basis == "WEIGHT" else None,
            pricing_billable_qty_snapshot=billable_qty,
            dont_invoice=False,
            paid=False,
            wip_snapshot_json=ticket_wip_snapshot(customer=customer, product=product),
            **ticket_kwargs,
        )
        db.add(ticket)
        ticket_rows[ticket_no] = (ticket, product, vehicle)
    db.flush()

    tax_fraction = Decimal(str(tax_rate.rate_percent or 0))
    if tax_fraction > 1:
        tax_fraction /= Decimal("100")

    for invoice_no, customer_code, ticket_numbers, day_key, status in INVOICES:
        customer = customers[customer_code]
        invoice_date = days[day_key]
        invoice = Invoice(
            tenant_id=tenant_id,
            invoice_no=invoice_no,
            customer_id=customer.id,
            invoice_date=invoice_date,
            due_date=invoice_date + timedelta(days=max(int(customer.payment_terms_days or 30), 0)),
            status=status,
            payment_method_id=payment_method.id if status == "PAID" else None,
            paid_at=datetime.combine(invoice_date, time(hour=16, minute=30)) if status == "PAID" else None,
            net_total=Decimal("0.00"),
            vat_total=Decimal("0.00"),
            gross_total=Decimal("0.00"),
            customer_snapshot_json={
                "account_code": customer.account_code,
                "name": customer.name,
                "invoice_email": customer.invoice_email,
                "phone": customer.phone,
                "address_line1": customer.address_line1,
                "address_line2": customer.address_line2,
                "city": customer.city,
                "postcode": customer.postcode,
                "country": customer.country,
                "vat_number": customer.vat_number,
            },
        )
        db.add(invoice)
        db.flush()

        net_total = vat_total = Decimal("0.00")
        for ticket_no in ticket_numbers:
            ticket, product, vehicle = ticket_rows[ticket_no]
            line_net = _money(ticket.total or 0)
            line_vat = _money(line_net * tax_fraction)
            db.add(
                InvoiceLine(
                    tenant_id=tenant_id,
                    invoice_id=invoice.id,
                    ticket_id=ticket.id,
                    description=f"Ticket {ticket.ticket_no} - {ticket.datetime.strftime('%d/%m/%Y')} - {vehicle.registration} - {product.description}",
                    quantity=Decimal(str(ticket.pricing_billable_qty_snapshot or ticket.qty or 0)),
                    unit_price=_money(ticket.unit_price or 0),
                    net=line_net,
                    vat=line_vat,
                    gross=_money(line_net + line_vat),
                    product_snapshot_json=product_wip_snapshot(product),
                )
            )
            ticket.invoice_id = invoice.id
            net_total += line_net
            vat_total += line_vat

        invoice.net_total = _money(net_total)
        invoice.vat_total = _money(vat_total)
        invoice.gross_total = _money(net_total + vat_total)

    return {
        "customers": len(CUSTOMER_NAMES),
        "vehicles": len(VEHICLE_REGISTRATIONS),
        "products": len(PRODUCTS),
        "containers": len(CONTAINERS),
        "drivers": len(DRIVERS),
        "hauliers": len(HAULIERS),
        "destinations": len(DESTINATIONS),
        "tickets": len(TICKETS),
        "tickets_open": sum(1 for row in TICKETS if row[3] == "OPEN"),
        "tickets_complete": sum(1 for row in TICKETS if row[3] == "COMPLETE"),
        "tickets_waste": sum(1 for row in TICKETS if row[5] in WASTE_TYPES),
        "invoices": len(INVOICES),
    }
