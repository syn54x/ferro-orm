# JSONB is the default JSON storage on Postgres

Derived json-family fields (`dict`/`list`/nested-model with no explicit
`db_type`) now store as **JSONB** on Postgres — the storage Postgres itself
recommends for nearly all JSON workloads and what most users would opt into
anyway. Explicit `db_type="json"` is the opt-out for workloads that need
representation fidelity (jsonb does not preserve dict key order); explicit
`db_type="jsonb"` remains valid and now simply states the default. SQLite is
untouched: both tokens lower to the same plain-JSON storage, so one model
definition still runs identically on both backends.

This reverses the "opt-in only; no default changes" decision of the original
JSONB PRD (#260) and ADR-0004's defaults-unchanged consequence, by owner
decision (2026-07-10): pre-v1.0 is the window for breaking storage defaults,
and shipping the wrong default forever to avoid a one-time migration is the
worse trade. The flip lives at the single derived-storage cascade
(`canonical_from_parts`), dialect-split exactly like `boolean` — explicit
tokens still win before the cascade runs.

## Consequences

- **Existing Postgres databases migrate on next connect** (with
  `migrate_updates=True`): each live plain-`json` column with a derived
  declaration diffs once, producing one
  `ALTER TABLE ... TYPE jsonb USING ...`; row values survive the cast. Users
  who want to keep plain json declare `db_type="json"` before upgrading —
  then the diff is zero operations. Alembic users see the same one-time
  migration through the bridge.
- **Round-trip semantics change for defaults**: dict key order is no longer
  preserved by default on Postgres (Python `==` equality still holds — see
  ADR-0004). Key-order-sensitive workloads must opt out.
- The parity suites pin the flip with exact-equality assertions (substring
  checks cannot tell `JSON` from `JSONB`).
- SQLite: zero behavior change, zero migration.
