# Changelog

All notable changes are tracked here by release chapter.

## Unreleased

### FIX
- Add build stamp in UI footer: `Build v<version> (commit <shortsha>)`.
- Add release discipline docs (`VERSION`, changelog workflow, contribution guide).

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
