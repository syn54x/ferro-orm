# Deploying Migrations

Migrations reach a database in one of two ways: a deploy step runs `ferro migrate up` before the new code starts, or the application applies them itself at start-up. Either way the application should refuse to serve a database its migrations have not reached.

## In CI

`ferro migrate check` reads the models and the migrations directory and nothing else, so it needs no database and no credentials:

```text
$ ferro migrate check
ok: models match 0002_author_slug
```

It exits 3, naming each problem, when a model change has no migration, the snapshot chain is broken, a number is duplicated or missing, a step lacks a target dialect's rendering, or a data step still says `todo(...)`. Run it on every pull request.

### A pre-commit hook

Ferro ships no pre-commit hook repository; a local hook runs the same check:

```yaml
# .pre-commit-config.yaml
repos:
  - repo: local
    hooks:
      - id: ferro-migrate-check
        name: ferro migrate check
        entry: ferro migrate check
        language: system
        pass_filenames: false
```

## On a server

The deploy step applies every pending migration, in order, under the run lock:

```text
$ ferro migrate up
0002_author_slug  01_expand           applied (0 ms)
0002_author_slug  02_backfill_author  applied (1 ms)
0002_author_slug  03_add_constraint   applied (0 ms)
0002_author_slug  04_contract         applied (0 ms)
```

It exits 0 when it applied everything (or there was nothing to apply), 1 when it refused or a step failed; the message says why and how to go on, and the next `up` resumes at the failed step. The URL comes from `$DATABASE_URL` (or the variable the configuration's `url_env` names), or `--url`.

The application then checks at start-up that the database stands at the head of its migrations, without changing anything and without taking a lock:

```python
import ferro
import ferro.migrations


async def start() -> None:
    await ferro.connect(database_url)
    await ferro.migrations.require_applied()
```

Behind its migrations, it raises `PendingMigrationsError`:

```text
ferro.migrations.PendingMigrationsError: ferro.migrations: this database is behind its migrations.
  pending  0002_author_slug
Run `ferro migrate up` (or `await ferro.migrations.up()`) before serving.
```

`.pending` lists the migrations; `.refusals` carries anything that would stop `up` (an interrupted run, an [edited file](#edited-files-and-rerecord)). Every refusal is a `MigrationRefused`, so one `except` catches them all.

## Apps that migrate at start-up

A desktop or local-first app that ships a SQLite file has no deploy step: it applies its own migrations when it starts.

```python
import ferro
import ferro.migrations


async def start() -> None:
    await ferro.connect(database_url)
    await ferro.migrations.up()
```

`up()` takes the same run lock as `ferro migrate up`, so two instances starting together apply the migrations once. It raises instead of returning a refusal: an app must not go on serving a database the run did not reach. When a user opens a file a newer build migrated, it raises `DatabaseAheadError` (see below). A file that existed before the app had migrations is [baselined](../../howto/adopting-migrations.md#an-app-that-migrates-at-start-up) in the same start-up code.

`up()` and `require_applied()` take no target: a migration is applied whole, always to the head (ADR-0040). Both take `settings=` and `database=` (which configured database; needed when several are configured) and `using=` (which open connection; the default one otherwise).

## Rolling deploys and the two-release contract

In a rolling deploy, old code keeps running while the migration applies. A migration is applied whole, so a change that old code cannot live with must be split across two releases. Take a required `slug`:

| Release | Model | Migration | Old code during the deploy |
| :--- | :--- | :--- | :--- |
| 1 | `slug: str \| None = None` | `0008_author_slug`: one schema step, `ADD COLUMN`, nullable | runs against a database one migration ahead of it |
| 2 | `slug: str` | `0009_author_slug_required`: backfill, add-constraint, contract | release 1 code already writes `slug` |

Between the two, release 0 code meets a database that has applied a migration its checkout does not have. `require_applied()` and `up()` refuse that by default:

```text
ferro.migrations.DatabaseAheadError: ferro migrate: this database has applied 0008_author_slug, which is not in migrations/.
The directory is behind the database: check out the branch that holds it. Nothing was applied.
Pass allow_ahead=True to run beside them (a rolling deploy).
```

So the old release must start with `allow_ahead=True`:

```python
await ferro.migrations.require_applied(allow_ahead=True)
```

A deployment that runs a fixed old version beside a new schema says so in code; nothing in the database or the configuration turns the check off. The one-migration shape (expand, backfill and contract together) is for deploys where writers stop, or where every writer already supplies the value. If a row slips in behind a backfill anyway, the contract fails and names [the recovery](data-steps.md#when-a-late-row-arrives).

## Locks and timeouts

Two different waits apply:

- **The run lock.** One run per database at a time. A second `up`, `down`, `baseline` or `rerecord` waits for it, printing at once:

    ```text
    Another ferro migration run holds the lock on this database; waiting (--lock-timeout to bound it).
    ```

    `--lock-timeout` bounds the wait: `30s` (the default), `500ms`, `1m`, a number of seconds, or `0` to refuse at once. The lock dies with the process that holds it, never by a timeout, so a crashed run never leaves it stuck (ADR-0029).
- **The DDL lock timeout.** On Postgres, each statement waits for its table lock under the database's [`ddl_lock_timeout`](../../reference/configuration.md#ddl_lock_timeout) (default `5s`), so one statement queued behind a long query never queues every other query behind itself. A statement that times out is retried with its step from the first statement, each attempt a progress line:

    ```text
    0003_add_slug  01_expand  waiting for a lock on "author" (attempt 1 of 10, retry in 1s)
    0003_add_slug  01_expand  applied (2214 ms)
    ```

    It does not apply to data steps.

## Where a database stands

`ferro migrate status` reads the tracking table without a lock and changes nothing:

```text
$ ferro migrate status --steps
default (sqlite) · main._ferro_migrations

0001_create_author  applied
  01_schema.up.sqlite.sql          applied
0002_author_slug    partial, 1 of 4 steps
  01_expand.up.sqlite.sql          applied
  02_backfill_author.py            pending
  03_add_constraint.up.sqlite.sql  pending
  04_contract.up.sqlite.sql        pending  [data-dependent]
```

| Exit code | Meaning |
| :--- | :--- |
| 0 | Everything is applied. |
| 3 | Something is pending: run `up`. |
| 4 | Something needs a person: a failed or interrupted step, an edited file, a migration applied that the directory does not have. |

`--json` prints the same report as a JSON document; `await ferro.migrations.status()` returns it.

## Edited files and `rerecord`

Every step record holds the checksum of the file this database ran (the up file, in this database's dialect). What happens when the file changes depends on the step:

| The step on this database | An edited file |
| :--- | :--- |
| Finished | Refused by `up` and `require_applied()`; `status` reports it (`applied (different checksum)`, with both checksums) and exits 4. Restore the file, or accept a deliberate edit with `ferro migrate rerecord <migration>:<step>`. |
| Started, not finished: an atomic data step, a transactional DDL step or a no-transaction step | Accepted: it committed nothing (or is safe to re-run), so the next `up` re-records the checksum, says so, and runs the edited file. |
| A chunked data step whose batches already committed rows | Refused: choose `rerecord <migration>:<step> --continue` (keep those rows, resume from the cursor) or `--restart` (run every row again from the first). |
| Never run | Free to edit. |
| `ir.json`, the schema snapshot | Never re-recorded: later migrations and historical models are built from it. Restore the file. |

```text
$ ferro migrate up
ferro migrate: 0001_create_author/01_schema.up.sqlite.sql was edited after it was applied to this database.
  applied   sha384:3dbe7e62…  (2026-10-07 15:55 UTC)
  on disk   sha384:db7cd673…
An applied step is never run again. Restore the file, or accept a deliberate edit with
`ferro migrate rerecord 0001:01`. Nothing was applied.

$ ferro migrate rerecord 0001:01
re-recorded 0001_create_author/01_schema.up.sqlite.sql (sha384:3dbe7e62… → sha384:db7cd673…); nothing was run
```

`rerecord` changes the record and nothing else. `--continue` is offered only while the edited query pages over the same order-key columns as the cursor; otherwise the cursor is no position in the new query and only `--restart` is accepted. A fixed `down` file or another dialect's rendering is not an edit to this database's record.

## Several databases

A project configured with several databases runs each command against one of them, named with `--database`:

```bash
ferro migrate up --database billing
```

Each database has its own migrations directory and its own lineage. In code, pass `database="billing"` to `up()`, `require_applied()`, `status()` and `check()`. The three that read the database (`up()`, `require_applied()`, `status()`) also take `using=`, naming the open connection that reaches it; `check()` reads only the models and the migrations directory, so it takes no connection. See [Multiple Databases](../../howto/multiple-databases.md).

## See Also

- [Migrations](migrations.md) — the loop and going back with `down`
- [Data steps and backfills](data-steps.md) — what a late row does to a contract
- [CLI reference](../../reference/cli.md) — every verb, flag and exit code
- [Migrations API reference](../../api/migrations.md) — `up`, `require_applied`, `status`, `check`
