# Changelog

All notable changes are tracked here by release chapter.

## Unreleased

### FIX
- Add build stamp in UI footer: `Build v<version> (commit <shortsha>)`.
- Add release discipline docs (`VERSION`, changelog workflow, contribution guide).

## v0.13.1 - 2026-03-03

### FIX
- Hotfix: import `Boolean` in `CompanySetting` model to prevent startup crash.
- Startup recovery: restore missing imports for templating/logo path resolution.

### DB
- Repair Alembic revision chain so `alembic heads` and `alembic stamp head` run without `KeyError: c2d3e4f5a6b7`.
