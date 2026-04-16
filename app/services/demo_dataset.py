from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Container,
    Customer,
    CustomerAdjustment,
    CustomerProductPrice,
    Destination,
    Driver,
    EwcCode,
    Haulier,
    Invoice,
    InvoiceSequence,
    InvoiceLine,
    PaymentMethod,
    Product,
    ProductGroup,
    TaxRate,
    Ticket,
    TicketSequence,
    TicketStatusEnum,
    Unit,
    Vehicle,
    VehicleType,
    WasteProducerSourceEnum,
    Yard,
)
from ..models.base import utcnow
from ..timezones import uk_date_from_utc
from .wip_snapshots import invoice_product_snapshot, ticket_wip_snapshot

MONEY_PLACES = Decimal("0.01")
WEIGHT_PLACES = Decimal("0.001")
WASTE_TYPES = {"WASTEIN", "WASTEOUT"}
DEMO_EWC_SOURCE_FILE = "demo-reset-seed"

CUSTOMER_NAMES = (
    "Beacon Aggregates Ltd", "Greenway Construction Ltd", "Riverside Civils Ltd",
    "Northside Recycling Ltd", "David Gregson", "Premier Groundworks Ltd",
    "Silverline Waste Ltd", "Hilltop Builders Merchants", "Metro Surfacing Ltd",
    "Delta Skip Hire", "Urban Paving Contractors", "Cedar Landscaping Ltd",
    "Bluewater Demolition Ltd", "Emma Whitaker", "Stonebrook Developments",
    "Simon Fletcher", "Kingswell Homes", "Redbridge Plant Hire",
    "Falcon Site Services", "Meadow Industrial Park", "Claire Bennett",
    "Oliver Mason", "Lucy Pritchard", "James Cartwright", "Sophie Ellwood",
)
DEMO_CUSTOMER_ACCOUNT_CODES = (
    "BCN-AGG",
    "GRN-CIV",
    "RIV-SITE",
    "NS-REC",
    "DGREGSON",
    "PRM-GW",
    "SLV-WASTE",
    "HILLTOP-BM",
    "MTSURF",
    "DSKIP-10",
    "URBAN-PV",
    "CEDAR-LS",
    "BLUE-DMO",
    "EWHIT",
    "STONE-DEV",
    "SFLETCH",
    "KINGS-HM",
    "RED-PLANT",
    "FALCON-SS",
    "MEADOW-IP",
    "CBENNETT",
    "OMASON",
    "LPRITCH",
    "JCARTWR",
    "SELLWOOD",
)
CUSTOMER_CITIES = ("Leeds", "Wakefield", "Bradford", "York", "Huddersfield", "Doncaster")
DEMO_CUSTOMER_INVOICE_FREQUENCIES = (
    "MONTHLY",
    None,
    "WEEKLY",
    "ADHOC",
    "MONTHLY",
    "WEEKLY",
    None,
    "ADHOC",
    "MONTHLY",
    "ADHOC",
    "WEEKLY",
    None,
    "MONTHLY",
    "ADHOC",
    "WEEKLY",
    None,
    "MONTHLY",
    "ADHOC",
    "WEEKLY",
    None,
    "MONTHLY",
    "WEEKLY",
    None,
    "ADHOC",
    "MONTHLY",
)
DEMO_CUSTOMER_PAYMENT_TERMS = (
    ("30 Days", 30),
    (None, None),
    ("7 Days", 7),
    ("Ad Hoc", None),
    (None, None),
    ("14 Days", 14),
    (None, None),
    ("Ad Hoc", None),
    ("30 Days", 30),
    ("Ad Hoc", None),
    ("14 Days", 14),
    (None, None),
    ("30 Days", 30),
    ("Ad Hoc", None),
    ("7 Days", 7),
    (None, None),
    ("30 Days", 30),
    ("Ad Hoc", None),
    ("14 Days", 14),
    (None, None),
    ("Ad Hoc", None),
    ("7 Days", 7),
    (None, None),
    ("Ad Hoc", None),
    ("30 Days", 30),
)
DEMO_CUSTOMER_VAT_NUMBERS = (
    "GB213478965",
    "GB384920157",
    "GB512764893",
    "GB728315409",
    "GB441982673",
    "GB693247185",
    "GB257836914",
    "GB819463752",
    "GB364185927",
    "GB905742618",
    "GB478219536",
    "GB632958471",
    "GB286471359",
    "GB754193826",
    "GB391625784",
    "GB847361295",
    "GB529184367",
    "GB618273954",
    "GB472836159",
    "GB783519642",
    "GB245718936",
    "GB691425378",
    "GB358962741",
    "GB874213569",
    "GB426597183",
)
DEMO_CUSTOMER_EMAILS = (
    "accounts@beaconaggregates.co.uk",
    "finance@greenwayconstruction.co.uk",
    "invoices@riversidecivils.co.uk",
    "accounts@northsiderecycling.co.uk",
    "david.gregson@customer-mail.test",
    "accounts@premiergroundworks.co.uk",
    "billing@silverlinewaste.co.uk",
    "accounts@hilltopbuilders.co.uk",
    "invoices@metrosurfacing.co.uk",
    "accounts@deltaskiphire.co.uk",
    "finance@urbanpaving.co.uk",
    "accounts@cedarlandscaping.co.uk",
    "billing@bluewaterdemo.co.uk",
    "emma.whitaker@customer-mail.test",
    "accounts@stonebrookdev.co.uk",
    "simon.fletcher@customer-mail.test",
    "accounts@kingswellhomes.co.uk",
    "billing@redbridgeplant.co.uk",
    "accounts@falconsiteservices.co.uk",
    "finance@meadowindustrial.co.uk",
    "claire.bennett@customer-mail.test",
    "oliver.mason@customer-mail.test",
    "lucy.pritchard@customer-mail.test",
    "james.cartwright@customer-mail.test",
    "sophie.ellwood@customer-mail.test",
)
DEMO_CUSTOMER_ON_STOP_INDEXES = frozenset({4, 17})
DEMO_CUSTOMER_DO_NOT_INVOICE_INDEXES = frozenset({3, 8, 15, 20, 21, 22})
DEMO_CUSTOMER_MUST_HAVE_PO_INDEXES = frozenset({1, 6, 9, 13, 19, 21, 22})
DEMO_CUSTOMER_CASH_ACCOUNT_INDEXES = frozenset({10, 14, 18})
DEMO_CUSTOMER_OWED_ADJUSTMENTS = (
    ("CUST005", "486.20", "Opening balance carried forward from prior month."),
    ("CUST017", "8425.00", "Account on hold while overdue balance is cleared."),
    ("CUST022", "1295.80", "Retained balance awaiting remittance advice."),
)
DEMO_CUSTOMER_PRICE_OVERRIDES = (
    ("CUST001", "AGG20", "13.75"),
    ("CUST006", "TOPSOIL", "16.80"),
    ("CUST011", "PALLET", "19.50"),
    ("CUST021", "EARDEF", "7.95"),
    ("CUST024", "HIVIS", "4.60"),
)
DEMO_ZERO_RATED_PRODUCT_CODES = frozenset({"EARDEF", "HIVIS"})
DEMO_USED_ON_SITE_PRODUCT_CODES = frozenset({"PALLET", "EARDEF"})
HAULIERS = (
    ("Atlas Haulage", "CBDU482761"),
    ("Pennine Bulk Logistics", "CBDU173954"),
    ("Swift Transport Services", "CBDU628315"),
    ("Northgate Bulk Carriers", "CBDU751842"),
    ("West Riding Logistics", "CBDU294637"),
    ("Mason Freight Services", "CBDU836429"),
)
DRIVERS = (
    "Liam Carter",
    "Jade Foster",
    "Ben Thornton",
    "Sophie Briggs",
    "Daniel Mercer",
    "Connor Willis",
    "Oliver Hayes",
    "Nathan Cooper",
    "Thomas Kirby",
)
CONTAINERS = ("Skip 8 Yard - A", "Skip 12 Yard - B", "RORO 20 Yard - C", "RORO 35 Yard - D")
DESTINATIONS = (
    "Inert Waste Bay 1",
    "Inert Waste Bay 2",
    "Riverside Landfill",
    "Hazardous Bay 1",
    "Hazardous Bay 2",
    "Wood/Timber Bay 1",
)
PRODUCT_GROUPS = (
    ("AGG", "Aggregate Sales", "Bulk sale materials", "4000"),
    ("WST", "Waste Receivals", "Waste intake and disposal", "4100"),
    ("HRE", "Hire and Services", "Count-based services", "4200"),
    ("REC", "Recovered Outputs", "Recovered site materials", "4300"),
    ("GNS", "General Sales", "Retail counter and sundry sales", "4400"),
)
PRODUCTS = (
    ("AGG20", "Recycled Aggregate 20mm", "sale", True, "AGG", "Tonnes", "14.50", "Inert Waste Bay 1", False),
    ("TOPSOIL", "Screened Topsoil", "sale", True, "REC", "Tonnes", "18.00", "Inert Waste Bay 2", False),
    ("TOPM3", "Screened Topsoil (m3)", "sale", True, "REC", "m3", "32.00", "Inert Waste Bay 2", False),
    ("BUILDSAND", "Building Sand", "sale", True, "AGG", "Tonnes", "12.25", "Inert Waste Bay 2", False),
    ("TYPE1", "MOT Type 1", "sale", True, "AGG", "Tonnes", "11.80", "Inert Waste Bay 1", False),
    ("MIXEDW", "Mixed Builders Waste", "waste", False, "WST", "Tonnes", "102.00", "Riverside Landfill", True),
    ("CLAYSOIL", "Clay and Soil Disposal", "waste", False, "WST", "Tonnes", "76.00", "Hazardous Bay 1", True),
    ("WOODW", "Wood Waste Disposal", "waste", False, "WST", "Tonnes", "88.00", "Wood/Timber Bay 1", False),
    ("BAG50", "50kg Bagged Aggregate", "sale", True, "HRE", "Each", "4.75", "Inert Waste Bay 1", False),
    ("PALLET", "Pallet Collection", "sale", True, "HRE", "Each", "22.00", "Inert Waste Bay 2", False),
    ("EARDEF", "Ear Defenders", "sale", True, "GNS", "Each", "8.95", "Inert Waste Bay 1", False),
    ("HIVIS", "High Vis Vest", "sale", True, "GNS", "Each", "5.50", "Inert Waste Bay 1", False),
    ("LOADDEL", "Loose Load Delivery", "sale", True, "HRE", "Load", "68.50", "Inert Waste Bay 2", False),
    ("SKIP8", "8 Yard Skip Exchange", "waste", False, "HRE", "Each", "265.00", "Riverside Landfill", True),
    ("BALES", "Compacted Bale Removal", "waste", False, "WST", "Load", "38.00", "Wood/Timber Bay 1", False),
)
DEMO_WASTE_PRODUCT_EWC = {
    "MIXEDW": ("170904", "Mixed construction and demolition waste", False),
    "CLAYSOIL": ("170503", "Soil and stones containing hazardous substances", True),
    "WOODW": ("170201", "Wood", False),
    "SKIP8": ("150106", "Mixed packaging", False),
    "BALES": ("150106", "Mixed packaging", False),
}
TICKETS = (
    (1, 5, "07:35", "COMPLETE", "OUTWARD", "SALE", "CUST001", "YP24KDM", "AGG20", "Atlas Haulage", "Liam Carter", "Inert Waste Bay 1", "30800", "13420", None, None, None),
    (2, 5, "08:20", "COMPLETE", "OUTWARD", "SALE", "CUST006", "YN21KPX", "TYPE1", "Pennine Bulk Logistics", "Jade Foster", "Inert Waste Bay 1", "32240", "13860", None, None, None),
    (3, 5, "10:05", "COMPLETE", "OUTWARD", "SALE", "CUST002", "YX24LNF", "TOPSOIL", "Atlas Haulage", "Liam Carter", "Inert Waste Bay 2", "29620", "13620", None, None, None),
    (4, 5, "14:10", "COMPLETE", "INWARD", "WASTEIN", "CUST013", "YO24TFS", "MIXEDW", "Swift Transport Services", "Ben Thornton", "Riverside Landfill", "27140", "14160", None, "RORO 20 Yard - C", None),
    (5, 4, "07:50", "COMPLETE", "OUTWARD", "SALE", "CUST001", "YP24KDM", "AGG20", "Atlas Haulage", "Liam Carter", "Inert Waste Bay 1", "31540", "13380", None, None, None),
    (6, 4, "08:45", "COMPLETE", "OUTWARD", "SALE", "CUST009", "YS22FLD", "TYPE1", "Pennine Bulk Logistics", "Connor Willis", "Inert Waste Bay 1", "33420", "13980", None, None, None),
    (7, 4, "10:30", "COMPLETE", "OUTWARD", "SALE", "CUST011", "YY24NKO", "PALLET", "West Riding Logistics", "Sophie Briggs", "Inert Waste Bay 2", None, None, "6", None, None),
    (8, 4, "13:15", "COMPLETE", "OUTWARD", "SALE", "CUST016", "NX72KLU", "LOADDEL", "Mason Freight Services", "Thomas Kirby", "Inert Waste Bay 2", None, None, "1", None, None),
    (9, 4, "15:05", "COMPLETE", "OUTWARD", "WASTEOUT", "CUST010", "YH70CGE", "SKIP8", "Atlas Haulage", "Daniel Mercer", "Riverside Landfill", None, None, "1", "Skip 8 Yard - A", None),
    (10, 3, "07:40", "COMPLETE", "OUTWARD", "SALE", "CUST002", "YX24LNF", "BUILDSAND", "Atlas Haulage", "Liam Carter", "Inert Waste Bay 2", "28440", "13620", None, None, None),
    (11, 3, "08:55", "COMPLETE", "OUTWARD", "SALE", "CUST006", "YN21KPX", "AGG20", "Pennine Bulk Logistics", "Jade Foster", "Inert Waste Bay 1", "30120", "13280", None, None, None),
    (12, 3, "10:20", "COMPLETE", "OUTWARD", "SALE", "CUST009", "YS22FLD", "TOPM3", "Pennine Bulk Logistics", "Connor Willis", "Inert Waste Bay 2", None, None, "9.5", None, None),
    (13, 3, "12:40", "COMPLETE", "INWARD", "WASTEIN", "CUST013", "YO24TFS", "WOODW", "Swift Transport Services", "Ben Thornton", "Wood/Timber Bay 1", "22480", "14220", None, "RORO 35 Yard - D", None),
    (14, 3, "15:25", "COMPLETE", "OUTWARD", "SALE", "CUST001", "YP24KDM", "BAG50", "Atlas Haulage", "Liam Carter", "Inert Waste Bay 1", None, None, "18", None, None),
    (15, 2, "07:30", "COMPLETE", "OUTWARD", "SALE", "CUST011", "YY24NKO", "PALLET", "West Riding Logistics", "Sophie Briggs", "Inert Waste Bay 2", None, None, "4", None, None),
    (16, 2, "08:35", "COMPLETE", "OUTWARD", "SALE", "CUST016", "NX72KLU", "TOPSOIL", "Mason Freight Services", "Thomas Kirby", "Inert Waste Bay 2", "28680", "13640", None, None, None),
    (17, 2, "10:10", "COMPLETE", "INWARD", "WASTEIN", "CUST004", "YC24HSL", "CLAYSOIL", "Northgate Bulk Carriers", "Nathan Cooper", "Hazardous Bay 1", "30220", "14120", None, "RORO 20 Yard - C", None),
    (18, 2, "11:20", "COMPLETE", "OUTWARD", "SALE", "CUST009", "YS22FLD", "TYPE1", "Pennine Bulk Logistics", "Connor Willis", "Inert Waste Bay 1", "32760", "14020", None, None, None),
    (19, 2, "14:05", "COMPLETE", "OUTWARD", "SALE", "CUST006", "YN21KPX", "LOADDEL", "Pennine Bulk Logistics", "Jade Foster", "Inert Waste Bay 2", None, None, "2", None, None),
    (20, 2, "15:30", "COMPLETE", "OUTWARD", "WASTEOUT", "CUST010", "YH70CGE", "BALES", "Atlas Haulage", "Daniel Mercer", "Wood/Timber Bay 1", None, None, "6", "Skip 12 Yard - B", None),
    (21, 1, "07:45", "COMPLETE", "OUTWARD", "SALE", "CUST002", "YX24LNF", "AGG20", "Atlas Haulage", "Liam Carter", "Inert Waste Bay 1", "30980", "13360", None, None, None),
    (22, 1, "09:15", "COMPLETE", "OUTWARD", "SALE", "CUST001", "YP24KDM", "TOPSOIL", "Atlas Haulage", "Liam Carter", "Inert Waste Bay 2", "28940", "13740", None, None, None),
    (23, 1, "11:10", "COMPLETE", "OUTWARD", "SALE", "CUST011", "YY24NKO", "EARDEF", "West Riding Logistics", "Sophie Briggs", "Inert Waste Bay 1", None, None, "4", None, None),
    (24, 1, "14:20", "COMPLETE", "INWARD", "WASTEIN", "CUST007", "YJ68MVT", "MIXEDW", "Swift Transport Services", "Ben Thornton", "Riverside Landfill", "26420", "14320", None, "RORO 35 Yard - D", None),
    (25, 1, "16:00", "OPEN", "OUTWARD", "SALE", "CUST017", "NX72KLU", "TYPE1", "Mason Freight Services", "Thomas Kirby", "Inert Waste Bay 1", "30920", None, None, None, None),
    (26, 0, "07:25", "OPEN", "OUTWARD", "SALE", "CUST006", "YN21KPX", "AGG20", "Pennine Bulk Logistics", "Jade Foster", "Inert Waste Bay 1", "31240", None, None, None, None),
    (27, 0, "08:50", "OPEN", "OUTWARD", "SALE", "CUST009", "YS22FLD", "TOPM3", "Pennine Bulk Logistics", "Connor Willis", "Inert Waste Bay 2", None, None, "7.5", None, None),
    (28, 0, "09:40", "OPEN", "INWARD", "WASTEIN", "CUST003", "YA24RVO", "MIXEDW", "Swift Transport Services", "Ben Thornton", "Riverside Landfill", "25840", None, None, "RORO 20 Yard - C", None),
    (29, 0, "11:05", "OPEN", "OUTWARD", "SALE", "CUST021", "YP24KDM", "HIVIS", "Atlas Haulage", "Liam Carter", "Inert Waste Bay 1", None, None, "5", None, None),
    (30, 0, "13:10", "OPEN", "OUTWARD", "SALE", "CUST001", "YP24KDM", "BAG50", "Atlas Haulage", "Liam Carter", "Inert Waste Bay 1", None, None, "20", None, None),
    (31, 0, "14:35", "OPEN", "OUTWARD", "WASTEOUT", "CUST010", "YH70CGE", "SKIP8", "Atlas Haulage", "Daniel Mercer", "Riverside Landfill", None, None, "1", "Skip 8 Yard - A", None),
    (32, 0, "15:50", "COMPLETE", "OUTWARD", "SALE", "CUST009", "YS22FLD", "AGG20", "Pennine Bulk Logistics", "Connor Willis", "Inert Waste Bay 1", "31860", "13440", None, None, None),
)
INVOICES = (
    (1, "CUST001", (1, 5, 14), 2, "PAID"),
    (2, "CUST006", (2, 11, 19), 1, "PAID"),
    (3, "CUST009", (6, 12, 18), 1, "PAID"),
    (4, "CUST013", (4, 13), 2, "PAID"),
    (5, "CUST016", (8, 16), 1, "PAID"),
    (6, "CUST002", (3, 10, 21), 0, "OPEN"),
    (7, "CUST011", (7, 15, 23), 1, "OPEN"),
)
VEHICLE_REGISTRATIONS = (
    "YP24KDM",
    "YX24LNF",
    "YA24RVO",
    "YC24HSL",
    "YG73TWN",
    "YN21KPX",
    "YJ68MVT",
    "YD24BXR",
    "YS22FLD",
    "YH70CGE",
    "YY24NKO",
    "YK19VLP",
    "YO24TFS",
    "YE23MJN",
    "YR24WDX",
    "NX72KLU",
)
VEHICLE_TYPE_CYCLE = ("8 Wheeler", "Artic", "6 Wheeler", "Van", "Tractor & Trailer")
DEMO_VEHICLE_TYPE_MASS_PROFILES = {
    "8 Wheeler": (
        ("14980", "32000"),
        ("15240", "32000"),
        ("15560", "32000"),
        ("15120", "32000"),
    ),
    "Artic": (
        ("14620", "44000"),
        ("14940", "44000"),
        ("15180", "40000"),
    ),
    "6 Wheeler": (
        ("11840", "26000"),
        ("12160", "26000"),
        ("11680", "24000"),
    ),
    "Van": (
        ("1980", "3000"),
        ("2240", "3500"),
        ("2160", "3500"),
    ),
    "Tractor & Trailer": (
        ("15980", "38000"),
        ("16340", "38000"),
        ("16820", "40000"),
    ),
}
DEMO_VEHICLES_WITHOUT_DEFAULT_HAULIER = frozenset(
    {
        "YA24RVO",
        "YG73TWN",
        "YJ68MVT",
        "YD24BXR",
        "YK19VLP",
        "NX72KLU",
    }
)
DEMO_SIGNATURE_DATA_URI = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAUAAAABQCAIAAADnUzvSAAACZElEQVR42u3cS1IDIRRAUfbi3MW4/73oyCqHpgPvxznl2ASai3QbXd9AW8sUgIABAQMCBgEDAgYEDAgYBAwIGBAwCBgQMCBgQMAgYEDAgIABAYOAAQEDAn7s4/Pr75cJQcAt0xUwAm6croARcON0BYyAG6cr4JhpNycCPpKuteWMI2DpCthUC1i6c6fdLAlYuqadcQFbQ6Y9cbACnrmGBi/r/0/7mIYFfN32P3JNP0h3/IYl4Jknt2EL+p10bxi1gC9Kt9dSfjDtAwZefHtaI6evS7pd1vE7Y+k79hanjCVd6Z4+7LSbhF23CQEDXNK95KyVe5/SZRJeHXX6VV7Szd2tW++eG79561EnXusl3bCrXrzhmGmvdik3jjplxS7pRl519+11Gj406uDVu6R77g2/9E2ueuSWe3EDRh02pUu6YemWCjh92iNfLuVhcsz0LunGL/TcMdaZ87CX3vtroUNfpQPu+Jw2ZXu+JN3IhlvU+3jIS7rnTst1Gm7611pFRn1pwLPTPfdyfU+Ph96kDSs6YOlWaLjLXdzehlt/bi8/4F4fQqqwucTHUDPgXQ33+vRboYB7fXiw1LngkqNjTMO3/SulFZxu+tG64Blhy6uP+ROoN9/8bf8FbYWlm3K663K8P/czp+ki3vjE+Hu0FZZuYsCtf+087K4v62mtgNP2+5iAKy/Za+/6BJwT8LxHIx0DnrFABRwasI08/q2OX98CjgjYCvAzJ2xOdLszYJPoZw5tAkbACBiHRgQMAgYEDAgYEDAIGBAwIGBAwCBgQMCAgEHAgIABAQMCBgEDAgYEDPz6Ab4kvDWwHa2oAAAAAElFTkSuQmCC"
)
DEMO_WASTE_TICKET_SIGNATURES: dict[int, dict[str, tuple[str, int]]] = {
    4: {
        "producer": ("Martin Shaw", -28),
        "carrier": ("Ben Thornton", -9),
        "receiver": ("Rachel Moore", 6),
    },
    9: {
        "receiver": ("Alicia Ford", 8),
    },
    13: {
        "carrier": ("Ben Thornton", -11),
        "receiver": ("Megan Frost", 7),
    },
    20: {
        "carrier": ("Daniel Mercer", -10),
    },
    24: {
        "producer": ("Leanne Whitfield", -31),
        "carrier": ("Ben Thornton", -12),
        "receiver": ("Ryan Porter", 5),
    },
}


def _money(value: object) -> Decimal:
    return Decimal(str(value)).quantize(MONEY_PLACES, rounding=ROUND_HALF_UP)


def _format_ewc_code_display(code_6: str) -> str:
    return f"{code_6[0:2]} {code_6[2:4]} {code_6[4:6]}"


def _ensure_demo_ewc_subset(db: Session) -> dict[str, EwcCode]:
    code_values = sorted({code_6 for code_6, _, _ in DEMO_WASTE_PRODUCT_EWC.values()})
    existing = {
        row.code_6: row
        for row in db.execute(select(EwcCode).where(EwcCode.code_6.in_(code_values))).scalars()
    }
    seeded_rows = {
        code_6: (description, hazardous)
        for code_6, description, hazardous in DEMO_WASTE_PRODUCT_EWC.values()
    }
    now = utcnow()
    ensured: dict[str, EwcCode] = {}

    for code_6 in code_values:
        description, hazardous = seeded_rows[code_6]
        code_display = _format_ewc_code_display(code_6)
        row = existing.get(code_6)
        if row is None:
            row = EwcCode(
                code_6=code_6,
                code_display=code_display,
                description=description,
                hazardous=hazardous,
                active=True,
                source_file=DEMO_EWC_SOURCE_FILE,
                imported_at=now,
            )
            db.add(row)
        else:
            changed = False
            if str(row.code_display or "") != code_display:
                row.code_display = code_display
                changed = True
            if str(row.description or "") != description:
                row.description = description
                changed = True
            if bool(row.hazardous) != bool(hazardous):
                row.hazardous = bool(hazardous)
                changed = True
            if not bool(row.active):
                row.active = True
                changed = True
            if str(row.source_file or "") != DEMO_EWC_SOURCE_FILE:
                row.source_file = DEMO_EWC_SOURCE_FILE
                changed = True
            if changed:
                row.imported_at = now
        ensured[code_6] = row

    db.flush()
    return ensured


def _demo_invoice_no(number: int, *, year: int) -> str:
    return f"INV-{str(year)[2:]}-{int(number):05d}"


def _resolve_demo_seed_date(day_spec: object, *, today: date, days: dict[str, date]) -> date:
    if isinstance(day_spec, date):
        return day_spec
    if isinstance(day_spec, int):
        return today - timedelta(days=max(int(day_spec), 0))
    if isinstance(day_spec, str) and day_spec in days:
        return days[day_spec]
    raise RuntimeError(f"Unsupported demo seed date: {day_spec!r}")


def _demo_vehicle_mass_profile(vehicle_type_code: str, occurrence: int) -> tuple[Decimal, Decimal]:
    profiles = DEMO_VEHICLE_TYPE_MASS_PROFILES[vehicle_type_code]
    tare_text, threshold_text = profiles[occurrence % len(profiles)]
    return Decimal(tare_text), Decimal(threshold_text)


def _apply_demo_wtn_signatures(
    ticket: Ticket,
    *,
    signature_plan: dict[str, tuple[str, int]] | None,
) -> None:
    if not signature_plan:
        return
    signed_base = ticket.datetime
    for role, (signer_name, minutes_offset) in signature_plan.items():
        setattr(ticket, f"wtn_{role}_signature_data_uri", DEMO_SIGNATURE_DATA_URI)
        setattr(ticket, f"wtn_{role}_signature_signer_name", signer_name)
        setattr(
            ticket,
            f"wtn_{role}_signature_signed_at",
            signed_base + timedelta(minutes=int(minutes_offset)),
        )


def _sync_demo_sequences(
    db: Session,
    *,
    tenant_id: int,
    ticket_numbers_by_year: dict[int, int],
    invoice_numbers_by_year: dict[int, int],
) -> None:
    now = utcnow()
    for year, last_number in ticket_numbers_by_year.items():
        sequence = db.execute(
            select(TicketSequence).where(
                TicketSequence.tenant_id == tenant_id,
                TicketSequence.year == int(year),
            )
        ).scalars().first()
        if sequence is None:
            sequence = TicketSequence(
                tenant_id=tenant_id,
                year=int(year),
                last_number=int(last_number),
                updated_at=now,
            )
            db.add(sequence)
            continue
        if int(sequence.last_number or 0) < int(last_number):
            sequence.last_number = int(last_number)
        sequence.updated_at = now

    for year, last_number in invoice_numbers_by_year.items():
        sequence = db.execute(
            select(InvoiceSequence).where(InvoiceSequence.year == int(year))
        ).scalars().first()
        if sequence is None:
            sequence = InvoiceSequence(
                year=int(year),
                last_number=int(last_number),
                updated_at=now,
            )
            db.add(sequence)
            continue
        if int(sequence.last_number or 0) < int(last_number):
            sequence.last_number = int(last_number)
        sequence.updated_at = now


def _demo_ticket_no(number: int, *, year: int) -> str:
    return f"{str(year)[2:]}-{int(number):05d}"


def seed_demo_dataset(db: Session, tenant_id: int) -> dict[str, int]:
    tenant_id = int(tenant_id)
    today = uk_date_from_utc(utcnow())
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
    missing_units = [name for name in ("Tonnes", "m3", "Each", "Load") if name not in units]
    if missing_units:
        raise RuntimeError(f"Demo dataset seed requires units: {', '.join(missing_units)}.")

    vehicle_types = {str(row.code or "").strip(): row for row in db.execute(select(VehicleType)).scalars()}
    missing_vehicle_types = [name for name in VEHICLE_TYPE_CYCLE if name not in vehicle_types]
    if missing_vehicle_types:
        raise RuntimeError(f"Demo dataset seed requires vehicle types: {', '.join(missing_vehicle_types)}.")

    payment_method = db.execute(select(PaymentMethod).where(PaymentMethod.code == "BACS").limit(1)).scalars().first()
    standard_tax_rate = next(
        (row for row in db.execute(select(TaxRate)).scalars() if str(row.code or "").strip().lower().startswith("standard (20%)")),
        None,
    )
    zero_tax_rate = next(
        (row for row in db.execute(select(TaxRate)).scalars() if str(row.code or "").strip().lower().startswith("zero (0%)")),
        None,
    )
    if payment_method is None or standard_tax_rate is None or zero_tax_rate is None:
        raise RuntimeError("Demo dataset seed requires standard VAT and BACS reference data.")

    demo_ewc_codes = _ensure_demo_ewc_subset(db)
    hauliers = {name: Haulier(tenant_id=tenant_id, name=name, carrier_licence_number=licence, is_active=True) for name, licence in HAULIERS}
    drivers = {name: Driver(tenant_id=tenant_id, name=name, is_active=True) for name in DRIVERS}
    containers = {name: Container(tenant_id=tenant_id, name=name, is_active=True) for name in CONTAINERS}
    destinations = {name: Destination(tenant_id=tenant_id, name=name, is_active=True) for name in DESTINATIONS}
    product_groups = {
        code: ProductGroup(
            tenant_id=tenant_id,
            code=code,
            name=name,
            description=description,
            nominal_code_default=nominal_code_default,
            is_active=True,
        )
        for code, name, description, nominal_code_default in PRODUCT_GROUPS
    }
    db.add_all([*hauliers.values(), *drivers.values(), *containers.values(), *destinations.values(), *product_groups.values()])

    customers: dict[str, Customer] = {}
    for index, name in enumerate(CUSTOMER_NAMES, start=1):
        customer_key = f"CUST{index:03d}"
        account_code = DEMO_CUSTOMER_ACCOUNT_CODES[index - 1]
        credit_limit = _money(Decimal("2500.00") + (Decimal(index) * Decimal("250.00")))
        invoice_frequency = DEMO_CUSTOMER_INVOICE_FREQUENCIES[index - 1]
        payment_terms, payment_days = DEMO_CUSTOMER_PAYMENT_TERMS[index - 1]
        vat_number = DEMO_CUSTOMER_VAT_NUMBERS[index - 1]
        invoice_email = DEMO_CUSTOMER_EMAILS[index - 1]
        on_stop = index in DEMO_CUSTOMER_ON_STOP_INDEXES
        do_not_invoice = index in DEMO_CUSTOMER_DO_NOT_INVOICE_INDEXES
        cash_account = index in DEMO_CUSTOMER_CASH_ACCOUNT_INDEXES
        must_have_po = index in DEMO_CUSTOMER_MUST_HAVE_PO_INDEXES
        customers[customer_key] = Customer(
            tenant_id=tenant_id,
            account_code=account_code,
            name=name,
            invoice_email=invoice_email,
            phone=f"0113 555 {1000 + index:04d}",
            address_line1=f"{20 + index} Meridian Way",
            address_line2="Industrial Estate" if index % 3 == 0 else None,
            city=CUSTOMER_CITIES[(index - 1) % len(CUSTOMER_CITIES)],
            postcode=f"LS{(index % 8) + 1} {10 + index}AB",
            country="United Kingdom",
            vat_number=vat_number,
            credit_limit=credit_limit,
            credit_limit_pence=int(credit_limit * 100),
            on_stop=on_stop,
            do_not_invoice=do_not_invoice,
            is_cash_account=cash_account,
            cash_account=cash_account,
            invoice_frequency=invoice_frequency,
            payment_terms=payment_terms,
            payment_terms_days=payment_days,
            must_have_po=must_have_po,
        )
    db.add_all(customers.values())
    db.flush()
    customer_price_overrides = {
        (customer_code, product_code): _money(unit_price)
        for customer_code, product_code, unit_price in DEMO_CUSTOMER_PRICE_OVERRIDES
    }

    products: dict[str, tuple[Product, Unit]] = {}
    for code, description, product_type, sales_only, group_code, unit_name, price, destination_name, final_disposal in PRODUCTS:
        unit = units[unit_name]
        product_ewc = None
        product_tax_rate = zero_tax_rate if code in DEMO_ZERO_RATED_PRODUCT_CODES else standard_tax_rate
        used_on_site = code in DEMO_USED_ON_SITE_PRODUCT_CODES
        if code in DEMO_WASTE_PRODUCT_EWC:
            product_ewc = demo_ewc_codes[DEMO_WASTE_PRODUCT_EWC[code][0]]
        product = Product(
            tenant_id=tenant_id,
            code=code,
            description=description,
            product_type=product_type,
            sales_only=bool(sales_only),
            group_id=product_groups[group_code].id,
            unit_id=unit.id,
            tax_rate_id=product_tax_rate.id,
            unit_price=_money(price),
            account_price=_money(price),
            cash_price=_money(price),
            min_price=_money(price),
            max_price=_money(price),
            is_hazardous=bool(product_ewc.hazardous) if product_ewc else False,
            final_disposal=bool(final_disposal),
            final_disposal_wip=bool(final_disposal),
            used_on_site=bool(used_on_site),
            used_on_site_wip=bool(used_on_site),
            ewc_code=product_ewc,
            default_destination_id=destinations[destination_name].id,
        )
        products[code] = (product, unit)
    db.add_all(product for product, _ in products.values())
    db.flush()

    db.add_all(
        CustomerProductPrice(
            tenant_id=tenant_id,
            customer_id=customers[customer_code].id,
            product_id=products[product_code][0].id,
            unit_price=_money(unit_price),
            is_active=True,
        )
        for customer_code, product_code, unit_price in DEMO_CUSTOMER_PRICE_OVERRIDES
    )
    db.flush()

    vehicles: dict[str, Vehicle] = {}
    vehicle_type_occurrences: dict[str, int] = {}
    for index, registration in enumerate(VEHICLE_REGISTRATIONS, start=1):
        customer = customers[f"CUST{index:03d}"]
        haulier_name = HAULIERS[(index - 1) % len(HAULIERS)][0]
        driver_name = DRIVERS[(index - 1) % len(DRIVERS)]
        vehicle_type_code = VEHICLE_TYPE_CYCLE[(index - 1) % len(VEHICLE_TYPE_CYCLE)]
        vehicle_type = vehicle_types[vehicle_type_code]
        occurrence = vehicle_type_occurrences.get(vehicle_type_code, 0)
        vehicle_type_occurrences[vehicle_type_code] = occurrence + 1
        default_tare_kg, overweight_threshold_kg = _demo_vehicle_mass_profile(vehicle_type_code, occurrence)
        vehicles[registration] = Vehicle(
            tenant_id=tenant_id,
            registration=registration,
            owner_customer_id=customer.id,
            default_customer_id=customer.id,
            vehicle_type_id=vehicle_type.id,
            default_tare_kg=default_tare_kg,
            overweight_threshold_kg=overweight_threshold_kg,
            haulier_id=hauliers[haulier_name].id,
            default_haulier_id=(
                None
                if registration in DEMO_VEHICLES_WITHOUT_DEFAULT_HAULIER
                else hauliers[haulier_name].id
            ),
            driver_id=drivers[driver_name].id,
            default_driver_id=drivers[driver_name].id,
        )
    db.add_all(vehicles.values())
    db.flush()

    all_ticket_specs = list(TICKETS)
    ticket_rows: dict[int, tuple[Ticket, Product, Vehicle]] = {}
    ticket_numbers_by_year: dict[int, int] = {}
    for (
        ticket_number,
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
    ) in all_ticket_specs:
        customer = customers[customer_code]
        vehicle = vehicles[registration]
        product, unit = products[product_code]
        product_final_disposal = bool(product.final_disposal) or bool(
            product.final_disposal_wip
        )
        product_used_on_site = bool(product.used_on_site) or bool(
            product.used_on_site_wip
        )
        if product_used_on_site:
            destination_id = None
        else:
            if destination_name is None:
                raise RuntimeError(
                    f"Demo ticket {ticket_number} requires destination_name for product {product_code}."
                )
            destination_id = destinations[destination_name].id
        if product_final_disposal and destination_id is None:
            raise RuntimeError(
                f"Demo ticket {ticket_number} requires destination for final disposal product {product_code}."
            )
        ticket_date = _resolve_demo_seed_date(day_key, today=today, days=days)
        ticket_no = _demo_ticket_no(ticket_number, year=ticket_date.year)
        ticket_numbers_by_year[ticket_date.year] = max(
            ticket_numbers_by_year.get(ticket_date.year, 0),
            int(ticket_no.split("-", 1)[1]),
        )
        gross_kg = Decimal(str(gross_text)) if gross_text is not None else None
        tare_kg = Decimal(str(tare_text)) if tare_text is not None else None
        qty = Decimal(str(qty_text)) if qty_text is not None else None
        net_kg = billable_qty = total = None
        pricing_basis = None
        ticket_unit_price = customer_price_overrides.get(
            (customer_code, product_code),
            _money(product.unit_price or 0),
        )
        if status == TicketStatusEnum.COMPLETE.value:
            if unit.unit_type == "WEIGHT":
                net_kg = Decimal(gross_kg or 0) - Decimal(tare_kg or 0)
                billable_qty = (net_kg / Decimal("1000")).quantize(WEIGHT_PLACES, rounding=ROUND_HALF_UP)
                pricing_basis = "WEIGHT"
            else:
                billable_qty = Decimal(qty or 0)
                pricing_basis = "QTY"
            total = _money(billable_qty * ticket_unit_price)
        elif qty is not None:
            total = _money(qty * ticket_unit_price)

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
            ticket_ewc = ewc_data
            if ticket_ewc is None and product.ewc_code is not None:
                ticket_ewc = (
                    product.ewc_code.code_6,
                    product.ewc_code.code_display,
                    product.ewc_code.description,
                    bool(product.ewc_code.hazardous),
                )
            if ticket_ewc:
                ticket_kwargs.update(
                    {
                        "ewc_code_6": ticket_ewc[0],
                        "ewc_code_display": ticket_ewc[1],
                        "ewc_description": ticket_ewc[2],
                        "ewc_hazardous": bool(ticket_ewc[3]) if len(ticket_ewc) > 3 else False,
                    }
                )

        hour, minute = [int(part) for part in time_text.split(":", 1)]
        ticket = Ticket(
            tenant_id=tenant_id,
            ticket_no=ticket_no,
            datetime=datetime.combine(ticket_date, time(hour=hour, minute=minute)),
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
            destination_id=destination_id,
            yard_id=yard.id,
            final_disposal=product_final_disposal,
            used_on_site=product_used_on_site,
            gross_kg=gross_kg,
            tare_kg=tare_kg,
            net_kg=net_kg,
            qty=qty,
            unit_id=unit.id,
            unit_price=ticket_unit_price,
            total=total,
            pricing_basis=pricing_basis,
            pricing_unit_name=unit.name,
            pricing_unit_type=unit.unit_type,
            pricing_unit_price=ticket_unit_price,
            pricing_qty_snapshot=qty if pricing_basis == "QTY" else None,
            pricing_net_kg_snapshot=net_kg if pricing_basis == "WEIGHT" else None,
            pricing_billable_qty_snapshot=billable_qty,
            po_number=(
                f"PO-{ticket_date.strftime('%y%m')}-{ticket_number:04d}"
                if bool(customer.must_have_po)
                else None
            ),
            dont_invoice=bool(customer.do_not_invoice or customer.is_cash_account),
            paid=bool(customer.is_cash_account and status == TicketStatusEnum.COMPLETE.value),
            wip_snapshot_json=ticket_wip_snapshot(customer=customer, product=product),
            **ticket_kwargs,
        )
        if (
            status == TicketStatusEnum.COMPLETE.value
            and transaction_type in WASTE_TYPES
        ):
            _apply_demo_wtn_signatures(
                ticket,
                signature_plan=DEMO_WASTE_TICKET_SIGNATURES.get(ticket_number),
            )
        db.add(ticket)
        ticket_rows[ticket_number] = (ticket, product, vehicle)
    db.flush()

    tax_fraction = Decimal(str(standard_tax_rate.rate_percent or 0))
    if tax_fraction > 1:
        tax_fraction /= Decimal("100")

    all_invoice_specs = list(INVOICES)
    invoice_numbers_by_year: dict[int, int] = {}
    for invoice_number, customer_code, ticket_numbers, day_key, status in all_invoice_specs:
        customer = customers[customer_code]
        invoice_date = _resolve_demo_seed_date(day_key, today=today, days=days)
        invoice_no = _demo_invoice_no(invoice_number, year=invoice_date.year)
        invoice_numbers_by_year[invoice_date.year] = max(
            invoice_numbers_by_year.get(invoice_date.year, 0),
            int(invoice_no.rsplit("-", 1)[1]),
        )
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
        for ticket_number in ticket_numbers:
            ticket, product, vehicle = ticket_rows[ticket_number]
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
                    product_snapshot_json=invoice_product_snapshot(
                        product,
                        tax_rate=product.tax_rate,
                    ),
                )
            )
            ticket.invoice_id = invoice.id
            net_total += line_net
            vat_total += line_vat

        invoice.net_total = _money(net_total)
        invoice.vat_total = _money(vat_total)
        invoice.gross_total = _money(net_total + vat_total)

    _sync_demo_sequences(
        db,
        tenant_id=tenant_id,
        ticket_numbers_by_year=ticket_numbers_by_year,
        invoice_numbers_by_year=invoice_numbers_by_year,
    )

    for offset, (customer_code, amount_text, note) in enumerate(DEMO_CUSTOMER_OWED_ADJUSTMENTS, start=1):
        customer = customers[customer_code]
        db.add(
            CustomerAdjustment(
                tenant_id=tenant_id,
                customer_id=customer.id,
                amount_decimal=_money(amount_text),
                reason="MANUAL_CORRECTION",
                note=note,
                created_by_user_id=None,
                created_at=datetime.combine(days["earlier"], time(hour=9, minute=offset * 7)),
            )
        )

    return {
        "customers": len(CUSTOMER_NAMES),
        "vehicles": len(VEHICLE_REGISTRATIONS),
        "products": len(PRODUCTS),
        "containers": len(CONTAINERS),
        "drivers": len(DRIVERS),
        "hauliers": len(HAULIERS),
        "destinations": len(DESTINATIONS),
        "tickets": len(all_ticket_specs),
        "tickets_open": sum(1 for row in all_ticket_specs if row[3] == "OPEN"),
        "tickets_complete": sum(1 for row in all_ticket_specs if row[3] == "COMPLETE"),
        "tickets_waste": sum(1 for row in all_ticket_specs if row[5] in WASTE_TYPES),
        "invoices": len(all_invoice_specs),
        "ewc_codes": len(demo_ewc_codes),
    }
