# Changelog

All notable changes are tracked here by release chapter.

## Unreleased

### FIX
- Add build stamp in UI footer: `Build v<version> (commit <shortsha>)`.
- Add release discipline docs (`VERSION`, changelog workflow, contribution guide).

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
