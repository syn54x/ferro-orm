# Connection & Registry

Functions for managing database connections and the global model registry. `connect()` registers a (optionally named) connection pool. `reset_engine()` tears everything down. The registry helpers control schema creation and the identity map. Sessionized routing is exposed via `ferro.engines.session(name)` / `ferro.Session`. See the [Connections & Databases guide](../guide/connections.md).

::: ferro.Session

::: ferro.engines

::: ferro.current_session

::: ferro.connect

::: ferro.PoolConfig

::: ferro.set_default_connection

::: ferro.reset_engine

::: ferro.create_tables

::: ferro.migrate

::: ferro.clear_registry

::: ferro.ensure_resolved_modelset

::: ferro.evict_instance

::: ferro.version

## What an auto-migrate pass did

`ferro.migrate()` and `ferro.create_tables()` return a `PassReport`, built from the statements the database actually ran. A pass that fails partway raises its usual error with `.report` set to what committed before the failure.

```python
report = await ferro.migrate(updates=True)
[(s.subject, s.sql) for s in report.statements if s.role == "schema"]
# [('author', 'ALTER TABLE "author" ADD COLUMN "slug" TEXT')]
[(w.kind, str(w)) for w in report.warnings]
# []
```

::: ferro.PassReport

::: ferro.ExecutedStatement

::: ferro.Report

::: ferro.pass_report.Subject

## Project settings

`FerroSettings()` reads the project's one config file (`ferro.toml`, or `[tool.ferro]` in `pyproject.toml`); every `ferro.migrations` call takes `settings=None` to mean exactly that. No file is not an error until a database is asked for; a malformed or contradictory configuration, or a database asked of none, raises [`SettingsError`](exceptions.md#ferro.SettingsError), whose message names the fix.

::: ferro.FerroSettings

::: ferro.DatabaseSettings
