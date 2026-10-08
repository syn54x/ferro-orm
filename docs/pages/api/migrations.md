# Migrations

`ferro.migrations` is what an application, a data step, a test and an Alembic `env.py` import. The guides show each in use: [Deploying migrations](../guide/schema/deploying.md) for `up` and `require_applied`, [Data steps and backfills](../guide/schema/data-steps.md) for the declarations, [Testing migrations](../guide/schema/testing.md) for the harness, [Alembic](../guide/schema/alembic.md) for the bridge. The commands are in the [CLI reference](../reference/cli.md).

```python
import ferro
import ferro.migrations

await ferro.connect(url)
await ferro.migrations.up()                # an app with no deploy step
await ferro.migrations.require_applied()   # a server that migrates in its deploy step
```

## Running migrations

Each call reads the project configuration (`settings=None` is `FerroSettings()`) and picks its database (`database=None` is the one configured). The calls that read a database (`up`, `require_applied`, `status`, `drift`, `baseline` and `remove_baseline`) take `using=`, the open connection to work on, or `url=`, to open a private connection for the call and close it after; with neither they work on the default connection, and with both they refuse. `check` reads only the models and the migrations directory and takes no connection. A refusal is raised, never returned: a refused `up` raises `MigrationRefused` whose `.report` is the run's report, saying what it applied before it stopped.

::: ferro.migrations.up

::: ferro.migrations.require_applied

::: ferro.migrations.status

::: ferro.migrations.check

## Baseline and drift

::: ferro.migrations.baseline.baseline

::: ferro.migrations.remove_baseline

::: ferro.migrations.BaselineReport

::: ferro.migrations.drift.drift

::: ferro.migrations.DriftReport

::: ferro.migrations.render_op

## Errors

::: ferro.migrations.MigrationRefused

::: ferro.migrations.PendingMigrationsError

::: ferro.migrations.DatabaseAheadError

## Data step declarations

::: ferro.migrations.atomic

::: ferro.migrations.steps.chunked

::: ferro.migrations.irreversible

::: ferro.migrations.nothing_to_reverse

::: ferro.migrations.todo

## The test harness

`ferro.migrations.testing` is imported by a project's tests, never by its application code.

::: ferro.migrations.testing.harness

::: ferro.migrations.testing.Harness

::: ferro.migrations.testing.RoundTripResult

## The Alembic bridge

Requires the `ferro-orm[alembic]` extra. These three load on first use, so the calls above never import Alembic.

::: ferro.migrations.alembic.get_metadata

::: ferro.migrations.alembic.ferro_options

::: ferro.migrations.alembic.render_item
