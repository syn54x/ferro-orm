# Table checks emit on SQLite create; reconciliation is Postgres-only

A table check is inline in `CREATE TABLE` on both dialects. Adding, rebuilding, or dropping one on an existing table is PostgreSQL-only: SQLite cannot `ALTER TABLE … ADD/DROP CONSTRAINT` without a table rebuild, so the reconciliation pass warns with the constraint name and skips. Table checks are not a postgres-only schema object — SQLite can represent them at create time, unlike materialized views or native enums. Column `db_check` staying elided on SQLite (its emission path is ALTER-shaped) is unchanged and out of scope.

Decision by owner (2026-08-18), grilling #339.

Rejected alternatives:

- **Postgres-only (skip SQLite create too)**: leaves a fresh SQLite schema unenforced with no path except "use Postgres," for a constraint SQLite's `CREATE TABLE` already supports.
- **Elide on create to match column `db_check`**: copies a limitation of the ALTER-shaped column-check path rather than of SQLite.
- **Table-rebuild on SQLite reconcile**: a half-working in-place migrate; Alembic's batch mode is the reviewed-rebuild door.
