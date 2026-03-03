# Changelog

All notable changes are tracked here by release chapter.

## Unreleased

### FIX
- Add build stamp in UI footer: `Build v<version> (commit <shortsha>)`.
- Add release discipline docs (`VERSION`, changelog workflow, contribution guide).

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
