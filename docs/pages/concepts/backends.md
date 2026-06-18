# Backends

Ferro supports SQLite and PostgreSQL through one Python API. Your models, queries, and transactions look identical on both; the Rust core selects the right SQLx driver, SQL dialect, and value conversion rules based on the connection URL.

## Supported Backends

The URL scheme passed to `connect()` decides the backend:

```python
from ferro import connect

# SQLite — file-backed (rwc = read/write/create) or in-memory
await connect("sqlite:app.db?mode=rwc")
await connect("sqlite::memory:")

# PostgreSQL — both scheme spellings work
await connect("postgresql://user:password@localhost:5432/app")
await connect("postgres://user:password@localhost:5432/app")
```

Hosted PostgreSQL providers such as **Supabase** work with a standard `postgresql://` connection string — Ferro's test matrix runs against Supabase-managed databases as well as local PostgreSQL.

Unsupported schemes (e.g. `mysql://`) fail at `connect()` time with an error naming the supported schemes. Backend detection happens once, during connection; after that the engine carries its backend kind and typed connection pool, so no operation rediscovers the database from URL strings.

!!! tip "Schema isolation for PostgreSQL tests"
    Ferro supports a private `ferro_search_path` URL parameter (stripped before SQLx connects) that runs `SET search_path TO <name>` on every pooled connection. Combined with `auto_migrate=True`, this lets many test runs share one PostgreSQL database while each sees only its own schema. Names must be ASCII alphanumeric or `_`.

## Type Mapping

Ferro maps Python field types to backend column types from your model annotations. SQLite uses type *affinity* (declared types are advisory), so the practical difference is mostly on the PostgreSQL side, where columns are strictly typed.

| Python type | SQLite | PostgreSQL | Notes |
|---|---|---|---|
| `int` | `INTEGER` | `INTEGER`/`BIGINT` | Autoincrement PKs come from `last_insert_rowid()` on SQLite; PostgreSQL uses `RETURNING`. Values always materialize as Python `int`. |
| `str` | text affinity | `VARCHAR` | Use `varchar(n)` / `db_type` tokens to pin an explicit length or `TEXT`. |
| `float` | numeric affinity | double precision | |
| `bool` | stored as integer 0/1 | `BOOLEAN` | Hydrates back to Python `bool` on both. |
| `uuid.UUID` | stored as text | native `uuid` | PostgreSQL expressions get explicit `::uuid` casts where needed; values round-trip as `uuid.UUID`. |
| `Decimal` | flexible (text/numeric affinity) | `NUMERIC` | Read as text on the wire so Python reconstructs an exact `Decimal` — no float precision loss. |
| `dict` / `list` | stored as JSON text | `JSON` | PostgreSQL writes cast JSON strings to `json`; reads parse back into Python values. |
| `datetime` / `date` | stored as ISO text | `TIMESTAMPTZ` / `DATE` | Temporal values cross the bridge as ISO strings and reconstruct into `datetime`/`date`. `db_type` tokens select `timestamp` vs `timestamptz`. |
| `Enum` | stored as text | text, or native enum type | Schema metadata carries the enum type name; PostgreSQL applies enum casts where the column uses a native enum type. |
| `bytes` | `BLOB` | `BYTEA` | |

The guiding rule: **the Python model is the contract**. Backends may store a value differently, but it must hydrate back into the annotated Python type exactly.

## Backend Differences

### Placeholders: `?` vs `$1`

The ORM handles parameter binding for you, but [raw SQL](../guide/queries.md) is passed through to the driver verbatim — placeholders are native to the backend, with no translation layer:

| Backend | Placeholder syntax | Example |
|---|---|---|
| PostgreSQL | `$1, $2, ...` | `select * from users where id = $1` |
| SQLite | `?` (positional) | `select * from users where id = ?` |

A mismatch surfaces as the database driver's own error.

### Type fidelity in raw SQL

Raw SQL has no schema map, so Ferro does not auto-cast bind values. Rich Python types are marshalled to wire-close primitives (`UUID`, `datetime`, `Decimal` → text; `dict`/`list` → JSON text), and rows come back as plain dicts of primitives. On PostgreSQL you write casts yourself where the column type demands them:

```python
from ferro import fetch_all

rows = await fetch_all(
    "select id, name from orders where id = $1::uuid and total > $2::numeric",
    order_id,
    minimum,
)
```

SQLite's flexible affinity usually needs no casts. Either way: if you want typed rows (`UUID`, `datetime`, `Decimal` objects), use the ORM — raw SQL is an escape hatch.

### Migration capabilities

`connect(auto_migrate=True)` creates missing tables identically on both backends, and `migrate_updates=True` adds missing columns on both. Beyond that, capabilities diverge with what each database can do in place:

- **PostgreSQL** supports in-place column **type changes** (`ALTER COLUMN ... TYPE ... USING` cast) and **nullability changes** (`SET`/`DROP NOT NULL`) when the live column disagrees with the model.
- **SQLite** cannot alter column types or nullability in place. Ferro emits a `UserWarning` naming the drifted column and pointing you at the [Alembic bridge](../guide/migrations.md). In practice SQLite's type affinity makes declared-type drift mostly cosmetic.

`migrate_destructive=True` drops model-removed columns on both backends (dependency-aware: covering indexes are dropped first; primary-key or constraint-enforced columns fail with a clear error instead). After any schema change the pool is refreshed so no cached statement observes the pre-migration schema.

## Troubleshooting

### `Engine not initialized`

You called a model or query method before `await connect(...)`. Importing models registers their schemas, but it does not connect to a database — call `connect()` during application startup.

### Unsupported URL scheme

Only `sqlite:`, `postgres://`, and `postgresql://` are accepted. Other databases (e.g. MySQL) are not currently supported.

### UUID or Decimal comparisons fail only on PostgreSQL

SQLite's affinity hides type mismatches that PostgreSQL enforces. In raw SQL, add explicit casts (`$1::uuid`, `$1::numeric`). Through the ORM this is handled for you — if you hit a case where it isn't, that's a bug worth [reporting](https://github.com/syn54x/ferro-orm/issues).

### Type or nullability drift warning on SQLite

`migrate_updates=True` detected that a live column's declared type or nullability disagrees with your model, and SQLite can't change it in place. Use the [Alembic bridge](../guide/migrations.md) for a table-rebuild migration, or ignore it if the drift is cosmetic.

## See Also

- [Architecture](architecture.md) — how the backend layer fits into the engine
- [Migrations](../guide/migrations.md) — `auto_migrate` flags and the Alembic bridge
- [Queries](../guide/queries.md) — the query builder and raw SQL escape hatch
