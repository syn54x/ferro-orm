---
description: "Connect Ferro to SQLite or PostgreSQL, configure the connection pool and run every ORM call inside a session."
---

# Connections

Ferro reaches SQLite and PostgreSQL through connection pools: you connect once at startup, then run every ORM call inside a session.

Connect to an in-memory SQLite database, open a session and run a query:

```python {test=run prelude=connections}
from ferro import connect, engines

await connect("sqlite::memory:", auto_migrate=True)

async with engines.session():
    await Author.create(name="Ada Lovelace", email="ada@example.com")
    print(await Author.select().count())
```

It prints `1`. `auto_migrate=True` created the `author` table as it connected, which suits development and tests.

The examples on this page use the quickstart's models:

=== "Assignment"

    ```python
    --8<-- "docs/examples/quickstart.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/quickstart_annotated.py:models"
    ```

## Connecting

`connect(url)` opens a pool for the database the URL names and registers it as the default connection.

### SQLite

Pass a file path, or `:memory:` for a database that lives as long as the process:

```python {test=compile reason="creates app.db in the working directory"}
from ferro import connect

await connect("sqlite:app.db?mode=rwc")
```

`mode=rwc` reads, writes and creates the file if it does not exist. `mode=rw` needs the file to exist, and `mode=ro` opens it read-only. `sqlite::memory:` gives an in-memory database, for tests.

### PostgreSQL

Pass a `postgresql://` URL; add `sslmode=require` to require TLS:

```python {test=compile reason="needs a PostgreSQL server"}
from ferro import connect

await connect("postgresql://user:password@localhost:5432/app?sslmode=require")
```

The pool connects over TLS. Load credentials from the environment rather than writing them into code, and percent-encode reserved characters in a password (`%24` for `$`) if you build the URL yourself.

### Supabase

Supabase hosts PostgreSQL behind TLS, and Ferro's published wheels ship with Rustls and the webpki CA bundle, so they connect without extra setup. Copy the URI from **Project Settings → Database** in the Supabase dashboard (pooler hostnames look like `*.pooler.supabase.com`, and the username may include the project ref), then make sure it requests TLS:

```python {test=compile reason="needs a Supabase DATABASE_URL"}
import os

from ferro import connect

url = os.environ["DATABASE_URL"]
if "sslmode=" not in url:
    separator = "&" if "?" in url else "?"
    url = f"{url}{separator}sslmode=require"

await connect(url)
```

The connection now refuses to fall back to plain text. Keep service-role credentials on the server, and never make an elevated connection the default in a user-facing runtime.

## Sessions

Every ORM and raw SQL call runs in a session. A session is one unit of work: it picks the connection the calls use and keeps an [identity map](../concepts/identity-map.md), so loading the same row twice gives you the same Python object. Open one with `async with engines.session():`:

```python {test=run prelude=connections}
from ferro import connect, engines

await connect("sqlite::memory:", auto_migrate=True)

async with engines.session():
    ada = await Author.create(name="Ada Lovelace", email="ada@example.com")
    same = await Author.where(lambda author: author.email == "ada@example.com").first()
    print(same is ada)
```

It prints `True`: inside the session, one row maps to one instance. With no name, `engines.session()` opens a session on the [default connection](#the-default-connection).

A call outside any session has no connection to run on, and raises:

```python {test=run prelude=connections}
from ferro import connect

await connect("sqlite::memory:", auto_migrate=True)

try:
    await Author.select().count()
except RuntimeError as error:
    print(error)
else:
    raise AssertionError("expected RuntimeError")
```

It prints:

```text
No database route for this operation. Open a session (`async with ferro.engines.session(...)`) or pass `using=`/`session=` explicitly. Implicit default-connection routing was removed in v0.14 (deprecated since v0.12); see the upgrade guide.
```

The only other route is an explicit one: `Author.using("name")`, or `using=` / `session=` on a call. Explicit routes are covered under [Named connections](#named-connections).

### A session per request

For a short unit of work, such as an HTTP handler, a script or a test, open a session for its length:

```python {test=run prelude=connections}
from ferro import connect, engines

await connect("sqlite::memory:", auto_migrate=True)


async def list_authors() -> list[str]:
    async with engines.session():
        authors = await Author.select().order_by(lambda author: author.name).all()
        return [author.name for author in authors]


async with engines.session():
    await Author.create(name="Grace Hopper", email="grace@example.com")
    await Author.create(name="Ada Lovelace", email="ada@example.com")

print(await list_authors())
```

It prints `['Ada Lovelace', 'Grace Hopper']`. Each request gets its own session, and with it its own identity map.

### A session for the app's lifetime

When startup and shutdown run in different asyncio tasks, as with Textual's `on_mount` and `on_unmount`, open a session handle at startup and close it at shutdown:

```python {test=run prelude=connections}
from ferro import connect, engines


class App:
    async def on_mount(self) -> None:
        await connect("sqlite::memory:", auto_migrate=True)
        self.session = engines.session()
        await self.session.__aenter__()

    async def on_unmount(self) -> None:
        await self.session.close()


app = App()
await app.on_mount()
await Author.create(name="Ada Lovelace", email="ada@example.com")
print(await Author.select().count())
await app.on_unmount()
```

It prints `1`: calls between `on_mount` and `on_unmount` run in the app's session. `Session.close()` works from any asyncio task, and closing twice does nothing. When the closing task may not see the session, pass it explicitly with `session=`.

Rules for closing a session:

- `close()` raises `RuntimeError` while a `transaction()` in the session is still open. Leave every `transaction()` block first.
- A call on a closed session, ambient or passed as `session=`, raises `RuntimeError`. It never falls back to another route.
- If you close a session from another task, the task that opened it may still hold it as its ambient session until it opens a new one. Calls there raise `RuntimeError` saying the session is closed.
- With nested sessions, close the inner ones before the outer one.

`reset_engine()` tears down every connection. It exists for test isolation, not for closing one session.

The [`Session`](../api/connection.md#ferro.Session) and [`engines`](../api/connection.md#ferro.engines) reference entries list every argument, including `settings=` for [row-level security](../concepts/row-level-security.md#session-settings).

!!! note "Changed in 0.13"
    Calls outside a session used to run on the default connection; they now raise `RuntimeError`. The [upgrade guide](../upgrading.md#connection-routing-requires-a-session) shows how to move old call sites into sessions.

## Connection options

Beyond the URL, `connect()` takes the options you will reach for most:

- `auto_migrate=True` creates the missing tables for every registered model; existing tables are left alone. `migrate_updates` and `migrate_destructive` also change existing tables; see [Auto-migrate](schema/auto-migrate.md). The three flags run under the same run lock as `ferro migrate up`, are refused on a database that migrations govern, and wait for locks under the project's `lock_timeout` and `ddl_lock_timeout` ([Concurrent passes and the run lock](schema/auto-migrate.md#concurrent-passes-and-the-run-lock)).
- `name="analytics"` registers a [named connection](#named-connections). Without a name, the connection registers as `"default"`.
- `identity_map=False` turns off the identity map for sessions on this connection, trading the `a is b` guarantee for lower memory use.
- `pool=PoolConfig(...)` sizes the pool, below.

The [`connect`](../api/connection.md#ferro.connect) reference lists every option and its default.

### Pool size

Size a connection's pool with `PoolConfig`:

```python {test=compile reason="needs a PostgreSQL server"}
from ferro import PoolConfig, connect

await connect(
    "postgresql://localhost/app",
    pool=PoolConfig(max_connections=10, min_connections=1),
)
```

The pool holds up to ten connections and keeps one open. `max_connections` defaults to 5 and `min_connections` to 0, and `min_connections` may not exceed `max_connections`. In a web application, connect once at startup and let the pool serve every request. `PoolConfig(settings_delivery=...)` chooses how sessions send their [row-level security settings](../concepts/row-level-security.md#connection-delivery); the [`PoolConfig`](../api/connection.md#ferro.PoolConfig) reference covers every field.

## Named connections

One process can hold several pools at once: separate databases, roles or pool sizes. Name each one with `name=`, and route to it with a named session or `Model.using(name)`:

```python {test=run prelude=connections}
from ferro import connect, engines

await connect("sqlite::memory:", name="app", default=True, auto_migrate=True)
await connect("sqlite::memory:", name="archive", auto_migrate=True)

async with engines.session():  # the default connection, "app"
    await Author.create(name="Ada Lovelace", email="ada@example.com")

async with engines.session("archive"):
    await Author.create(name="Grace Hopper", email="grace@example.com")

print([author.name for author in await Author.using("archive").all()])
```

It prints `['Grace Hopper']`: each connection is its own database. `Author.using("archive")` routes one call and needs no session.

Raw SQL takes `using=` too (`await execute("...", using="archive")`), and `transaction(using="archive")` pins a whole transaction to one connection. [How to use several databases](../howto/multiple-databases.md) walks through routing, transactions and table creation across two databases.

!!! warning "Connection names are code, not input"
    Choose connection names from constants or trusted server-side logic. Never take one from a request parameter, a header or other untrusted input.

Ferro has no automatic routing: no read/write splitting, distributed transactions or cross-connection joins. Each call goes where you route it.

### The default connection

`engines.session()` with no name opens a session on the default connection. The default is set three ways:

1. `connect(url)` without a `name` registers it as `"default"`.
2. `connect(url, name="app", default=True)` makes a named connection the default.
3. `set_default_connection("archive")` switches the default at runtime.

```python {test=run prelude=connections}
from ferro import connect, engines, set_default_connection

await connect("sqlite::memory:", name="app", default=True, auto_migrate=True)
await connect("sqlite::memory:", name="archive", auto_migrate=True)

set_default_connection("archive")

async with engines.session():
    await Author.create(name="Ada Lovelace", email="ada@example.com")

print(len(await Author.using("archive").all()), len(await Author.using("app").all()))
```

It prints `1 0`: after the switch, a session with no name opens on `archive`.

## Creating tables manually

To create tables yourself instead of with `auto_migrate=True`, import your models (importing registers them), then call `create_tables()`:

```python {test=run prelude=connections}
from ferro import connect, create_tables

await connect("sqlite::memory:")

report = await create_tables()
for statement in report.statements:
    print(statement.sql)
```

On SQLite it prints:

```text
CREATE TABLE IF NOT EXISTS "author" ( "email" varchar NOT NULL, "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "name" varchar NOT NULL )
CREATE UNIQUE INDEX IF NOT EXISTS "uq_author_email" ON "author" ("email")
CREATE TABLE IF NOT EXISTS "post" ( "author_id" integer NOT NULL, "body" varchar NOT NULL, "created_at" DATETIME NOT NULL, "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "published" integer NOT NULL, "title" varchar NOT NULL, CONSTRAINT "fk_post_author_id_author" FOREIGN KEY ("author_id") REFERENCES "author" ("id") ON DELETE CASCADE )
```

`create_tables()` returns a [`PassReport`](../api/connection.md#ferro.PassReport) of the statements it ran, here two tables and the unique index on `email`. It creates missing tables, many-to-many join tables included, and never changes existing ones. `create_tables(using="archive")` targets a named connection.

## Postgres schema isolation

Add the `ferro_search_path` parameter to a PostgreSQL URL to pin the connection to one schema:

```python {test=compile reason="needs a PostgreSQL server"}
from ferro import connect

await connect("postgresql://user:password@localhost:5432/app?ferro_search_path=tenant_a")
```

Ferro removes the parameter before passing the URL to the driver and runs `SET search_path` on every pooled connection, so unqualified table names resolve in `tenant_a`. The value must be one identifier of ASCII letters, digits and underscores; anything else raises `ValueError`. Use it for schema-per-tenant setups, or to isolate test runs that share one database.

## Resetting the engine

`reset_engine()` tears down every pool and all connection state, so a test suite can reconnect to a fresh database for each test:

```python {test=run prelude=connections}
from ferro import connect, reset_engine

reset_engine()
await connect("sqlite::memory:", auto_migrate=True)
```

The new connection registers as the default again. Pools also close when the process exits. [Testing](../howto/testing.md) has a ready-made pytest fixture.

## Logging

The `ferro.sql` logger is the statement log: one `DEBUG` record for each statement Ferro sends, written as the statement finishes. Turn it on with your usual logging setup:

```python {test=run prelude=connections}
import logging

logging.basicConfig(format="%(name)s %(levelname)s %(message)s")
logging.getLogger("ferro.sql").setLevel(logging.DEBUG)
```

A schema change now prints each statement it runs, with its parameters and how long it took:

```text
ferro.sql DEBUG CREATE TABLE IF NOT EXISTS "post" (...) params=() 1.2ms
ferro.sql DEBUG ALTER TABLE "author" ADD COLUMN "bio" varchar params=() 0.8ms
```

A statement the database rejects ends its line with `failed: <error>`, and the error still raises. Each record also carries the statement as attributes, so a structured handler reads fields instead of parsing the line:

| Attribute | Value |
| --- | --- |
| `sql` | The statement, placeholders included |
| `params` | The bound parameters, as a tuple |
| `dialect` | `"sqlite"` or `"postgres"` |
| `duration_ms` | Time the database took, in milliseconds |
| `operation` | What sent it: one of the values below |
| `subject` | The table or enum type a pass statement changes, or the step file (`0003_add_slug/01_add_slug.up.sql`) a migration statement comes from; `None` otherwise |
| `connection` | The connection's name |
| `rows` | Rows affected, or `None` for a read |
| `error` | The database's error text, or `None` |

| `operation` | Sent by |
| --- | --- |
| `"fetch"` | `all()`, `first()`, `get()` and relation loads |
| `"count"` / `"exists"` | `count()` / `exists()` |
| `"save"` | `save()`, insert or update |
| `"bulk_create"` | `bulk_create()`, one record per chunk |
| `"update"` / `"delete"` | A query's `update()` / `delete()`; `delete()` on an instance |
| `"m2m_add"` / `"m2m_remove"` / `"m2m_clear"` | A many-to-many relation's `add()` / `remove()` / `clear()` |
| `"raw"` | `ferro.raw.execute()`, `fetch_all()` and `fetch_one()` |
| `"transaction"` | A `transaction()` block's `BEGIN`, `SAVEPOINT`, `RELEASE`, `ROLLBACK TO`, `COMMIT` and `ROLLBACK` |
| `"settings"` | A session's settings batch (`SET LOCAL`, `set_config`) and its reset |
| `"catalog"` | The Postgres catalog read the first time an operation touches a table |
| `"migrate"` | The reconciliation pass and `ferro migrate`, the run lock's statements included |

Statements a Postgres pool sends on its own, outside any operation, are not in the log: the `SET search_path` it runs on each new connection, and, with `settings_delivery="connection"`, the check it runs on each returned connection while a session has one pinned.

The logger is the only switch: while `ferro.sql` is not enabled for `DEBUG`, Ferro collects nothing. Ferro installs only a `NullHandler` on its `ferro` logger, which propagates, so your logging configuration (and pytest's `caplog`) decides where the records go. The `ferro` command line installs its own handler, so a data step's `ctx.log.info(...)` lines still print there.

## See also

- [Transactions](transactions.md): commit and roll back on one connection
- [How to use several databases](../howto/multiple-databases.md): routing across named connections
- [Schema management](schema/overview.md): auto-migrate, migrations and Alembic
- [Database backends](../concepts/backends.md): SQLite and PostgreSQL differences
- [Connection reference](../api/connection.md): `connect`, `PoolConfig`, `engines` and `Session`
