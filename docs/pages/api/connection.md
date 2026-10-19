---
description: "API reference for connect(), sessions and the model registry."
---

# connect, Session and the registry

`connect()` registers a connection pool, optionally under a name, and `engines.session()` opens a session on one; every model call runs inside a session. `reset_engine()` closes every connection. See the [Connections and databases guide](../guide/connections.md).

## Connecting

::: ferro.connect

::: ferro.PoolConfig

::: ferro.set_default_connection

::: ferro.reset_engine

::: ferro.version

## Sessions

::: ferro.engines

::: ferro.session.EngineManager

::: ferro.Session

::: ferro.current_session

## Registry

::: ferro.create_tables

::: ferro.migrate

::: ferro.clear_registry

::: ferro.ensure_resolved_modelset

::: ferro.evict_instance

## Auto-migrate reports

`ferro.migrate()` and `ferro.create_tables()` return a `PassReport`, built from the statements the database actually ran. A pass that fails partway raises its usual error with `.report` set to what committed before the failure:

```python {test=compile reason="needs a database one column behind its models"}
report = await ferro.migrate(updates=True)
[(s.subject, s.sql) for s in report.statements if s.role == "schema"]
# [('author', 'ALTER TABLE "author" ADD COLUMN "slug" TEXT')]
[(w.kind, str(w)) for w in report.warnings]
# []
```

Each `ExecutedStatement` names its table (`subject`) and its `role`; each warning is a typed `Report`.

::: ferro.PassReport

::: ferro.ExecutedStatement

::: ferro.Report

::: ferro.pass_report.Subject

## Project settings

`FerroSettings()` reads the project's one config file (`ferro.toml`, or `[tool.ferro]` in `pyproject.toml`); every `ferro.migrations` call takes `settings=None` to mean exactly that. No file is not an error until a database is asked for; a malformed or contradictory configuration, or a database asked of none, raises [`SettingsError`](exceptions.md#ferro.SettingsError), whose message names the fix.

::: ferro.FerroSettings

::: ferro.DatabaseSettings
