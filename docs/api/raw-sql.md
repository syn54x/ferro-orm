# Raw SQL

Ferro exposes a raw SQL escape hatch for statements that don't fit a `Model` —
Postgres GUCs (`set_config`, `SET LOCAL`), advisory locks, `LISTEN/NOTIFY`,
or any one-off query.

!!! warning "Raw SQL is an escape hatch"
    Bind values cross the FFI as wire-close primitives, and rows come back as
    `dict[str, str | int | float | bool | bytes | None]`. UUID/datetime/JSON
    columns are returned as **strings**. **If you want typed rows, use the
    ORM.**

## Two surfaces, same plumbing

### Transaction-bound (preferred)

```python
from ferro import transaction

async with transaction() as tx:
    await tx.execute(
        "select set_config('request.jwt.claims', $1, true)",
        claims_json,
    )
    rows = await tx.fetch_all(
        "select id, name from users where org_id = $1 limit 50",
        org.id,
    )
    row = await tx.fetch_one(
        "select count(*) as n from users where org_id = $1",
        org.id,
    )
```

The `tx` handle owns the transaction's connection. You cannot misuse it —
calling `tx.execute(...)` after the `async with` block exits raises
`RuntimeError`.

### Top-level (`using` or active transaction)

```python
from ferro import execute, fetch_all, fetch_one

# Outside any tx — runs on the default connection.
await execute("select pg_advisory_unlock_all()")

# Route explicitly to a named connection.
await execute("select run_pipeline_job($1)", job_id, using="service")

# Inside a tx — auto-picked up via the same ContextVar that Model.create() uses.
async with transaction(using="service"):
    await execute("select set_config('request.jwt.claims', $1, true)", claims_json)
    rows = await fetch_all("select * from foo where org_id = $1", org_id)
```

Passing `using=...` inside an active transaction raises. A transaction is pinned to
one connection, and unqualified raw SQL inherits that connection.

## Placeholders are native to the backend

| Backend  | Placeholder syntax | Example                                |
| -------- | ------------------ | -------------------------------------- |
| Postgres | `$1, $2, …`        | `select set_config('k', $1, true)`     |
| SQLite   | `?` (positional)   | `select * from users where id = ?`     |

There is no translation layer. What you write is what `sqlx::query(sql)` runs.
Mismatches surface as the database driver's own error.

## Bind type table

| Python type             | Sent as                | Postgres cast you must write |
| ----------------------- | ---------------------- | ---------------------------- |
| `None`                  | `NULL`                 | —                            |
| `bool`                  | bool                   | —                            |
| `int`                   | `i64`                  | —                            |
| `float`                 | `f64`                  | —                            |
| `str`                   | text                   | —                            |
| `bytes` / `bytearray`   | bytea / blob           | —                            |
| `uuid.UUID`             | text                   | `$N::uuid`                   |
| `datetime.datetime`     | ISO 8601 text          | `$N::timestamptz`            |
| `datetime.date`         | ISO 8601 text          | `$N::date`                   |
| `datetime.time`         | ISO 8601 text          | `$N::time`                   |
| `decimal.Decimal`       | text                   | `$N::numeric`                |
| `enum.Enum`             | recursive on `.value`  | (depends on `.value` type)   |
| `dict` / `list`         | `json.dumps(v)` text   | `$N::jsonb`                  |
| anything else           | `TypeError` is raised  | —                            |

Raw SQL has no schema map, so Ferro does not auto-cast bind values. This
matches asyncpg / psycopg / pgx behavior. **Never** f-string user input into
the `sql` argument — use placeholders and pass values as positional args.

### Postgres cast cheat-sheet

```python
"... where id = $1::uuid"                # uuid.UUID
"... where created_at = $1::timestamptz" # datetime
"... where day = $1::date"               # date
"... where amount = $1::numeric"         # Decimal
"... set data = $1::jsonb"               # dict / list
```

## Connection affinity

Outside a `transaction()` block, each top-level `execute` / `fetch_all` /
`fetch_one` call runs on the selected named pool (`using=...`) or the default
pool. Consecutive calls may use different physical connections from that pool.
Wrap in `transaction(using=...)` for connection-affinity-sensitive operations
like `SET LOCAL`, advisory locks, or `LISTEN/NOTIFY`.

## What raw SQL doesn't do

- **No typed rows.** Rows are always plain dicts of primitives. If you want
  `uuid.UUID` / `datetime` / `Decimal` objects, use `Model.fetch_*`.
- **No multi-statement strings.** One statement per call.
- **No string-interpolation guard.** The API forces placeholders by shape;
  detecting f-strings at runtime is not possible.
- **No auto type-casts on Postgres.** Write `$N::uuid` / `$N::jsonb` yourself.

## API reference

::: ferro.execute
::: ferro.fetch_all
::: ferro.fetch_one
::: ferro.Transaction
