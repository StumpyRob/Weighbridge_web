# Contributing

## Branch Workflow

1. Create a feature branch from `main`.
2. Open PR into `dev` for validation and review.
3. Merge `dev` into `main` only for a release-ready chapter.

If `dev` does not exist yet, use feature branch -> `main`, then keep this release flow from the next cycle.

## Commit Prefixes (Required)

Use one of these prefixes at the start of every commit subject:

- `AUTH:` login/roles/permissions
- `AUDIT:` event logging/admin audit views
- `PRINT:` templates/profiles/destination printing
- `STORAGE:` uploads/volume/storage behavior
- `SEC:` security headers/cookies/CSRF/session hardening
- `FIX:` bugfixes/cleanup/ops changes
- `DB:` migrations/schema/data backfills

Examples:

- `AUTH: add bootstrap route for first superadmin`
- `AUDIT: log ticket completion + invoice paid`
- `SEC: scope caching for /static/uploads`

## Release Discipline (Tag + Changelog)

Every chapter release must include:

1. Update `VERSION` to the target version, e.g. `0.13.2`.
2. Move release notes from `docs/CHANGELOG.md` `Unreleased` into a new `vX.Y.Z - YYYY-MM-DD` section.
3. Commit release prep with prefix, usually `FIX:` (or `DB:` if migration-only release).
4. Create an annotated tag:
   - `git tag -a vX.Y.Z -m "Release vX.Y.Z"`
5. Push branch and tags:
   - `git push origin main`
   - `git push origin vX.Y.Z`

This keeps each deploy mapped to:

- one version (`VERSION`)
- one changelog chapter (`docs/CHANGELOG.md`)
- one git tag (`vX.Y.Z`)
