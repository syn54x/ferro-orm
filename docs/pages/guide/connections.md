# Connections & Databases

Ferro talks to SQLite and PostgreSQL through connection pools managed by the Rust core (SQLx). Connect once at application startup with `await ferro.connect(url, ...)` before performing any database operation.

## Connecting

### SQLite

```python
import ferro

# File database — `mode=rwc` creates the file if it doesn't exist (recommended)
await ferro.connect("sqlite:app.db?mode=rwc")

# In-memory database (great for tests)
await ferro.connect("sqlite::memory:")
```

Modes: `rwc` (read/write/create), `rw` (read/write, file must exist), `ro` (read-only).

### PostgreSQL

```python
import ferro

await ferro.connect("postgresql://user:password@localhost:5432/dbname")

# Require TLS
await ferro.connect("postgresql://user:password@localhost:5432/dbname?sslmode=require")
```

Load credentials from the environment rather than hard-coding them, and percent-encode reserved characters in passwords (`%24` for `$`, etc.) if you assemble URLs yourself.

### Supabase

Supabase hosts PostgreSQL behind TLS; Ferro's driver stack ships with Rustls and the webpki CA bundle, so published wheels connect out of the box. Copy the exact URI from **Project Settings → Database** in the Supabase dashboard (pooler hostnames look like `*.pooler.supabase.com`, and the username may include the project ref), then ensure TLS is requested:

```python
import os

import ferro

url = os.environ["DATABASE_URL"]
if "sslmode=" not in url:
    sep = "&" if "?" in url else "?"
    url = f"{url}{sep}sslmode=require"

await ferro.connect(url)
```

Keep service-role credentials server-side, and never make an elevated connection the default in user-facing runtimes.

## Connection Options

`connect()` accepts:

| Parameter | Default | Description |
| :--- | :--- | :--- |
| `url` | required | Database connection string. |
| `auto_migrate` | `False` | Create tables for all registered models; existing tables are left untouched. |
| `name` | `None` | Connection name. Omitted connections register as `"default"`. |
| `default` | `False` | Make this named connection the default for unqualified operations. |
| `pool` | `None` | A [`PoolConfig`](#connection-pooling); defaults apply when omitted. |
| `identity_map` | `True` | Keep a per-connection [identity map](../concepts/identity-map.md) so one primary key maps to one Python instance. `False` trades the `a is b` guarantee for lower memory use. |
| `migrate_updates` | `False` | Also `ALTER` existing tables to match the models (implies `auto_migrate`). See [Schema Migrations](migrations.md#applying-column-changes-with-migrate_updates). *Added in 0.11.0.* |
| `migrate_destructive` | `False` | Also drop live columns removed from the models (implies `migrate_updates`). See [Schema Migrations](migrations.md#destructive-drops-with-migrate_destructive). *Added in 0.11.0.* |

## Connection Pooling

Size each connection's pool with `PoolConfig`:

```python
import ferro

await ferro.connect(
    "postgresql://localhost/app",
    pool=ferro.PoolConfig(max_connections=10, min_connections=1),
)
```

`max_connections` defaults to 5, `min_connections` to 0, and `min_connections` may not exceed `max_connections`. For web applications, connect once at startup and let the long-lived pool serve all requests.

## Named Connections

Ferro can hold multiple live pools in one process — separate databases, roles, or pool sizes:

```python
--8<-- "docs/examples/multiple_databases.py:connect"
```

Route individual operations with `Model.using("name")`, which exposes the full ORM surface (`create`, `get`, `where`, `bulk_create`, `get_or_create`, ...) bound to that connection:

```python
--8<-- "docs/examples/multiple_databases.py:routing"
```

Raw SQL routes the same way via `using=`: `await ferro.execute("...", using="analytics")`. Transactions opened with `transaction(using="analytics")` pin everything inside to that connection — see [Transactions](transactions.md#using-named-connections).

!!! warning "Connection names are code, not input"
    Choose `using` values from constants or trusted server-side logic. Never bind connection names from request parameters, headers, or other untrusted input.

Ferro does not provide automatic router policies, read/write splitting, distributed transactions, or cross-connection joins — route each operation explicitly. See [Multiple Databases](../howto/multiple-databases.md) for fuller patterns.

## Sessions (recommended)

Session-scoped routing is now the preferred runtime model:

```python
import ferro

async with ferro.engines.session("analytics") as s:
    rows = await s.query(User).where(lambda t: t.active == True).all()  # noqa: E712
```

Inside an active session context, convenience APIs (`User.all()`, `User.where(...)`, `ferro.execute(...)`) automatically bind to that session's connection.

Legacy implicit default-connection routing (calling unqualified operations outside a session) is still temporarily supported for compatibility, but now emits a deprecation warning and is on the `v0.13.0` removal track.

## The Default Connection

Unqualified operations (`User.all()`, top-level `execute(...)`) use the default connection. It is established three ways:

1. `connect(url)` without a `name` registers as `"default"`.
2. `connect(url, name="app", default=True)` makes a named connection the default.
3. `ferro.set_default_connection("app")` switches the default at runtime.

```python
import ferro

await ferro.connect("sqlite:app.db?mode=rwc", name="app", default=True)
await ferro.connect("sqlite:analytics.db?mode=rwc", name="analytics")

ferro.set_default_connection("analytics")  # unqualified ops now hit analytics
```

## Creating Tables Manually

To control table creation yourself instead of `auto_migrate=True`, make sure your models are imported (importing registers them), then call `create_tables()`:

```python
import ferro


async def init() -> None:
    await ferro.connect("sqlite::memory:")

    from myapp.models import Post, User  # noqa: F401 — importing registers models

    await ferro.create_tables()                  # default connection
    await ferro.create_tables(using="analytics")  # a named connection
```

`create_tables()` creates missing tables (including many-to-many join tables) and never modifies existing ones.

## Postgres Schema Isolation

Append the Ferro-specific `ferro_search_path` URL parameter to pin a connection to a PostgreSQL schema. Ferro strips the parameter from the URL before handing it to the driver and applies `SET search_path` on every pooled connection:

```python
import ferro

await ferro.connect(
    "postgresql://user:password@localhost:5432/app?ferro_search_path=tenant_a"
)
```

The value must be a single identifier of ASCII letters, digits, and underscores — anything else raises `ValueError`. This is handy for schema-per-tenant setups and for isolating test runs against one physical database.

## Resetting the Engine

`ferro.reset_engine()` tears down all pools and connection state — primarily for test suites that reconnect with a fresh database per test:

```python
import ferro

ferro.reset_engine()
await ferro.connect("sqlite::memory:", auto_migrate=True)
```

See [Testing](../howto/testing.md) for a ready-made pytest fixture.

!!! note "No `disconnect()` yet"
    An explicit `disconnect()` is **not yet implemented** — pools are cleaned up on process exit. See the [Roadmap](../roadmap.md).

## See Also

- [Schema Migrations](migrations.md) — `auto_migrate` flags and Alembic
- [Transactions](transactions.md) — connection affinity
- [Multiple Databases](../howto/multiple-databases.md) — multi-connection patterns
- [Testing](../howto/testing.md) — test database setup
- [Database Backends](../concepts/backends.md) — SQLite vs PostgreSQL specifics
