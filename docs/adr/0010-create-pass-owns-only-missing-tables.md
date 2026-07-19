# Auto-migrate pass ownership: the create pass creates missing tables; the reconciliation pass owns every existing table

The auto-migrate **create pass** (`CONTEXT.md`) brings missing tables into
existence — table, columns, indexes, constraints, together — and leaves a
table that already exists completely untouched, whatever its shape. All DDL
against an existing table belongs to the **reconciliation pass**
(`migrate_updates`), which orders column changes before the indexes and
constraints that reference them. This makes the documented contract
("existing tables are left untouched unless `migrate_updates` is set") the
enforced one: previously the create pass also fired `CREATE INDEX IF NOT
EXISTS` at existing tables, which crashed the single-deploy shape of adding
columns plus a composite unique over them — the index DDL ran before the
reconciliation pass could add the columns (#324).

Decision by owner (2026-07-19), grilling #324/#325.

Rejected alternatives:

- **Keep the create pass's index DDL, gated on "all referenced columns
  exist"**: preserves two competing owners of index creation — the exact
  divergence that produced #324 — and turns the crash into order-dependent
  behavior (the index appears or not depending on which pass runs first).
- **Run the reconciliation pass before the create pass**: reconciliation
  diffs against live tables and would race table creation for new tables;
  the dependency between the passes is "create what's missing, then
  reconcile what exists," not the reverse.

## Consequences

- Under plain `auto_migrate=True` (no `migrate_updates`), adding an index —
  single-column or composite — to an already-live table no longer creates
  it. That convenience was undocumented, accidental, and the crash vector;
  index reconciliation on existing tables requires `migrate_updates=True`,
  where it already lives correctly ordered.
- `create_tables()` shares the create pass and becomes literally "create
  missing tables."
- The create pass must learn which tables exist (one introspection query up
  front) instead of leaning on `IF NOT EXISTS` to paper over partial
  application.
