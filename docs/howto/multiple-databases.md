# How-To: Multiple Databases

Use named connections when one process needs more than one database, role, or pool. The common case still works with `await ferro.connect(url)`, which registers and selects `"default"`. Named connections are explicit.

## Basic Configuration

```python
import ferro

async def setup():
    await ferro.connect(
        "postgresql://localhost/main_db",
        name="primary",
        default=True,
    )
    await ferro.connect(
        "postgresql://localhost/replica_db",
        name="replica",
    )
    await ferro.connect(
        "postgresql://localhost/analytics_db",
        name="analytics",
        pool=ferro.PoolConfig(max_connections=3),
    )
```

## Using Specific Databases

```python
# Default database (primary)
users = await User.all()

# Specific database
replica_users = await User.using("replica").all()
analytics_data = await Metric.using("analytics").all()
```

## Transactions

```python
async with ferro.transaction(using="analytics"):
    await Metric.create(name="daily-active-users")
    await ferro.execute("select refresh_metric(?)", "daily-active-users")
```

The transaction pins work to one named connection. Nested transactions inherit that same connection. Ferro does not support distributed transactions across named connections.

## Schema Setup

Schema creation targets one connection:

```python
await ferro.create_tables(using="primary")
```

Do not run schema creation concurrently through multiple roles that point at the same physical database. Prefer one migration-capable connection and Alembic for production migrations.

## Security Notes

- Keep elevated service credentials server-side and out of source control.
- Do not choose `using` directly from untrusted request input.
- Do not make a service-role connection the default in user-facing runtimes.
- Named connections isolate pools and roles, not per-request RLS/JWT context inside one shared pool.
- Objects loaded through an elevated connection can contain elevated data; filter them before returning user-facing responses.

Automatic routing policies, read/write splitting, cross-connection joins, and two-phase transactions are not part of v1. Use explicit `using` calls where routing matters.

## See Also

- [Database Setup](../guide/database.md)
