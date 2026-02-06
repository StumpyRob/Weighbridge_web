# Dev Notes (WIP Inventory)

This file lists fields/features that are intentionally **not implemented** yet, and how they behave in the UI.

## Tickets

- Page: `Tickets -> Edit Ticket` (`app/templates/tickets/edit.html`)
  - **Yard (optional)** (`yard_id`)
    - `DEV_MODE=true`: shown as disabled WIP field with hint "Midsoft yard integration not implemented yet."
    - `DEV_MODE=false`: hidden.
    - Submission: not posted while WIP (field not present in form; backend ignores it).
  - **Area (optional)** (`area_id`)
    - `DEV_MODE=true`: shown as disabled WIP field with hint "Midsoft area integration not implemented yet."
    - `DEV_MODE=false`: hidden.
    - Submission: not posted while WIP (field not present in form; backend ignores it).

## Products

- Page: `Products -> New/Edit Product` (`app/templates/products/_form.html`)
  - **Tax rate** (`tax_rate_id`)
    - Implemented and used for invoice VAT calculation.
    - Requires `tax_rates` lookup rows to exist (seeded via `python -m app.seed`).
  - **Product group**
    - Stored on `Product`, but no downstream behaviour yet (no grouping logic/UI).
    - `DEV_MODE=true`: shown as disabled WIP field with hint "Stored on product but not used yet."
    - `DEV_MODE=false`: hidden.
  - **Nominal code**
    - Stored on `Product`, but no downstream behaviour yet (invoicing/export/reporting).
    - `DEV_MODE=true`: shown as disabled WIP field with hint "Stored on product but not used yet."
    - `DEV_MODE=false`: hidden.
  - **Final disposal** (flag)
    - Status: NOT IMPLEMENTED
    - `DEV_MODE=true`: shown as disabled WIP checkbox.
    - `DEV_MODE=false`: hidden.
    - Intended future use: waste compliance / reporting logic.
    - Submission: not posted while WIP (field not present in form; backend ignores it).
  - **Used on site** (flag)
    - Status: NOT IMPLEMENTED
    - `DEV_MODE=true`: shown as disabled WIP checkbox.
    - `DEV_MODE=false`: hidden.
    - Intended future use: internal consumption / stock tracking.
    - Submission: not posted while WIP (field not present in form; backend ignores it).
  - **Pricing card (entire section)**
    - `DEV_MODE=true`: shown with disabled WIP fields.
    - `DEV_MODE=false`: hidden entirely.
  - WIP fields (all show disabled with hint "Not implemented" in dev mode):
    - Account price
    - Cash price
    - Min price
    - Max price
    - Max qty
    - Excess trigger
    - Excess price

## Customers

- Page: `Customers -> New/Edit Customer` (`app/templates/customers/_form.html`)
  - `DEV_MODE=true`: shows disabled WIP fields and hints for non-functional billing/pricing metadata.
  - `DEV_MODE=false`: hides those WIP fields entirely.
  - WIP fields:
    - VAT number
    - Invoice frequency
    - Payment terms
    - Credit limit
    - Cash account (flag)
    - Do not invoice (flag)
    - Must have PO (flag)
  - Live fields (always shown):
    - Account code, name, invoice email, phone, address fields
    - On stop (flag)

## Global indicators

- Navbar pill: shows **DEV MODE** when `DEV_MODE=true` (`app/templates/base.html`).
- Page banner: shows at top of pages that set `has_wip_fields` when `DEV_MODE=true` (`app/templates/base.html`).
