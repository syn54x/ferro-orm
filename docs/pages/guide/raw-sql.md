# Raw SQL

Ferro exposes a raw SQL escape hatch — `execute`, `fetch_all`, `fetch_one` — for statements that don't fit a `Model`.

## When to Reach for Raw SQL

Reach for raw SQL when the ORM can't express what you need: aggregations and reports, Postgres GUCs (`set_config`, `SET LOCAL`), advisory locks, `LISTEN/NOTIFY`, database-side functions, or one-off maintenance statements. For everyday CRUD, prefer the ORM — it returns typed, validated instances; raw SQL returns plain dicts of primitives.

!!! warning "Raw SQL is an escape hatch"
    Bind values cross the FFI as wire-close primitives, and rows come back as `dict[str, str | int | float | bool | bytes | None]`. UUID, datetime, and JSON columns are returned as **strings**. If you want typed rows, use the ORM.

## Executing Statements

`execute(sql, *args)` runs a statement and returns the number of affected rows:

```python
--8<-- "docs/examples/raw_sql.py:execute"
```

One statement per call — multi-statement strings are not supported. **Never** f-string user input into the `sql` argument; pass values as positional args so they are bound as parameters.

## Fetching Rows

`fetch_all(sql, *args)` returns a list of dicts; `fetch_one(sql, *args)` returns the first row or `None` (add `LIMIT 1` when more rows could match):

```python
--8<-- "docs/examples/raw_sql.py:fetch"
```

All three functions accept `using="name"` to route to a [named connection](connections.md#named-connections).

## Placeholders

Placeholders are **native to the backend** — there is no translation layer. What you write is what the driver runs, and mismatches surface as the database's own error.

=== "SQLite"

    Positional `?` placeholders:

    ```python
    from ferro import fetch_all

    rows = await fetch_all("SELECT * FROM users WHERE role = ? AND age >= ?", "admin", 18)
    ```

=== "PostgreSQL"

    Numbered `$1, $2, ...` placeholders:

    ```python
    from ferro import fetch_all

    rows = await fetch_all("SELECT * FROM users WHERE role = $1 AND age >= $2", "admin", 18)
    ```

## Type Caveats

Raw SQL has no schema map, so Ferro does not auto-cast bind values (matching asyncpg / psycopg behavior). Python values are marshalled to wire-close primitives:

| Python type | Sent as | Postgres cast you must write |
| :--- | :--- | :--- |
| `None` | `NULL` | — |
| `bool` | bool | — |
| `int` | `i64` | — |
| `float` | `f64` | — |
| `str` | text | — |
| `bytes` / `bytearray` | bytea / blob | — |
| `uuid.UUID` | text | `$N::uuid` |
| `datetime.datetime` | ISO 8601 text | `$N::timestamptz` |
| `datetime.date` | ISO 8601 text | `$N::date` |
| `datetime.time` | ISO 8601 text | `$N::time` |
| `decimal.Decimal` | text | `$N::numeric` |
| `enum.Enum` | recursive on `.value` | depends on `.value` type |
| `dict` / `list` | `json.dumps(...)` text | `$N::jsonb` |
| anything else | raises `TypeError` | — |

On PostgreSQL, write the casts in the SQL when the column type is stricter than text:

```python
from ferro import execute

sql = (
    "UPDATE events SET payload = $1::jsonb, occurred_at = $2::timestamptz "
    "WHERE id = $3::uuid"
)
await execute(sql, payload_dict, occurred_at, event_id)
```

The same caveat applies on the way out: UUID, datetime, and JSON **result columns** come back as strings — parse them yourself, or load through the ORM for typed values.

## Raw SQL in Transactions

Inside an `async with transaction()` block, top-level `execute` / `fetch_all` / `fetch_one` automatically run on the transaction's connection. The yielded `Transaction` handle (`as tx`) offers the same three methods bound explicitly, which is the hard-to-misuse path:

```python
--8<-- "docs/examples/raw_sql.py:in-transaction"
```

Passing `using=...` for a *different* connection inside an active transaction raises — a transaction is pinned to one connection.

!!! tip "Connection affinity"
    Outside a transaction, consecutive top-level calls may use **different pooled connections**. Wrap connection-affinity-sensitive sequences — `SET LOCAL`, advisory locks, `LISTEN/NOTIFY` — in `transaction()` so they share one connection.

## See Also

- [Transactions](transactions.md) — the `Transaction` handle and nesting behavior
- [Connections & Databases](connections.md) — named connections and routing
- [Queries](queries.md) — what the ORM can express without raw SQL
- [Raw SQL API reference](../api/raw-sql.md) — full signatures
