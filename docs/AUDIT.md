# Audit Logging Policy

## Scope

The application records operational audit events in `audit_events` for security, traceability, and incident review.

## What Is Logged

- Authentication:
  - `LOGIN_SUCCESS`
  - `LOGIN_FAILED` (generic invalid credentials reason)
  - `LOGOUT`
- User account lifecycle:
  - `USER_CREATE`
  - `USER_UPDATE` (when implemented on user admin routes)
  - `USER_DISABLE` / `USER_ENABLE` (when implemented on user admin routes)
  - `USER_ROLE_CHANGE` (when implemented on user admin routes)
  - `USER_PASSWORD_RESET` (when implemented)
- Core business actions:
  - Ticket create/update/complete/void
  - Invoice create/paid/void
  - Customer create/update and credit adjustments
  - Product create/update

Each audit row includes:

- UTC event timestamp
- actor user id (if known)
- request IP address (best effort via forwarded/real/client address)
- action + entity type/id
- human summary
- optional safe `details_json`

## Change Deltas

For key update flows, `details_json` stores whitelisted field deltas only:

```json
{
  "changed": {
    "field_name": { "from": "old", "to": "new" }
  }
}
```

No full request payloads are logged.

## What Is Intentionally Not Logged

- Raw passwords or password hashes in audit detail payloads
- Session secrets, CSRF tokens, cookies, auth headers
- Full request bodies and arbitrary unbounded form/query payloads
- Large binary payloads or file contents

## Retention Intent

Policy intent: retain audit events for 2 years.

Enforcement/archival automation is not yet implemented in this release.
