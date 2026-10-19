---
description: "Connect one process to two databases and route each model call to the right one."
---

# How to use several databases in one app

This how-to connects one process to two databases, an application database and an analytics database, and routes each call to the right one.

Give each connection a name, mark one as the default, and route calls to each:

```python
--8<-- "docs/examples/multiple_databases.py:connect"
```

It prints:

```text
1 1
```

Each database holds one metric: `signups` went to the default connection, `app`, through the session, and `page_views` went to `analytics` through `.using()`. [Named connections](../guide/connections.md#named-connections) in the Connections guide explains how naming and the default work, and [Sessions](../guide/connections.md#sessions) explains the session.

The examples use one model, a named metric:

=== "Assignment"

    ```python
    --8<-- "docs/examples/multiple_databases.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/multiple_databases_annotated.py:models"
    ```

Each `connect()` call opens its own pool, sized with `pool=PoolConfig(...)`, and `auto_migrate=True` creates the `metric` table on each database.

## Route calls to a connection

Every call needs a route. Inside `async with engines.session():`, unqualified calls use the session's connection: the default connection when you open the session with no name. [`Model.using(name)`](../api/model.md#ferro.Model.using) routes one call and needs no session.

To run a whole block on one named connection, open a session on it:

```python
--8<-- "docs/examples/multiple_databases.py:session"
```

It prints:

```text
['page_views']
```

Only the analytics database has a metric above 100.

Inside a session, `.using()` must name the session's connection. Naming another one raises `ValueError` rather than running the call outside the session:

```python
--8<-- "docs/examples/multiple_databases.py:conflict"
```

It prints:

```text
Explicit `using='analytics'` conflicts with the ambient session bound to connection 'app'. Pass an explicit `session=` for that connection, or open a session on 'analytics'.
```

A call with no session and no `using=` raises `RuntimeError`; Ferro never falls back to the default connection on its own.

Routing is per call: `.using()` changes no global state, so two coroutines can use different databases at once. A `.using()` call outside a session has no identity map, so every load returns a new instance; see [Sessionless operations](../concepts/identity-map.md#sessionless-operations).

## Transactions on a named connection

`transaction(using=name)` pins a transaction to one connection. Unqualified calls inside the block run on it, and routing a call elsewhere raises `ValueError`:

```python
--8<-- "docs/examples/multiple_databases.py:transaction"
```

It prints:

```text
Operations inside a transaction inherit the transaction connection
2
```

The `clicks` metric landed on `analytics`, which now holds two metrics; the call routed to `app` was refused. Raw SQL through `execute()` and `fetch_all()`, and nested `transaction()` blocks, inherit the connection too. Ferro has no distributed transactions: one transaction spans one connection, so writes to two databases never commit together.

## Create tables on one connection

`connect(url, name=..., auto_migrate=True)` creates the tables on that connection as it connects. To create them later, call `create_tables(using=name)`:

```python
--8<-- "docs/examples/multiple_databases.py:create-tables"
```

It prints:

```text
['metric']
```

`create_tables()` returns a [`PassReport`](../api/connection.md#ferro.PassReport) of the statements it ran, here one `CREATE TABLE` for `metric`. Two connections that name the same database share its run lock, so their passes run one after the other ([Concurrent passes and the run lock](../guide/schema/auto-migrate.md#concurrent-passes-and-the-run-lock)).

For production schema changes, use [migrations](../guide/schema/migrations.md): a project whose models live in separate databases [configures each one](../reference/configuration.md#several-databases) with its own migrations directory, and every command names it with `--database` ([Deploying migrations](../guide/schema/deploying.md#several-databases)).

## Security and routing rules

!!! warning "Never route from untrusted input"
    Choose connection names from constants or trusted server-side logic. A name taken from a request parameter or header lets the caller pick the database.

- **Typical roles.** A read replica for expensive list endpoints (`Metric.using("replica")`), an analytics database that receives event rows, or a database another team owns.
- **Keep credentials server-side.** Elevated service-role connections belong in configuration, not source control, and a user-facing runtime never makes one its default.
- **Pools isolate roles, not request context.** A named connection separates credentials and pooling; it does not give each request its own row-security context inside one shared pool. Objects loaded through an elevated connection can hold elevated data, so filter them before returning them to users.
- **No automatic routing.** Ferro has no read/write splitting, cross-connection joins or two-phase commit. Every route is a `using=`, `session=` or `engines.session(name)` you write yourself.

## See also

- [Connections](../guide/connections.md): URLs, pools, sessions and the default connection
- [Transactions](../guide/transactions.md): commit, rollback and savepoints
- [Connection reference](../api/connection.md): `connect`, `PoolConfig`, `set_default_connection` and `create_tables`
