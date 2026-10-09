---
title: Postgres "contention" was a race in the connect guard
type: issue
tags: [postgres, migrations, pytest, gotcha]
related_files:
  - src/run.rs
  - tests/conftest.py
  - tests/test_postgres_isolation.py
related_issues: [511]
related_prs: [601]
captured: 2026-10-08
---

# Postgres "contention" was a race in the connect guard

A Postgres test that fails only when two suites share one server and passes
alone on rerun is not "contention". For days of epic #511 it was a real bug in
`connect(auto_migrate=True)`.

## What it looked like

Two test runs against one server, and one of these failed in each:

```
OperationalError: reading a tracking table's format:
relation "ferro_3fa9c1._ferro_migrations_format" does not exist
```

It hit pin (e), the connect guard's "neighbour schema" test, an auto-migrate
rename test and an RLS owner test. Every one passed on rerun, so each was
written off as "shared-Postgres contention" and retried.

## The actual cause

Say tenant `tenant_b` is being deprovisioned while tenant `tenant_a` boots. The
guard refuses a database that a migration chain already tracks, so it:

1. lists every schema holding a `_ferro_migrations_format` table, then
2. reads each one with its own `SELECT`.

If `DROP SCHEMA tenant_b CASCADE` lands between 1 and 2, the read in step 2
fails and the whole pass with it. In the tests, the other run's teardown played
`tenant_b`. This was a product bug (a tenant could hit it in production), not a
test artefact. The code is `tracking_tables_for` in `src/run.rs`.

## The fix

A read that fails with SQLSTATE `42P01` (`undefined_table`) or `3F000`
(`invalid_schema_name`) is skipped only after the catalog confirms the table is
gone. A dropped format table governs nothing. Any other error, or a table that
is still there, raises. `tests/test_postgres_isolation.py` pins it with a
hundred passes beside a schema that is created and dropped in a loop.

## The lesson

A failure that appears only under concurrent runs and passes alone is a
*lead*, not a verdict. Before writing "contention", reproduce it: run two
suites at once, or loop the suspect call beside a churning schema, as
`tests/test_postgres_isolation.py` does. A script reproduced this one on 4 of
the first 10 connects.

## Other leaks the same work closed (#601)

Check these places first the next time two runs disagree:

- Catalog reads by name alone (`pg_indexes`, `pg_constraint`, `pg_tables`,
  `pg_type`, `information_schema.columns`): scope them to `current_schema()`.
- Roles are server-global: name them with the `pg_role(label)` fixture, which
  `db_url` drops after the test's schema.
- `pg_stat_statements` installs once per database: hold
  `postgres_server_lock(base_url, name)` from check to `DROP EXTENSION`.
- Lock-timeout holders timed from taking the lock, not from the moment another
  session queues behind it: start the hold when `pg_locks` shows a waiter.

## How to recognize

- The error names `ferro_<hex>.<table>` for a schema your test did not make.
- It disappears when you rerun the one file.
- Two `pytest --db-backends=sqlite,postgres` runs at once make it fail every
  time.
