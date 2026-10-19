# Database backends

Ferro runs the same models, queries and transactions on SQLite and PostgreSQL; this page explains where the two backends still differ: column types, raw SQL and schema changes.

The examples on this page use this `User` model:

=== "Assignment"

    ```python
    --8<-- "docs/examples/concepts.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/concepts_annotated.py:models"
    ```

The URL passed to `connect()` picks the backend, and nothing after it changes:

```python
--8<-- "docs/examples/concepts.py:backends"
```

That block runs as written against an empty database, with `url` set to any of these:

| Backend | URL |
| :--- | :--- |
| SQLite, file-backed (`rwc` creates the file if missing) | `sqlite:app.db?mode=rwc` |
| SQLite, in memory | `sqlite::memory:` |
| PostgreSQL | `postgresql://user:password@localhost:5432/app` |
| PostgreSQL, short scheme | `postgres://user:password@localhost:5432/app` |

Hosted PostgreSQL services take the same `postgresql://` URL; [Connections › Supabase](../guide/connections.md#supabase) shows how to require TLS on one.

Ferro reads the backend from the URL once, in `connect()`. From then on the engine holds a SQLite pool or a PostgreSQL pool, two separate types, and no operation looks at the URL again. Any other scheme fails in `connect()`, naming the ones it accepts:

```text
Unsupported database URL scheme 'mysql'. Supported schemes: sqlite, postgres, postgresql
```

## Type mapping

Ferro derives each column's type from the field's annotation, and [Models and fields › Field types](../guide/models-and-fields.md#field-types) lists the type every annotation gets on each backend, generated from the code that decides it. The differences that matter come from how each database treats a declared type:

- **PostgreSQL types are strict.** A `uuid.UUID` field is a native `uuid` column, a `bool` is `bool`, a `datetime` is `timestamp with time zone`, and `dict`, `list` and nested-model fields are `jsonb` (`db_type="json"` gives plain `json`). A `StrEnum` becomes a native enum type.
- **SQLite types are advisory.** SQLite stores any value in any column, whatever its declared type, so a `bool` is stored as an integer, a `UUID` and a `datetime` as text, and a JSON field as JSON text.

Either way, a value comes back as the field's annotated Python type: a `bool` from SQLite's `0` or `1`, an exact `Decimal` with no float rounding, a `UUID`, a `datetime`, `date` or `time`, an enum member, or the `dict` or `list` you saved. The model is the contract; each backend stores the value its own way, and hydration turns it back into the annotated type.

An `int` field is `integer` on both backends. For a 64-bit column, declare `db_type="bigint"`; [Column type overrides with `db_type`](../guide/models-and-fields.md#column-type-overrides-with-db_type) lists every token.

## Raw SQL

The ORM binds every value for you, with the casts the column needs. [Raw SQL](../guide/raw-sql.md) goes to the driver as you wrote it, so two differences show through:

- **Placeholders are the backend's own:** `?` on SQLite, `$1`, `$2`, ... on PostgreSQL. Ferro does not translate them, and a mismatch fails with the database's own error. See [Raw SQL › Placeholders](../guide/raw-sql.md#placeholders).
- **Bound values are not cast.** Raw SQL has no model to read types from, so a `UUID`, `datetime` or `Decimal` is sent as text, and rows come back as plain dicts. PostgreSQL rejects text compared with a `uuid` or `numeric` column until you write the cast (`$1::uuid`, `$2::numeric`); SQLite accepts it, so a raw query that works on SQLite can fail on PostgreSQL. See [Raw SQL › Bind types and casts](../guide/raw-sql.md#bind-types-and-casts).

If you want typed values in and out, use the ORM.

## Schema changes

`connect(auto_migrate=True)` creates missing tables the same way on both backends, and `migrate_updates=True` adds missing columns and indexes on both. Beyond that, each backend changes what it can change in place:

- **PostgreSQL** changes a column's type (`ALTER COLUMN ... TYPE ... USING`), its nullability (`SET NOT NULL`, `DROP NOT NULL`) and Ferro's constraints in place.
- **SQLite** cannot change a column's type, nullability or constraints on an existing table; only a rebuild of the table can. Auto-migrate never rebuilds a table, so it raises a `UserWarning` naming the column and changes nothing. [`ferro migrate new`](../guide/schema/migrations.md#sqlite-table-rebuilds) writes the change as a reviewed table rebuild; the Alembic bridge refuses it and names `ferro migrate new`.

Because SQLite stores any value in any column, a SQLite column whose declared type differs from the model's still accepts every write; the warning is about the declared schema, not your data. [Auto-migrate › Applying column changes](../guide/schema/auto-migrate.md#applying-column-changes-with-migrate_updates) lists every change per backend.

## PostgreSQL schema isolation

Add `ferro_search_path` to a PostgreSQL URL and every pooled connection runs `SET search_path` to that schema:

```python {test=compile reason="needs a PostgreSQL server"}
from ferro import connect

await connect(
    "postgresql://user:password@localhost:5432/app?ferro_search_path=test_42",
    auto_migrate=True,
)
```

Unqualified table names now resolve in `test_42`, so many test runs can share one database, each with its own tables. [Connections › Postgres schema isolation](../guide/connections.md#postgres-schema-isolation) covers the rules for the name.

## See also

- [Connections](../guide/connections.md): URLs, sessions, pools and named connections
- [Models and fields › Field types](../guide/models-and-fields.md#field-types): the column type of every annotation
- [Raw SQL](../guide/raw-sql.md): placeholders and casts on each backend
- [Schema management](../guide/schema/overview.md): auto-migrate, migrations and the Alembic bridge on each backend
- [Architecture](architecture.md): where the backend layer sits in the engine
