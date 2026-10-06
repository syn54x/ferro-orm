# `connect()` never runs migration files, and a tracked database refuses auto-migrate

Ferro has two ways to change a schema from inside an application, and they are spelled so that neither can be mistaken for the other:

```python
# the built-in door: straight from the models, no files
await ferro.connect(url, auto_migrate=True)

# the migration door: the files, through the same functions the CLI calls
await ferro.connect(url)
await ferro.migrations.up()                # `ferro migrate up`
# or, for a server whose deploy step runs the CLI:
await ferro.migrations.require_applied()   # raises PendingMigrationsError
```

A desktop app whose user's SQLite file is at `0004` while the new version carries `0007` calls `up()` at start-up; there is no deploy step where a CLI could run. A server that migrates in its deploy step calls `require_applied()` and refuses to serve when that step was skipped.

**`connect()` gains no migration parameter.** A plain `connect(url)` reads no config, reads no directory and runs no extra query. `auto_migrate`, `migrate_updates` and `migrate_destructive` stay where they are and mean what they meant.

**The two doors are exclusive per database, decided by what the database holds.** Any auto-migrate flag on a database that carries a tracking table raises before any DDL:

```
ferro.connect: this database is managed by ferro migrations
  (tracking table "public"."_ferro_migrations").
auto_migrate, migrate_updates and migrate_destructive do not run on a database that has
migrations: what they changed would be drift, and the next migration would fail on it.
Remove the flag and generate the change with `ferro migrate new`. Nothing was changed.
```

This covers plain `auto_migrate=True` too: the create pass would create a new model's table, and the migration meant to create it would then fail. A database with no tracking table is unaffected, so a test suite that builds a throwaway database with `auto_migrate=True` keeps working in a project that has migrations.

The guard reads two things. It constructs `FerroSettings()` (ADR-0039) and looks in the connection's current schema and in every configured `tracking_schema`. It also asks the database: the tracking table's format table records the schema it governs (the connection's current schema when the table was created), and `connect()` finds every format table through the catalog and refuses when one governs the schema it is about to change. The second read is the one that holds in an image built without its config file. Both happen only when an auto-migrate flag is passed.

**The auto-migrate passes take the run lock.** The create pass and the reconciliation pass take the same lock a run takes (ADR-0029), then run the guard, then do their work. Two processes booting together with `migrate_updates=True` no longer collide on one `ALTER`, and the guard cannot race a `baseline`. On Postgres this means auto-migrate is refused behind a transaction-mode pooler, with ADR-0029's text.

For both doors to exclude each other the lock has to be the same lock wherever the tracking table sits, so the Postgres key is derived from the governed schema, not from the tracking table's own schema. An in-memory SQLite database has no second process and takes no lock.

**The migration calls.** `up()`, `require_applied()`, `status()` and `baseline()` are public in `ferro.migrations`. `down` and `rerecord` remain CLI verbs only: they are operator decisions with prompts. Each call takes `settings=` (default `FerroSettings()`), `database=` when several are configured, `using=` for the connection (the default connection otherwise) and, where it takes the run lock, `lock_timeout=`. There is no `directory=` or `tracking_schema=` argument; with no config found the call refuses and names where it searched. The connection-to-database binding ADR-0036 asked for is those two arguments, written by the developer.

- `up()` is a run: it takes the run lock and waits by default.
- `require_applied()` takes no lock. It raises `PendingMigrationsError` listing the pending migrations, and the tracking table's own refusals for an interrupted run or an edited file (ADR-0030).
- `status()` returns what `ferro migrate status --json` prints.
- `baseline()` keeps ADR-0031 whole: it records only after the drift check passes. A local-first app that shipped on `auto_migrate=True` adopts migrations on its users' files with `status()`, `baseline()` when the file is unadopted, then `up()`.

Amended by ADR-0045: `drift()` and `check()` join the public calls, each returning the report its CLI verb prints and raising nothing; `raise_for_problems()` on the report raises carrying it. The targets and the promptless revert this ADR keeps off the application API live in `ferro.migrations.testing`.

**A database ahead of the code is refused by default.** When the tracking table holds records for migrations the directory does not have, `up()` and `require_applied()` raise `DatabaseAheadError` naming them. `allow_ahead=True` on either call turns that off.

## Considered options

- **`connect(url, migrate=True)` or `migrations="apply"`.** Rejected: three `connect()` parameters would contain "migrate", two meaning "from the models" and one meaning "run the files", with nothing in the names to tell them apart. The door's own arguments would also have to land on `connect()`.
- **One `schema=` parameter holding the whole ladder** (`"create"`, `"update"`, `"destructive"`, `"migrations"`, `"require-applied"`). Rejected: it deprecates three shipped flags to gain one-line start-up, and still needs somewhere to put the migration door's arguments.
- **A warn mode.** Rejected: a warning followed by `column does not exist` is degrading silently.
- **A check on every `connect()`.** Rejected: every start-up would search for config and query the tracking table, in projects that have no migrations.
- **Calling the refusal `check`.** Rejected: `ferro migrate check` is the offline gate (models against the head snapshot). Behind-or-not is `status` on the CLI and `require_applied()` in Python.
- **A ladder: auto-migrate beside migrations, its changes reported as drift.** Rejected: the next generated migration fails with "already exists" on the database that was changed and succeeds everywhere else.
- **Exclusive per project** (refuse auto-migrate once config declares migrations). Rejected: it breaks throwaway test databases, and it depends on finding the config.
- **A guard that reads only config.** Rejected: it fails open in exactly the build that did not ship the file.
- **Searching the catalog for any `_ferro_migrations`.** Rejected: an unrelated application auto-migrating in its own schema of the same Postgres database would be refused because a neighbour is tracked.
- **Leaving auto-migrate lock-free.** Rejected: the concurrent-start race is real today, and the guard would have a check-then-act gap.
- **Ahead always passes, or always raises.** Rejected: the first lets a downgraded desktop app write into a newer schema; the second forbids the rolling deploy that expand/contract depends on.
- **`up`, `require_applied` and `status` only.** Rejected: a shipped local-first app could never adopt migrations, having no CLI moment on its users' machines.

## Consequences

- ADR-0029 is amended: the auto-migrate passes take the run lock, and the Postgres key is derived from the governed schema.
- ADR-0036 is amended: the run-lock key no longer follows `tracking_schema`.
- The tracking table's format table gains the governed schema.
- A project running `auto_migrate=True` through a transaction-mode pooler is refused at start-up after this ships.
- A local-first project keeps its config and migrations inside the package so both ship in the wheel.
