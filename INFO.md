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
