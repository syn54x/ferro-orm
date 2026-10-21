---
description: "Run SQL you write yourself with execute(), fetch_all() and fetch_one(), for statements a model query cannot express."
---

# Raw SQL

[`execute()`][ferro.execute], [`fetch_all()`][ferro.fetch_all] and [`fetch_one()`][ferro.fetch_one] run SQL you write yourself, for the statements a model query cannot express.

Every example on this page uses one table of events. The `Event` model creates it, and Ferro names a table after its model in lowercase, so the SQL says `event`:

=== "Assignment"

    ```python
    --8<-- "docs/examples/raw_sql.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/raw_sql_annotated.py:models"
    ```

Here is a whole program. It connects, opens a session, adds two clicks and a signup, then counts the events by kind:

```python
--8<-- "docs/examples/raw_sql.py:first"
```

It prints:

<!-- prints: raw_sql.py:first -->
```text
[{'kind': 'click', 'n': 2}, {'kind': 'signup', 'n': 1}]
```

Each row is a plain `dict` keyed by column name. Every example on this page runs inside a session like this one; see [Sessions](connections.md#sessions) for why and how to open one.

## Use cases

Raw SQL fits what the ORM cannot express: reports with window functions, CTEs or `HAVING`, Postgres settings (`set_config`, `SET LOCAL`), advisory locks, `LISTEN`/`NOTIFY`, database functions and one-off maintenance statements. For everyday reads and writes, and for [aggregations and grouped queries](aggregations.md), which the ORM handles, use the ORM: it returns typed, validated model instances, where raw SQL returns dicts of primitives.

!!! note "Rows hold primitives"
    A raw row is a `dict[str, str | int | float | bool | bytes | None]`. UUID, datetime and JSON columns come back as strings. Load through the ORM when you want typed values.

## Fetching rows

`fetch_all(sql, *args)` returns every row as a list of dicts, as the first program showed. `fetch_one(sql, *args)` returns the first row, or `None` when no row matches:

```python
--8<-- "docs/examples/raw_sql.py:fetch-one"
```

The earliest event is the 09:00 click, and its `occurred_at` comes back as the string `"2026-03-02T09:00:00Z"`, not as a `datetime`. No event has the kind `purchase`, so the second call returns `None`. `fetch_one()` does not add `LIMIT 1` for you; write it when more than one row could match.

## Running a statement with `execute()`

`execute(sql, *args)` runs a statement and returns the number of rows it changed:

```python
--8<-- "docs/examples/raw_sql.py:execute"
```

Both clicks matched, so `execute()` returns `2`. Pass one statement per call; a string holding several statements is not supported. Pass every value as an argument after the SQL, never formatted into the string: arguments are sent as bound parameters, so a value cannot change the statement.

### Statements that refuse a transaction

A few Postgres statements refuse to run inside a transaction block: `CREATE INDEX CONCURRENTLY`, `VACUUM`, and `ALTER TYPE ... ADD VALUE` before Postgres 12. In a session that carries settings, Ferro runs every operation inside a transaction, so these statements fail with error `25001`. Pass `autocommit=True` to run one outside it:

```python {test=run prelude=raw_sql backend=postgres}
async with engines.session(settings={"myapp.tenant_id": "acme"}):
    await execute(
        "CREATE INDEX CONCURRENTLY idx_event_kind ON event (kind)",
        autocommit=True,
    )
```

The index builds outside any transaction. An `autocommit` statement carries none of the session's settings, so a row-security policy that reads `current_setting(...)` sees nothing while it runs. Use it for maintenance statements, never for statements that read or write rows a policy protects. Inside an explicit `transaction()` block, `autocommit=True` changes nothing and Postgres rejects the statement. On a connection whose pool sets `settings_delivery="connection"`, the settings live on the connection itself, so an `autocommit` statement still sees them; see [Connection delivery](../concepts/row-level-security.md#connection-delivery).

## Placeholders

Ferro hands your SQL to the driver unchanged, so you write each backend's own placeholders, and a mismatch surfaces as the database's own error:

=== "SQLite"

    Positional `?` placeholders:

    ```python {test=run prelude=raw_sql}
    rows = await fetch_all(
        "SELECT id, kind FROM event WHERE kind = ? AND id >= ?", "click", 2
    )
    assert rows == [{"id": 3, "kind": "click"}]
    ```

=== "PostgreSQL"

    Numbered `$1`, `$2`, ... placeholders:

    ```python {test=run prelude=raw_sql backend=postgres}
    rows = await fetch_all(
        "SELECT id, kind FROM event WHERE kind = $1 AND id >= $2", "click", 2
    )
    assert rows == [{"id": 3, "kind": "click"}]
    ```

Both return the 09:30 click, the only click with an `id` of 2 or more.

## Bind types and casts

Raw SQL has no schema map, so Ferro does not cast bind values for you, as asyncpg and psycopg do not. Each Python value is sent as the closest wire primitive:

| Python type | Sent as | Postgres cast you write |
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
| `enum.Enum` | its `.value`, by the same rules | depends on `.value` |
| `dict` / `list` | `json.dumps(...)` text | `$N::jsonb` |
| anything else | raises `TypeError` | — |

On PostgreSQL, write the cast in the SQL wherever the column type is stricter than text:

```python {test=run prelude=raw_sql backend=postgres}
from datetime import UTC, datetime

since = datetime(2026, 3, 2, 9, 5, tzinfo=UTC)
rows = await fetch_all(
    "SELECT id, kind FROM event WHERE occurred_at >= $1::timestamptz ORDER BY id",
    since,
)
assert rows == [{"id": 2, "kind": "signup"}, {"id": 3, "kind": "click"}]
```

`since` travels as ISO 8601 text and `::timestamptz` turns it back into a timestamp, so the signup and the later click come back. Without the cast, Postgres refuses the comparison with `operator does not exist: timestamp with time zone >= text`. Result columns work the same way in reverse: UUID, datetime and JSON values come back as strings, as `fetch_one()` showed.

## Raw SQL in transactions

Inside an `async with transaction()` block, the top-level `execute()`, `fetch_all()` and `fetch_one()` run on the transaction's connection. The handle the block yields (`as tx`) has the same three methods bound to that connection, which is harder to misuse:

```python
--8<-- "docs/examples/raw_sql.py:in-transaction"
```

The delete and the read share one connection, so the read already sees the two `tap` rows gone and only the signup left. Passing `using=` for a different connection inside an active transaction raises `ValueError`, because a transaction is pinned to one connection.

Outside a transaction, two consecutive calls may run on different pooled connections. Wrap a sequence that needs one connection, such as `SET LOCAL`, an advisory lock or `LISTEN`/`NOTIFY`, in `transaction()`. [Transactions](transactions.md) covers the handle and nesting.

## Routing

All three functions take `using="name"` to run on a [named connection](connections.md#named-connections), and `session=` to run in a session you hold instead of the current one. With neither, they run in the current session, and outside one they raise `RuntimeError`:

```python
--8<-- "docs/examples/raw_sql.py:no-session"
```

The error names the fix: open a session with `async with engines.session():`, or pass `using=` or `session=`. The [Raw SQL reference](../api/raw-sql.md) lists every parameter of `execute()`, `fetch_all()` and `fetch_one()`.

## See also

- [Transactions](transactions.md): the `Transaction` handle and nesting
- [Connections and databases](connections.md): named connections and sessions
- [Queries](queries.md): what the ORM expresses without raw SQL
- [Raw SQL reference](../api/raw-sql.md): full signatures
