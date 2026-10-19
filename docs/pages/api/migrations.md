---
description: "API reference for ferro.migrations, which applications, data steps, tests and an Alembic env.py import."
---

# ferro.migrations

`ferro.migrations` is what an application, a data step, a test and an Alembic `env.py` import. The guides show each in use: [Deploying migrations](../guide/schema/deploying.md) for `up` and `require_applied`, [Data steps and backfills](../guide/schema/data-steps.md) for the declarations, [Testing migrations](../guide/schema/testing.md) for the harness, [Alembic](../guide/schema/alembic.md) for the bridge. The commands are in the [CLI reference](../reference/cli.md).

```python {test=compile reason="needs a project with a migrations directory and a database URL"}
import ferro
import ferro.migrations

await ferro.connect(url)
await ferro.migrations.up()                # an app with no deploy step
await ferro.migrations.require_applied()   # a server that migrates in its deploy step
```

## Running migrations

Each call takes these parameters to say which project, database and connection it works on:

| Parameter | Meaning | Taken by |
| :--- | :--- | :--- |
| `settings=` | `None` is `FerroSettings()`, the project's config file | every call |
| `database=` | `None` is the one database configured | every call |
| `using=` | the open connection of that name; `None` is the default connection, unless `url=` is given | `up`, `require_applied`, `status`, `drift`, `baseline`, `remove_baseline` |
| `url=` | a private connection opened for the call and closed after it | the same six |

Passing both `using=` and `url=` raises `MigrationRefused`. `check` reads only the models and the migrations directory and takes no connection. A refusal is raised, never returned: a refused `up` raises `MigrationRefused` whose `.report` is the run's report, saying what it applied before it stopped.

::: ferro.migrations.up

::: ferro.migrations.require_applied

::: ferro.migrations.status

::: ferro.migrations.check

### Reports

`up()` returns a `RunReport`, `status()` a `StatusReport` and `check()` a `CheckReport`. None of the three is exported: you read them, you never build one.

::: ferro.migrations.runner.RunReport

::: ferro.migrations.report.StatusReport

::: ferro.migrations.generate.CheckReport

## Baseline and drift

::: ferro.migrations.baseline

::: ferro.migrations.remove_baseline

::: ferro.migrations.BaselineReport

::: ferro.migrations.drift

::: ferro.migrations.DriftReport

::: ferro.migrations.render_op

## Errors

A database ahead of the checkout raises `DatabaseAheadError`: `.ahead` names the migrations it has applied that the directory lacks, and `.contract_steps` each of their steps whose class is not `expand` (`["0008_drop_bio:01"]`), the steps `allow_ahead="expand-only"` refuses on. The [rolling-deploy recipe](../guide/schema/deploying.md#rolling-deploys-and-the-two-release-contract) shows it in use.

```python {test=compile reason="needs a database ahead of the checkout"}
try:
    await ferro.migrations.require_applied(allow_ahead="expand-only")
except ferro.migrations.DatabaseAheadError as ahead:
    print(ahead.contract_steps)   # ['0008_drop_bio:01']
```

::: ferro.migrations.MigrationRefused

::: ferro.migrations.PendingMigrationsError

::: ferro.migrations.DatabaseAheadError

::: ferro.migrations.AlreadyTrackedError

::: ferro.migrations.AppliedAboveBaselineError

## Data step declarations

::: ferro.migrations.atomic

::: ferro.migrations.chunked

::: ferro.migrations.irreversible

::: ferro.migrations.nothing_to_reverse

::: ferro.migrations.todo

## The test harness

`ferro.migrations.testing` is imported by a project's tests, never by its application code.

::: ferro.migrations.testing.harness

::: ferro.migrations.testing.Harness

::: ferro.migrations.testing.RoundTripResult

## The Alembic bridge

Requires the `ferro-orm[alembic]` extra. These three functions load on first use, so the calls above never import Alembic.

::: ferro.migrations.get_metadata

::: ferro.migrations.ferro_options

::: ferro.migrations.render_item
