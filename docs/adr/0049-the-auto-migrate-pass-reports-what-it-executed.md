# The auto-migrate pass reports what it executed

Amends ADR-0038.

A project adds `slug: str | None = None` to `Author` and calls the pass by hand:

```python
report = await ferro.migrate()
[(s.subject, s.sql) for s in report.statements if s.role == "schema"]
# [('author', 'ALTER TABLE "author" ADD COLUMN "slug" TEXT')]
[(w.kind, str(w)) for w in report.warnings]
# []
```

`ferro.migrate()` and `ferro.create_tables()` will return a `PassReport`, a frozen, public value: every statement the pass sent to the database, in order, and every warning it raised. `connect(auto_migrate=…)` stays `-> None`; it builds the same report and logs from it.

- `statements` is a tuple of `ExecutedStatement(subject, sql, role)`. `subject` is the table or enum type the statement belongs to. `role` is one of:
  - `"schema"`: the create pass, type statements, the reconciliation pass;
  - `"lock_timeout"`: the `SET LOCAL lock_timeout` and `SET` / `RESET` lines of ADR-0044;
  - `"probe"`: the SQLite label row probe of ADR-0047.
- `warnings` is a tuple of `Report(kind, subject, text, recurs)`, where `str()` gives the text. It is the typed report decided in ADR-0052, which the planner, the renderer and the pass are to share: the planner's and renderer's kinds, plus the pass's own `PendingTableRename`, `StrandedLabelRename` and `RowSecurityUnderMigrator`. The pass's two operational warnings, waiting for the run lock and retrying under the DDL lock timeout, join that enum as `RunLockWait` and `DdlLockRetry`. `recurs` says whether the warning repeats on every pass (today's "always" channel). Every warning is still raised as a Python warning, as before. The report lists it as well.
- A pass that fails partway raises its usual error, carrying `.report`: what committed before the failure, with the failing statement left out. On Postgres each table is its own transaction, so the earlier tables stay changed, and the report is the record of which.

The report is to be built from what the DDL executor actually ran, never rebuilt from the plan, so it can never describe a statement that did not execute. For that, the executor is to take statement lists instead of closures: `ddl.run(engine, unit, &[Statement { subject, sql, role }], on_attempt, settle)` will return the statements it ran, or the index of the one that failed. That makes it the single place every pass statement goes through.

The executor is to run every statement unprepared. Today the create pass sends its `CREATE TABLE` and follow-on statements through `execute_sql`, which uses the prepared-statement cache, and every other DDL path uses `execute_sql_unprepared`. Caching a statement against a schema that the same pass is about to change can poison the connection it ran on. Once every statement goes through one executor that never prepares, that defect cannot come back. A pin is to cover it (A-0): a `CREATE TABLE` followed by a schema change on the same connection.

## Why a report and not the log

Until now the pass's only observable output was its debug log, and the tests read that log: three line prefixes, a wrapped `warnings.warn`, and a recorder plugin that re-ran ten test files in subprocesses against JSON recordings keyed by test node ID. Production logging was shaped by the tests. The lock-timeout statements got their own prefix "so every recording … reads exactly what it did before". Re-recording was the expected way out of a failure, so a failure said little. A user who wanted to know what auto-migrate did had nothing but the log either.

The recorder did two jobs. Each gets an owner when the recorder is retired:

- **"The pass runs what the planner renders" (AGENTS.md I-1).** A new parity pin (g) is to run `ferro.migrate()` over every casebook case on both dialects. It asserts that the report's schema statements equal the one planner's rendering for the same pair, after the pass's documented adjustments: the create pass's `CREATE TABLE` stands in for each add, and the statements are grouped by table. It also asserts that the report's warnings equal the plan's reports by kind and subject, never by sentence. Pins (a) and (d) compare the generator with the plan. Pin (g) is the one that closes the gap between the plan and what actually ran.
- **"No scenario's DDL changed unnoticed."** Each scenario test asserts its own report. In the PR that retires the recorder, every test that had a recorded statement or warning is converted to an explicit assertion, reviewed one node at a time, so no coverage is lost silently (AGENTS.md I-6).

## Considered options

- **A private test-only door** returning the statements. Rejected: users keep the log as their only answer to "what did auto-migrate do", and the production log stays shaped by tests.
- **Golden files per test through a fixture.** Rejected: that is the recorder again, with re-recording as the remedy.
- **`warnings` as plain strings.** Rejected: the planner's reports are to be typed (ADR-0052), and typing a public field later would be a second public change. This report ships after that type lands.
- **`connect()` returning the report.** Rejected: `connect()` is for opening a connection; its auto-migrate flags stay flags, and the report is reached through `migrate()`.

## Consequences

- `tests/test_pass_recording.py`, `tests/fixtures/pass_recording/` and the `FERRO_PASS_RECORD` workflow are to be deleted, along with the log harnesses in `test_auto_migrate.py` and the prefix readers in five other test files.
- The `RECONCILE_STATEMENT_LOG_PREFIX`, `LOCK_TIMEOUT_LOG_PREFIX` and `LABEL_PROBE_LOG_PREFIX` constants go. The debug log becomes one free-text line per statement, free to change.
- When pin (g) lands, AGENTS.md I-1 lists seven pins and its reconciliation-pass bullet names `PassReport`.
- The Auto-migrate guide page documents `PassReport`, its roles, its warning kinds and `.report` on failure.

## Amendments

- 2026-10-08: the executor's input is the shape A-0 built, not the one sketched above: `ddl.run(engine, unit, Door::Pass(subject), &[sql], on_attempt, settle)` takes the door (whose subject every statement carries) and the plain SQL, and returns `Executed { statements: [Statement { subject, sql, role }] }`. A failure carries what its unit committed (`Failed::committed`, and the same on a lock timeout): an unwrapped unit's earlier statements, nothing for a transactional one. The SQLite label probe runs through `Executed::probe_on`, recorded as `role = "probe"`. Every statement of a type is reported under the type's name, the create pass's guarded `CREATE TYPE` included.
- 2026-11-17: on Postgres the create pass runs every new table of a pass as one transaction when any of them declares row security: each CREATE TABLE with its indexes and checks, in foreign-key order, then every table's row-security flags and policies. A policy may read any table of the pass, and a failure rolls back every new table, so .report holds none of them. A pass in which no new table declares row security keeps one transaction per table (#753).
- 2026-11-17: under `migrate_updates` that one transaction is the reconciliation pass's. The create pass creates none of those tables; the plan's own ops create them, with the tables held back for a rename, after the plan's renames. A policy may so read a renamed table or one created after a rename as well (#753).
- 2026-11-18: the one transaction of the first 2026-11-17 amendment also covers a deferred foreign key. On Postgres a foreign key of a new table to a new table created later in the pass is left out of its CREATE TABLE and added after the last one. The create pass runs every new table of a pass as one transaction whenever a statement of theirs runs after the last CREATE TABLE: a deferred key, then row-security flags and policies. A pass with neither keeps one transaction per table; this replaces that amendment's last sentence (#304).
