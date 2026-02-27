from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from sqlalchemy import func, or_, select
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from ..models import CustomerAdjustment, Invoice

MONEY_QUANTIZE = Decimal("0.01")
INVOICE_OUTSTANDING_ISSUED_STATUSES = ("ISSUED", "SENT", "OPEN")
INVOICE_OUTSTANDING_EXCLUDED_STATUSES = ("DRAFT", "PAID", "VOID")


def money_decimal(value: object | None) -> Decimal:
    if value is None:
        return Decimal("0.00")
    try:
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0.00")
    return decimal_value.quantize(MONEY_QUANTIZE, rounding=ROUND_HALF_UP)


def customer_invoice_outstanding_total(db: Session, customer_id: int | None) -> Decimal:
    if not customer_id:
        return Decimal("0.00")
    status_upper = func.upper(func.coalesce(Invoice.status, ""))
    outstanding_status = or_(
        status_upper.in_(INVOICE_OUTSTANDING_ISSUED_STATUSES),
        ~status_upper.in_(INVOICE_OUTSTANDING_EXCLUDED_STATUSES),
    )
    stmt = (
        select(func.coalesce(func.sum(Invoice.gross_total), 0))
        .where(Invoice.customer_id == customer_id)
        .where(status_upper != "")
        .where(outstanding_status)
    )
    return money_decimal(db.execute(stmt).scalar())


def customer_adjustments_total(db: Session, customer_id: int | None) -> Decimal:
    if not customer_id:
        return Decimal("0.00")
    stmt = select(func.coalesce(func.sum(CustomerAdjustment.amount_decimal), 0)).where(
        CustomerAdjustment.customer_id == customer_id
    )
    try:
        value = db.execute(stmt).scalar()
    except (OperationalError, ProgrammingError):
        # Graceful fallback when migrations are pending.
        return Decimal("0.00")
    return money_decimal(value)


def customer_outstanding_total(db: Session, customer_id: int | None) -> Decimal:
    return money_decimal(
        customer_invoice_outstanding_total(db, customer_id)
        + customer_adjustments_total(db, customer_id)
    )


def outstanding_display_values(
    raw_outstanding: Decimal,
) -> tuple[Decimal, Decimal | None]:
    normalized = money_decimal(raw_outstanding)
    if normalized < 0:
        return Decimal("0.00"), money_decimal(abs(normalized))
    return normalized, None
