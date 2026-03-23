# Changelog

All notable changes are tracked here by release chapter.

## Unreleased

## v0.19.1 - 2026-03-23

### RELEASE
- `v0.19.1` improves WTN signature visibility in ticket UIs while keeping existing completion and signature workflows unchanged.

### ADD
- Add computed ticket helpers for WTN signature state:
  - `ticket.has_wtn_signature`
  - `ticket.wtn_signature_status` (`signed`/`unsigned`/`None`)
- Add `WTN` status column on the tickets list with compact badges:
  - `Signed` for waste tickets with a saved signature
  - `Unsigned` for waste tickets without a saved signature
  - `—` for non-waste tickets
- Add a subtle warning banner on complete waste ticket detail pages when no WTN signature is present: `WTN not signed`.

### FIX
- Move the WTN signature tools out of the top-right document action stack and render them as a full-width card in the main ticket flow to remove empty header whitespace and keep header actions compact.

### TEST
- Add list-page UI coverage for signed waste, unsigned waste, and non-waste WTN status rendering.
- Extend ticket detail WTN UI coverage to assert unsigned warning visibility rules and placement above the WTN Signature card.
- Re-run targeted ticket list and WTN signature regression tests.

## v0.19 - 2026-03-22

### RELEASE
- `v0.19` introduces browser-based WTN signature capture on tickets, with WTN preview/PDF rendering from the saved ticket signature.

### ADD
- Add ticket-level WTN signature storage fields and migration:
  - `wtn_signature_data_uri`
  - `wtn_signature_signed_at`
  - `wtn_signature_signer_name` (optional)
- Add browser signature capture flow on complete waste tickets from the ticket document area:
  - Capture Signature
  - Clear
  - Save Signature
  - Cancel
- Add WTN payload variables for signature fields and expose them in print payload variable docs.
- Render saved signature, signer name, and signed timestamp in the default WTN template (preview and PDF paths).

### FIX
- Harden server-side blank-signature validation so blank detection is enforced by server-side PNG pixel analysis, not client-provided hidden fields.
- Add ticket audit events for WTN signature save/replace with ticket reference, operation type, signer name, and signed timestamp.

### TEST
- Add/extend WTN signature tests for save success, blank-signature rejection, WTN preview signature rendering, and WTN PDF signature rendering.
- Add audit regression coverage for WTN signature save and replace events.
- Re-run targeted WTN, audit-log, and unified print payload regression slices.

## v0.18.2 - 2026-03-22

### RELEASE
- `v0.18.2` is a ticket operation-flags stabilization and output-variable patch release, with focused UI polish and audit/payload coverage updates.

### FIX
- Remove the framed card styling around ticket operation flag checkboxes and add a clear `Operation flags` section title for consistency with the rest of the ticket page.
- Add a product-field tooltip clarifying that product options are filtered by direction and transaction type.
- Add a weights-section tooltip clarifying that all weights are measured in kilograms.
- Normalize `Active` status pills to use the green active styling across customer overrides, admin users/tenants, product groups, and printing admin lists.
- Include ticket-level `final_disposal` and `used_on_site` in ticket audit diff tracking so save changes to those flags are captured in audit events.
- Expose ticket-level `final_disposal` and `used_on_site` in ticket print/PDF and WTN payload variables and document them in print payload variable docs.
- Expose `{final_disposal}` and `{used_on_site}` placeholders for ticket email templates and wire default ticket email rendering to populate those values.

### TEST
- Add audit regression coverage for ticket save updates to `final_disposal` and `used_on_site`.
- Extend ticket print/WTN/unified payload tests to assert ticket-level operation flags are present in payload output.
- Extend ticket email template rendering tests to assert ticket-level operation flag placeholders resolve correctly.

## v0.17.2 - 2026-03-19

### RELEASE
- `v0.17.2` is a UI polish and operational stability release focused on print/document action layout consistency, demo reset safety, and cleaner product/unit guidance.

### FIX
- Prevent demo reset FK failures by deleting tenant-scoped AI usage logs before tenant users are removed.
- Remove the AI Assistant `Try next...` follow-up block from the drawer UI.
- Align ticket/invoice document action header rows so labels line up cleanly, with clearer spacing between titles and action buttons.
- Refine product/unit UI messaging by moving KG/Tonnes guidance into tooltip help and trimming redundant inline helper text.
- Keep system weight unit presentation cleaner in lookups (retain `Weight (system)` type indicator while removing extra row badges).
- Improve Company Settings section readability by increasing spacing rhythm between stacked fields.

### TEST
- Re-run targeted reset-demo, print/documents-panel, company-settings, product-form, and units-list regression slices after UI/stability updates.

## v0.17.1 - 2026-03-19

### RELEASE
- `v0.17.1` hardens ticket product selection by introducing explicit product transaction types (`sale` vs `waste`) and applying those rules end-to-end in ticket product options and save validation.

### ADD
- Add `products.product_type` with migration `0f1a2b3c4d5e_add_product_type_to_products.py`, including a safe backfill for existing rows.
- Add product type handling in product create/update parsing so explicit form values are persisted and sensible defaults are applied when older forms omit the field.
- Add explicit `product_type` values in demo tenant reset seed products so demo data no longer depends on migration backfill for classification.
- Add marketing demo credentials under the landing page demo button (`demo@demo.com` / `password`).
- Add tooltip-based help rendering for the edit-email attachment help text in the shared document email form header.

### FIX
- Filter ticket product dropdown options by transaction type (`SALE` -> sale products, `WASTEIN/WASTEOUT` -> waste products), with type-specific empty-state messages.
- Block transaction-type mismatches consistently during pricing/default resolution and ticket save validation (sale tickets cannot use waste-only products; waste tickets cannot use sale-only products).
- Update product field HTMX refresh wiring so transaction-type switches do not preserve stale product selections.
- Align shared document action buttons to the right in header layouts and constrain action frame width while keeping responsive mobile behavior.
- Frame Company Settings address fields as a dedicated section and tighten Document Email Defaults card embedding/layout consistency.

### TEST
- Expand `tests/test_ticket_product_usage_rules.py` to cover sale/waste type rejection, transaction-type-driven option filtering, HTMX include behavior, and empty dropdown messaging.
- Re-run targeted demo reset and ticket product usage regression slices.

## v0.17.0 - 2026-03-13

### RELEASE
- `v0.17.0` promotes the AI admin work into the release baseline, separating platform AI defaults from tenant AI overrides, adding minimum-safe operational guardrails, and cleaning up several customer and ticket UI flows.

### ADD
- Add platform-managed AI defaults and tenant AI override controls for model selection and dashboard insights, including clearer admin copy and default-vs-override precedence.
- Add minimum-safe AI hardening controls including configurable usage thresholds, backend assistant limits, dashboard throttling, graceful fallback handling, and basic AI usage logging.

### FIX
- Clean up super admin and tenant admin layouts with clearer spacing and tooltip-based help in place of inline notes.
- Update the customers list to show active pricing overrides as `Yes (x)` instead of a binary tick indicator.
- Simplify the ticket screen by moving billing details nearer the weights section, normalizing section headings, constraining transaction choices by direction, and removing the unused `Readout (kg)` field.

### TEST
- Re-run targeted super admin AI settings, customer list, ticket UI, walk-in sale, weights preview, and product-option regression slices after the release cleanup.

## v0.16.2 - 2026-03-12

### RELEASE
- `v0.16.2` introduces the tenant-facing AI chatbot inside the workspace, giving operators a live assistant drawer for operational questions without leaving the current screen.

### ADD
- Add a tenant-visible AI Assistant trigger and right-side drawer across the workspace for AI-enabled tenants.
- Add quick prompts for open tickets, today's tonnage, unpaid invoices, and recent tickets, wired to the existing tenant-scoped assistant endpoint.

### FIX
- Polish the assistant drawer copy, loading state, slide-in behavior, and response layout so answers appear cleanly under the response heading.

### TEST
- Re-run tenant assistant workspace UI visibility and assistant query smoke coverage for enabled and disabled tenants.

## v0.16.1 - 2026-03-12

### RELEASE
- `v0.16.1` delivers the first platform-managed AI assistant release for tenant workspaces, with tenant-level enable/disable controls, per-tenant model selection, and a read-only assistant path designed for operational queries.
- This release also corrects the Alembic migration chain so production deployments resolve to a single head and `alembic upgrade head` runs cleanly.

### ADD
- Add OpenAI-backed assistant backend using `gpt-5-mini` as the platform default, with tenant-scoped operational context for open tickets, uninvoiced work, today’s weight total, unpaid invoices, and recent activity.
- Add platform admin tenant controls for `AI Assistant Enabled` and tenant model selection, including safe default guidance and runtime enforcement of tenant-level AI availability.

### FIX
- Repair the AI settings migration parent revision so the Alembic graph is linear again and Railway-style deploys no longer fail on multiple heads.

### TEST
- Verify tenant AI assistant runtime, tenant model selection, disabled-tenant rejection, read-only guardrails, and platform admin tenant AI settings.
- Verify `python -m alembic heads`, `python -m alembic history`, and `python -m alembic upgrade head` against a clean smoke database.

## v0.16 - 2026-03-12

### RELEASE
- `v0.16` is the latest release baseline. Multi-tenant workspace support is now implemented, the current issue-fix backlog has been cleared, and follow-up work from this point is expected to focus on additional features and enhancements.
- This release closes out the tenant rollout chapter with tenant isolation, tenant-aware audit coverage, and a broad UI cleanup pass across the operational dashboard and core list/detail screens.

### FIX
- Complete tenant rollout across host-based workspace routing, tenant admin management, tenant-scoped branding/uploads, tenant-aware numbering/uniqueness rules, and audit-log isolation.
- Expand tenant audit coverage so company settings, lookups, products, vehicles, customer price overrides, and printing administration all write tenant-scoped audit events.
- Replace the legacy `30 Days` dashboard mode with a clearer `12 Months` monthly view and add period totals to both ticket activity and weight throughput charts.
- Fix dashboard chart presentation issues including throughput edge clipping and add clearer operational summaries for today, 7-day, and 12-month views.
- Resolve ticket/list UX bugs including ad-hoc vehicle registration visibility, ticket action placement, reset/pager button styling, invoice filter consistency, and lookup search layout alignment.
- Apply a wider UI polish pass across invoices, tickets, lookups, customers, products, and vehicles to standardize filters, buttons, chart spacing, and navigation controls.

### TEST
- Re-run tenant dashboard, tenant audit, lookup, product/pricing override, printing admin, vehicle, and list-table regression slices after the release polish.

## v0.15.3 - 2026-03-11

### FIX
- Expand and polish the demo tenant reset dataset with realistic customers, vehicles, products, EWC links, tickets, invoices, branding defaults, and lookup data so demo environments feel like active live systems after reset.
- Resolve tenant backend issues across tenant-scoped numbering, uniqueness, setup, branding, migration, and reset flows so tenant admin and demo reseed paths stay consistent.
- Tidy product and ticket UI regressions by rendering product basis values as colored tags and removing obsolete ticket print notices.
- Add build stamp in UI footer: `Build v<version> (commit <shortsha>)`.
- Add release discipline docs (`VERSION`, changelog workflow, contribution guide).

### TEST
- Re-run demo reset, EWC import, products list, ticket print, and tenant smoke regressions after the demo seed and tenant backend fixes.

## v0.15.2 - 2026-03-07

### FIX
- Clean up stale tenant dashboard and invoice context fields, remove old non-dashboard home copy, and keep the dashboard summary/chart spacing fixes in place.
- Tighten callback and compatibility-only parameters so active compatibility paths remain explicit without carrying dead helper plumbing.

### TEST
- Re-run auth, tenant dashboard, setup wizard, invoice PDF, print payload, and product list regressions after the cleanup pass.

## v0.15.1 - 2026-03-06

### FIX
- Make UI fixes for the backend tenant management pages, including tenant detail spacing, action placement, and login form button spacing.

## v0.15.0 - 2026-03-05

### FIX
- Add host-based multi-tenant routing with platform/tenant separation, tenant activation enforcement, reserved subdomain validation, and forwarded-host/allowed-host controls.
- Store branding uploads under `/data/uploads/tenants/<tenant_id>/company`, serve them through tenant-scoped routes only, and block shared static upload fallbacks.
- Add superadmin tenant management, tenant baseline seeding, deterministic role normalization, and first-platform-user superadmin bootstrap behavior.
- Close admin-only permission gaps for customer credit controls and printing administration so operator users cannot reach those entrypoints.

### TEST
- Add multi-tenant smoke coverage for tenant host isolation, disabled-tenant responses, upload collision/traversal blocking, tenant creation seeding, and superadmin tenant actions.
- Add auth/setup regression coverage for bootstrap superadmin creation, role normalization, admin-host login restrictions, and superadmin-only system status access.

## v0.14.4 - 2026-03-04

### FIX
- Purge navbar hardcoded text colours so header text/link/auth elements use `var(--nav-fg)` with hover/focus readability based on opacity/underline, not fixed hues.
- Keep active navbar item visible via `var(--primary)` decoration (bottom border) while retaining readable `var(--nav-fg)` text.
- Keep fallback title strict in navbar templating: render `Weighbridge Web` only when both `show_logo_nav` and `show_title_nav` are disabled.
- Add Company Settings branding debug readout (DEV mode or superadmin) showing `show_logo_nav`, `show_title_nav`, `nav_bg`, `nav_fg`, and branding stylesheet link state.
- Promote shared `nav_foreground_color(...)` helper so `/branding.css` and admin diagnostics use the same foreground-contrast logic.

### TEST
- Extend branding assertions for strict fallback behavior (`logo on/title off` has no fallback string; `both off` shows fallback).
- Add CSS-level assertion that navbar link color is bound to `var(--nav-fg)` and not hardcoded hex/primary text color.
- Add Company Settings debug-readout visibility test for superadmin flow.

## v0.14.3 - 2026-03-04

### FIX
- Remove navbar logo frame styling by clearing badge/image border, background, shadow, and radius styles so uploaded logos render cleanly.
- Keep navbar fallback rendering strict: show `Weighbridge Web` only when both navbar logo and title toggles are disabled.
- Add automatic navbar foreground colour (`--nav-fg`) derived from `--nav-bg` in `/branding.css`, and bind navbar text/link/badge styles to `var(--nav-fg)` for readability.

### TEST
- Add assertions that `/branding.css` emits `--nav-fg`.
- Extend navbar CSS regression coverage to assert `var(--nav-fg)` usage and no framed logo badge styling.

## v0.14.2 - 2026-03-04

### FIX
- Add Company Settings branding control sync script (`company_branding.js`) so navbar/primary colour picker and HEX text inputs stay in sync both ways, normalize to uppercase `#RRGGBB`, and show inline invalid-hex hints.
- Tidy Company Settings branding UI with compact inline colour controls and a centered, fixed-height logo preview block.
- Fix navbar branding toggle rendering logic so logo and title render independently, with a neutral `Weighbridge Web` fallback label only when both are disabled.

### TEST
- Add regression coverage that Company Settings renders both colour input types and includes the branding sync JS asset.
- Expand navbar brand visibility coverage for all four combinations: both on, logo-only, title-only, and both off fallback.

## v0.14.1 - 2026-03-04

### FIX
- Move company uploads to a single `UPLOADS_DIR` root (default `/data/uploads`), ensure `company/` is created at startup, and mount it at `/static/uploads` for reliable logo serving.
- Add CSP-safe dynamic branding stylesheet at `/branding.css` and load it globally with `?v=<BUILD_STAMP>` instead of inline theme variable injection.
- Normalize logo file resolution across UI/PDF/print/health services to use the configured uploads directory (with consistent `/static/uploads/company/...` web paths).

### TEST
- Add regression coverage for `/branding.css` rendering DB theme colours and `Cache-Control: no-store`.
- Add coverage that uploaded logo URLs are directly retrievable (`200 OK`) from `/static/uploads/company/...`.
- Update global branding page assertions (`/login`, `/products`, `/admin/system-status`) to validate `branding.css` injection and values.

## v0.14.0 - 2026-03-04

### FIX
- Add global deploy cache-busting via `BUILD_STAMP` and append it to core static assets in `base.html` (`style.css`, `help_tooltips.js`, `toasts.js`, `table_rows.js`).
- Enforce theme ownership with explicit final `!important` cascade guards for `.site-header` and `.btn--primary` (`var(--nav-bg)` / `var(--primary)`).
- Keep navbar logo rendering readable by using the logo badge treatment and removing forced white fill from the logo image itself.
- Simplify Company Settings logo preview to a fixed plain image container and add explicit diagnostics (`Open logo` link, resolved logo URL, and file-exists status).

### TEST
- Add regression assertions that rendered pages include `style.css?v=<BUILD_STAMP>`, include DB-driven `--nav-bg`, and keep navbar CSS bound to `var(--nav-bg)`.
- Add coverage for Company Settings logo diagnostics visibility.

## v0.13.9 - 2026-03-03

### FIX
- Force navbar theme ownership by keeping the final `.site-header` rule bound to `background-color: var(--nav-bg)`.
- Improve navbar logo readability with a dedicated logo badge style and remove forced white logo background styling.
- Add dual logo preview surfaces (light + dark navbar simulation) on Company Settings to catch contrast issues before saving.
- Bump stylesheet cache key so updated branding UI styles are picked up immediately.

### TEST
- Extend branding CSS regression coverage to ensure later `.site-header` rules do not reintroduce hardcoded hex backgrounds after the `var(--nav-bg)` declaration.
- Add coverage for Company Settings dual logo preview blocks.

## v0.13.8 - 2026-03-03

### FIX
- Harden theme cascade for branding by keeping final `.site-header` and `.btn--primary` rules bound to `var(--nav-bg)` and `var(--primary)`.
- Bump stylesheet cache key to ensure browsers pull the updated cascade rules immediately.

### TEST
- Add regression coverage to block reintroduction of hardcoded `.site-header` hex backgrounds after the `var(--nav-bg)` rule.

## v0.13.7 - 2026-03-03

### FIX
- Fix global branding injection so nav/primary/logo values resolve through a single `get_branding(...)` path for all template renders.
- Add robust logo fallback behavior and versioned logo URLs (`?v=<timestamp>`) when rendering navbar/favicon assets.
- Apply theme variables (`--nav-bg`, `--primary`) consistently in shared layout/CSS so navbar and primary controls match branding across pages.
- Add cache policy split: HTML responses `no-store`, `/static/uploads/*` cacheable (`public, max-age=86400`).
- Add branded 403/404/500 fallback templates for plain text error responses and expose branding diagnostics on `/admin/system-status`.

### TEST
- Add coverage for global branding vars on `/login`, `/products`, and `/admin/system-status`.
- Add cache-control assertions for HTML and `/static/uploads/*` responses.
- Add assertion that uploaded logo URLs are rendered with cache-busting version query strings.

## v0.13.6 - 2026-03-03

### AUDIT
- Add auth audit events for `LOGIN_SUCCESS`, `LOGIN_FAILED`, and `LOGOUT` (with actor and request IP capture).
- Add user account creation audit events (`USER_CREATE`) for web and CLI bootstrap paths.
- Add shared whitelist-based `audit.diff(before, after, keys)` helper and wire curated update deltas for customer, product, and ticket updates.
- Improve admin audit viewer with exact `entity_id` filtering, explicit entity `View` links, and `Europe/London` timestamp display.

### TEST
- Add assertions for auth audit event coverage (login success/failure and logout).
- Add idempotency assertions that ticket complete and invoice paid actions create exactly one corresponding audit event when retried.

### DOCS
- Add `docs/AUDIT.md` documenting logged events, excluded sensitive data, and retention intent (2 years).

## v0.13.5 - 2026-03-03

### AUDIT
- Add `audit_events` table (tenant-ready nullable `tenant_id`) with indexed `occurred_at`, entity, action, and user-time lookups.
- Add shared `audit.log(...)` helper and wire high-value event logging for ticket create/update/complete/void, invoice create/paid/void, customer create/update/flags/credit-limit/credit-adjustment, and product create/update.
- Add `/admin/audit` viewer (ADMIN/SUPERADMIN) with filters for entity type, action, user, date range, and entity id; includes best-effort links to related entities.

### TEST
- Add `tests/test_audit_log.py` covering ticket completion, customer create, invoice paid event creation, and admin audit access control rules.

## v0.13.4 - 2026-03-03

### FIX
- Repo hygiene: ignore `htmlcov/` and remove tracked `__pycache__/` / `*.pyc` artifacts from git index.
- Housekeeping: switch `app/routes/debug.py` to use shared `app.templating.templates` (remove local `Jinja2Templates(...)` instance).
- Add GitHub Actions pytest gate at `.github/workflows/tests.yml` (push + PR, Python 3.11, `pytest -q`).
- Keep intentional navbar polish changes in `app/static/css/style.css` and bump stylesheet cache version in `app/templates/base.html`.

## v0.13.2 - 2026-03-03

### SETUP
- Clean production setup flow for first-run initialization.
- Default print templates and destinations are always created during `/setup`.
- Demo lookups seed is removed from production `/setup` (dev-mode only).

### FIX
- Housekeeping cleanup of unused legacy setup/auth constants and dead helper code in `app/main.py`.

## v0.13.1 - 2026-03-03

### FIX
- Hotfix: import `Boolean` in `CompanySetting` model to prevent startup crash.
- Startup recovery: restore missing imports for templating/logo path resolution.

### DB
- Repair Alembic revision chain so `alembic heads` and `alembic stamp head` run without `KeyError: c2d3e4f5a6b7`.
