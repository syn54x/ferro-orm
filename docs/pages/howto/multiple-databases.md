# Multiple Databases

One process can hold several named connections — a primary application database, a read replica, an analytics warehouse — each with its own pool. The common case still works untouched: `await connect(url)` registers and selects `"default"`. Named connections make everything else explicit.

## Registering Connections

Give each connection a `name`; mark one as `default` for unqualified operations:

```python
--8<-- "docs/examples/multiple_databases.py:connect"
```

Each `connect()` call creates an independent pool, configurable per connection with `pool=PoolConfig(max_connections=...)`. You can change which named connection is the default later with [`set_default_connection(name)`](../api/connection.md).

## Routing Queries

Everything runs on the default connection unless routed. `Model.using(name)` returns a handle exposing the same API (`create`, `all`, `select`, `where`, `get`, `get_or_none`, `bulk_create`, `get_or_create`, `update_or_create`) pinned to the named connection:

```python
--8<-- "docs/examples/multiple_databases.py:routing"
```

Routing is per-call: `using()` doesn't change any global state, so two coroutines can talk to different databases concurrently without interfering.

## Transactions on Named Connections

`transaction(using=...)` pins a transaction to one named connection:

```python
--8<-- "docs/examples/multiple_databases.py:transaction"
```

Everything inside the block — including raw SQL via `execute()` / `fetch_all()` — inherits that connection. Nested `transaction()` blocks inherit it too. Ferro does not support distributed transactions: one `transaction()` spans exactly one named connection, so writes to two databases are never atomic together.

## Per-Connection Schema Setup

Schema creation targets one connection at a time:

- `connect(url, name=..., auto_migrate=True)` runs auto-migration **on that connection** as part of connecting — each database gets tables for all registered models, as the example above shows.
- [`create_tables(using=...)`](../api/connection.md) creates tables explicitly on a named connection after the fact:

```python
from ferro import create_tables

await create_tables(using="analytics")
```

Don't run schema creation concurrently through multiple names that point at the same physical database. For production schema changes, prefer one migration-capable connection and the [Alembic bridge](../guide/migrations.md).

## Practical Notes

- **Typical roles.** A read replica for expensive list endpoints (`User.using("replica")`), an analytics database that receives event rows, or a separate service database owned by another team.
- **Keep credentials server-side.** Elevated service-role connections belong in configuration, not source control — and never make a service-role connection the default in a user-facing runtime.
- **Never route from untrusted input.** Don't pick the `using` name from request data.
- **Pools isolate roles, not request context.** A named connection isolates credentials and pooling; it does not provide per-request RLS/JWT context inside one shared pool. Objects loaded through an elevated connection can contain elevated data — filter before returning them to users.
- **No automatic routing.** Read/write splitting, cross-connection joins, and two-phase commit are not features; routing is always an explicit `using` call.

## See Also

- [Connections & Databases guide](../guide/connections.md) — URLs, pools, and connection lifecycle
- [Transactions guide](../guide/transactions.md) — transaction semantics and nesting
- [Connection & Registry API](../api/connection.md) — `connect`, `PoolConfig`, `set_default_connection`, `create_tables`
