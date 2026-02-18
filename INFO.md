# Info

## Field Length Policy

The app uses a single canonical field-length policy from `app/constants/field_limits.py`.
Single source of truth rule: define/update limits only in that module and import them everywhere else (models, routes, templates).

| Field | Limit |
| --- | ---: |
| `CODE_MAX` | 50 |
| `NAME_MAX` | 120 |
| `DESC_MAX` | 255 |
| `NOTES_MAX` | 1000 |
| `REG_MAX` | 15 |
| `POSTCODE_MAX` | 16 |
| `ADDRESS_LINE_MAX` | 120 |
| `PO_NUMBER_MAX` | 50 |
| `NOMINAL_CODE_MAX` | 20 |

Implementation guarantees:

- DB schema enforces hard limits (via bounded `String(...)` columns).
- Server-side validators return friendly max-length errors.
- UI form controls apply matching `maxlength` values.
- List/table cells use truncation classes with hover tooltips to prevent layout breaks.
- Migration note: narrowing migrations pre-truncate overlength data and are lossy by design.

## Walk-In Sales Policy

Walk-in sale mode is a simple counter-sale flow for `SALE` tickets where no customer invoice is created.

- Ticket rule: `walk_in_sale=true` is accepted only when `transaction_type == SALE`.
- Invoicing rule: walk-in sale forces `dont_invoice=true` and those tickets are excluded from invoice generation.
- Customer rule: SALE tickets in walk-in mode can complete without `customer_id`; when walk-in mode is off, customer is required as normal.
- UI rule: walk-in mode hides/disables customer and logistics fields (`haulier`, `driver`, `container`, `destination`, `area`) while keeping product/pricing/qty-weight rules unchanged.
- Waste workflows remain strict and unchanged (customer + waste compliance required on complete).
- Reporting: walk-ins are tagged via `tickets.walk_in_sale` and can be filtered in the tickets list.
