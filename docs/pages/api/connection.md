# Connection & Registry

Functions for managing database connections and the global model registry. `connect()` registers a (optionally named) connection pool; `reset_engine()` tears everything down; the registry helpers control schema creation and the identity map. Sessionized routing is exposed via `ferro.engines.session(name)` / `ferro.Session`. See the [Connections & Databases guide](../guide/connections.md).

::: ferro.connect

::: ferro.PoolConfig

::: ferro.set_default_connection

::: ferro.reset_engine

::: ferro.create_tables

::: ferro.migrate

::: ferro.clear_registry

::: ferro.evict_instance

::: ferro.version
