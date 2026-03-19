# weighbridge_web

FastAPI starter with SQLAlchemy, Alembic, and Postgres.

## Latest release

`v0.18` is the current release baseline. This release makes Product `Final disposal` and `Used on site` operational flags in product maintenance and ticket workflows.

It keeps scope lean: destination site/company capture now uses existing ticket destination data (with completion enforcement for final-disposal products unless marked used-on-site), adds clear ticket hints for both flags, and keeps legacy snapshot compatibility without schema changes.

## Local setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Set environment variables:

```bash
set DATABASE_URL=postgresql+psycopg://weighbridge:weighbridge@localhost:5432/weighbridge
set SECRET_KEY=change-me
set BASE_DOMAIN=127.0.0.1.nip.io
set PLATFORM_SUBDOMAIN=admin
set DEFAULT_TENANT_SUBDOMAIN=default
set RESERVED_SUBDOMAINS=admin,www,api,static
set TRUST_FORWARDED_HOST=false
```

Run database migrations (only create a new revision when models change):

```bash
alembic upgrade head
```

If you are enabling required product units, seed lookup units first:

```bash
alembic upgrade 0a9b3d5c7e21
python -m app.seed
alembic upgrade head
```

Start the app:

```bash
uvicorn app.main:app --reload
```

## Multi-tenant local development

Recommended local host pattern:

- Tenant host: `https://company1.127.0.0.1.nip.io:8000`
- Platform admin host: `https://admin.127.0.0.1.nip.io:8000`

Alternative localhost pattern is also supported:

- Tenant host: `https://company1.localhost:8000`
- Platform admin host: `https://admin.localhost:8000`

Bootstrapping flow:

1. Open admin host and create/login superadmin (`/bootstrap` if needed).
2. Open **Admin -> Tenants**.
3. Create a tenant (name + subdomain + initial tenant admin).
4. Sign in on the tenant host as the tenant admin and complete `/setup`.

Notes:

- `admin` host runs in platform mode (no tenant context).
- Unknown tenant subdomains return HTTP 404.
- Disabled tenants return HTTP 403 for tenant entrypoints including `/login`, `/branding.css`, `/static/uploads/company/...`, and `/health`.
- Branding/logo files are stored under tenant-specific directories: `/data/uploads/tenants/<tenant_id>/company/...`.
- Branding/logo URLs under `/static/uploads/company/...` are resolved per current tenant host only.
- EWC admin management routes (`/admin/ewc-codes`) are platform-only on `admin.*`.
- Tenant creation validates DNS-safe subdomains, rejects reserved names (`admin`, `www`, `api`, `static`, plus configured values), and normalizes uppercase input to lowercase.

## Table ownership policy

Global tables (shared across tenants):

- `ewc_codes`
- `ewc_import_logs`

Tenant-owned tables (tenant scoped):

- Core business: `customers`, `vehicles`, `products`, `tickets`, `invoices`
- Branding/settings: `company_settings`
- Printing: `print_templates`, `print_destinations`, `print_template_versions`, `print_jobs`
- User accounts: `users` (`tenant_id` nullable only for superadmin)

Access model:

- Tenant hosts can read required global data (for example EWC options shown in tenant workflows).
- Global admin management pages are restricted to platform mode (`admin.*`) and superadmin role.

## Railway / domain readiness

1. Configure wildcard DNS for your app domain to Railway:
   - `*.mydomain.com`
   - `mydomain.com` (optional root)
2. Set `BASE_DOMAIN=mydomain.com`.
3. Set `PLATFORM_SUBDOMAIN=admin` (or your chosen platform subdomain).
4. Route tenant traffic to `https://<tenant>.mydomain.com` and platform traffic to `https://admin.mydomain.com`.
5. Keep `TRUST_FORWARDED_HOST=false` by default. Enable only when behind a trusted proxy that sets `X-Forwarded-Host` correctly.
6. Set `ALLOWED_HOSTS` (comma-separated) to the exact hosts/subdomains you serve, and include the platform host and tenant wildcard base as needed.
7. If `TRUST_FORWARDED_HOST=true`, tenant resolution and host allow-list checks use `X-Forwarded-Host` (first value when multiple are provided).

## Release checklist

Run these checks before deployment:

```bash
python scripts/migration_smoke.py
pytest tests/test_multi_tenant_smoke.py
```

This executes:

- `alembic downgrade base`
- `alembic upgrade head`
- `alembic downgrade -1`
- `alembic upgrade head`

It verifies `void_reasons` uniqueness transitions correctly between:

- previous revision: unique on `code`
- head: unique on (`code`, `reason_type`)

## Debug tooling

- `/debug/integrity` is available only when `DEBUG=true`.
- It lists:
  - negative net weights
  - complete tickets missing weights
  - tickets referencing inactive lookups/units
- Date filtering uses server-local time (UTC by default).

## Docker

```bash
docker compose up --build
```

App: http://localhost:8000  
Health: http://localhost:8000/health
