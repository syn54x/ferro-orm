---
title: A REVOKE from PUBLIC in one Postgres test breaks every other worker
type: issue
tags: [pytest, postgres, gotcha]
related_files:
  - tests/conftest.py
  - tests/test_session_set_config.py
related_issues: [984]
captured: 2026-11-17
---

# A REVOKE from PUBLIC in one Postgres test breaks every other worker

## Symptom

An intermittent `permission denied for function set_config` in a test that
revokes nothing, such as
`test_a_reconciled_policy_actually_filters_rows[postgres]`. It shows up on the
Postgres CI shards only, passes on a re-run, and passed on the PR that
introduced it.

## Cause

`db_url` isolates a Postgres test by schema, but every xdist worker shares one
database. A privilege on a `pg_catalog` function belongs to the database, not
to the schema. `test_reapply_failure_still_commits_the_swap_and_the_eviction`
ran `REVOKE EXECUTE ON FUNCTION pg_catalog.set_config(...) FROM PUBLIC`, so
until its `GRANT` undid it, any test on another worker that connected as a
`NOSUPERUSER` role could not call `set_config`. A superuser bypasses the
revoke, which is why most tests never noticed.

## Rule

A test that changes database-wide state (a privilege on a `pg_catalog`
object, an extension, a database setting) asks for `own_database_url`
instead of `db_url`. The fixture creates a database and a schema named after
the test, and drops the database at teardown, pass or fail.

`test_the_reapply_failure_test_leaves_set_config_granted_in_the_shared_database`
pins the fix: it runs the revoking test in a child pytest and checks that
PUBLIC keeps `EXECUTE` in the shared database throughout.
