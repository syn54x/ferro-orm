# Backend Guide

Ferro supports SQLite and PostgreSQL through one Python API and an explicit Rust backend layer. Application code still calls `connect()`, defines Pydantic-style models, and uses the query builder. The Rust core decides which typed SQLx driver, SeaQuery dialect, transaction connection, and value conversion rules apply for the active database.

This guide starts with the user-facing behavior, then explains the implementation details that maintainers need when changing the backend.

## What The Backend Is

The backend is the runtime database engine behind Ferro's Python API. It owns:

- the active database kind, currently SQLite or PostgreSQL
- the typed SQLx connection pool
- SQL execution and row materialization
- transaction-bound typed connections
- backend-specific SQL generation choices
- value binding and hydration rules

The backend supports a registry of named connections. The common case still uses one default engine per process, while advanced applications can register multiple pools and route ORM, raw SQL, transaction, and schema operations with `using="name"`.

## Supported Backends

Ferro currently treats these URL schemes as first-class runtime targets:

```python
await connect("sqlite:app.db?mode=rwc")
await connect("sqlite::memory:")
await connect("postgresql://user:password@localhost:5432/app")
await connect("postgres://user:password@localhost:5432/app")
```

Unsupported schemes fail during connection setup:

```python
await connect("mysql://user:password@localhost/app")
# raises a connection error: supported schemes are sqlite, postgres, postgresql
```

The important implementation detail is that URL detection happens once during `connect()`. After that, the active `EngineHandle` carries the backend kind and typed pool, so operations do not need to rediscover the database from global state or URL strings.

## Connection Lifecycle

`ferro.connect()` is the public entry point. Internally, the Rust connection layer does four things:

1. Splits Ferro-only query parameters from the database URL.
2. Classifies the backend from the URL scheme.
3. Creates a typed SQLx pool for that backend.
4. Registers an `Arc<EngineHandle>` under a connection name and optionally selects it as the default.

SQLite uses `SqlitePoolOptions` and PostgreSQL uses `PgPoolOptions`. `PoolConfig(max_connections=..., min_connections=...)` is applied per named connection, so app-role and service-role pools can have different sizes.

```text
connect(url, name, default, auto_migrate, pool)
  -> split ferro_search_path
  -> BackendKind::from_url(url)
  -> connect typed pool
  -> optionally create tables
  -> store EngineHandle in the named registry
  -> optionally update the default connection
```

Connection resolution is centralized. Explicit `using` wins outside a transaction; active transactions pin all work to their selected connection; instance methods prefer the instance's origin connection; unqualified calls then fall back to the selected default connection.

### PostgreSQL Search Paths

Ferro supports a private `ferro_search_path` URL parameter for test isolation:

```python
await connect(
    "postgresql://localhost/ferro?ferro_search_path=ferro_test_schema",
    auto_migrate=True,
)
```

The parameter is removed before SQLx connects. If present, Ferro installs an `after_connect` hook that runs:

```sql
SET search_path TO ferro_test_schema
```

Search path names must be ASCII alphanumeric or `_`. This keeps the test helper ergonomic without allowing arbitrary SQL in the connection URL.

Use this when several test runs share one PostgreSQL database, but each test should see its own tables. Instead of creating and dropping a whole database for every test, create a temporary schema, connect with that schema as the search path, and let `auto_migrate=True` create the model tables there:

```python
import uuid

import psycopg
from ferro import connect, reset_engine


async def run_isolated_postgres_test(base_url: str):
    schema_name = f"ferro_{uuid.uuid4().hex[:16]}"

    with psycopg.connect(base_url, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema_name}"')

    try:
        await connect(
            f"{base_url}?ferro_search_path={schema_name}",
            auto_migrate=True,
        )

        # Test code now reads and writes tables in only this schema.
        # A second test can use the same database with a different schema.
    finally:
        reset_engine()
        with psycopg.connect(base_url, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
```

This is how Ferro's PostgreSQL matrix keeps tests isolated while still supporting both local `pytest-postgresql` databases and externally managed databases such as Supabase.

## Typed Engine Internals

The core backend types live in `src/backend.rs`.

```text
BackendKind
  Sqlite
  Postgres

EngineHandle
  backend: BackendKind
  pool: BackendPool

BackendPool
  Sqlite(Arc<SqlitePool>)
  Postgres(Arc<PgPool>)

EngineConnection
  Sqlite(PoolConnection<Sqlite>)
  Postgres(PoolConnection<Postgres>)
```

This replaced the old `sqlx::Any`-centered execution path. Instead of one generic pool that tries to behave like every database, Ferro stores exactly the pool it connected:

- SQLite connections are executed through SQLx's SQLite driver.
- PostgreSQL connections are executed through SQLx's PostgreSQL driver.
- Transaction connections keep the same typed distinction.
- Backend dispatch is a small enum match at the boundary where SQL actually runs.

This gives Ferro access to backend-specific SQLx behavior without making the Python API backend-specific.

## Query And Mutation Execution

Most ORM operations follow the same high-level pipeline:

```text
Python Query / Model API
  -> JSON query or mutation payload
  -> Rust operation function
  -> SeaQuery statement
  -> backend-specific SQL builder
  -> EngineBindValue list
  -> EngineHandle or EngineConnection execution
  -> EngineRow values
  -> RustValue values
  -> Python model instances
```

SeaQuery remains the SQL construction layer. The backend controls which SeaQuery builder lowers the statement:

- SQLite uses `SqliteQueryBuilder`
- PostgreSQL uses `PostgresQueryBuilder`

Bind values are converted into a backend-neutral Ferro enum before execution:

```text
EngineBindValue
  Bool
  I64
  F64
  String
  Bytes
  Null
```

The backend then binds those values to the typed SQLx query. This keeps most operation code independent of SQLx's generic types, while still executing through real SQLite or PostgreSQL drivers.

### Reads

Read operations fetch typed rows through the engine, materialize each SQLx row into `EngineRow`, then convert the values into Ferro's internal `RustValue` representation. `RustValue` is the final GIL-free representation before Python objects are created.

This split matters because database values are not the same as Python field values. For example:

- a PostgreSQL `integer` may decode as `i32`, but Ferro model IDs use Python `int`
- PostgreSQL UUIDs are selected as text before becoming Python `uuid.UUID`
- Decimal values are selected as text before becoming Python `Decimal`
- JSON values are selected as text before becoming Python dicts or lists

### Writes

Create, update, relationship, and delete operations build SeaQuery statements and execute them through either:

- the active `EngineHandle`, if no transaction is active
- the transaction's `EngineConnection`, if a transaction ID is present

SQLite insert results can report `last_insert_rowid()`. PostgreSQL insert paths rely on explicit `RETURNING` where Ferro needs generated values.

## Schema Metadata And DDL

The backend depends on normalized schema metadata from Python. `src/ferro/schema_metadata.py` enriches Pydantic's JSON schema with Ferro-specific keys before Rust consumes it.

Important metadata includes:

- `primary_key`
- `autoincrement`
- `unique`
- `index`
- `foreign_key`
- `ferro_nullable`
- `format: "decimal"`
- `enum_type_name`

That metadata is shared by:

- Rust runtime DDL in `src/schema.rs`
- Alembic metadata generation in `src/ferro/migrations/alembic.py`
- query and mutation casting decisions in `src/operations.rs`
- relationship join-table generation in `src/ferro/relations/__init__.py`

The goal is to make the Python schema the contract. Runtime DDL and Alembic may lower it differently, but they should not infer conflicting meanings from the same model.

### Auto-Migration

When `auto_migrate=True`, `connect()` creates the typed engine first, then asks Rust to create tables for all registered models.

```python
await connect("sqlite:dev.db?mode=rwc", auto_migrate=True)
await connect("postgresql://localhost/ferro", auto_migrate=True)
```

Runtime DDL uses the active backend:

- SQLite gets SQLite-compatible column definitions and index SQL.
- PostgreSQL gets PostgreSQL-compatible column definitions, native casts, and SQL syntax.

## Type Handling Across SQLite And Postgres

SQLite and PostgreSQL do not store or decode every logical type the same way. Ferro's backend layer aims to preserve the Python model contract while allowing backend-specific SQL where needed.

### Integer Primary Keys

SQLite autoincrement IDs come from `last_insert_rowid()`. PostgreSQL `SERIAL` / integer values may decode as `i32`; Ferro materializes them as `i64` and then Python `int`.

### UUID

UUIDs are a bridge-boundary type. They can appear as:

- Python `uuid.UUID`
- JSON query payload strings
- SQL bind values
- PostgreSQL `uuid` columns
- SQLite text-like columns

Ferro serializes UUIDs before JSON query payloads cross the Python/Rust boundary. For PostgreSQL SQL expressions, Ferro adds explicit `uuid` casts where SQLx or PostgreSQL would otherwise see text. Many-to-many add, remove, and clear operations use the same backend-aware cast path for UUID join-table columns.

### Decimal

Python `Decimal` fields are marked with `format: "decimal"` in schema metadata. PostgreSQL can use numeric storage, while SQLite remains more flexible. On reads, Ferro selects Decimal values as text when needed so Python can reconstruct an exact `Decimal`.

### JSON Objects And Arrays

Python `dict` and `list` fields are represented as JSON object or array schema types. PostgreSQL writes cast JSON strings to `json` so inserts and updates target native JSON columns correctly. Reads select JSON values as text when required, then parse them back into Python values.

### Dates And Datetimes

Temporal values cross the bridge as ISO strings and are reconstructed into Python `date` or `datetime` objects. PostgreSQL SQL generation applies explicit casts for temporal comparisons and nulls where needed.

### Enums

Enums are represented through schema metadata, including the enum type name. PostgreSQL-specific enum casts are applied where the column uses a native enum type. Portable text-like enum behavior remains available through the same Python model shape.

## Transactions

Transactions use the same typed backend model as normal operations.

When a root transaction begins:

```text
active EngineHandle
  -> acquire typed pool connection
  -> BEGIN
  -> TransactionHandle::root(EngineConnection)
```

Nested transactions reuse the same typed connection and create savepoints:

```text
parent TransactionConnection
  -> SAVEPOINT sp_<tx_id>
  -> TransactionHandle::nested(parent_conn, savepoint_name)
```

The transaction registry stores a transaction ID mapped to:

- a shared `Arc<Mutex<EngineConnection>>`
- an optional savepoint name

This means all operations inside a transaction execute on the same typed database connection. Commit and rollback dispatch through the `EngineConnection` enum, not through a generic SQLx connection.

## Testing The Backend Matrix

Backend correctness is tested with the same public API users call. Tests that should run on both databases use the backend matrix fixtures:

```python
@pytest.mark.backend_matrix
async def test_create_and_fetch(db_url):
    await connect(db_url, auto_migrate=True)
    ...
```

Run the SQLite default suite:

```bash
uv run pytest -q
```

Run the SQLite/PostgreSQL matrix:

```bash
uv run pytest -m "backend_matrix or postgres_only" --db-backends=sqlite,postgres -q
```

Run only the PostgreSQL side:

```bash
uv run pytest -m "backend_matrix or postgres_only" --db-backends=postgres -q
```

### Local PostgreSQL Provider

The test harness supports local ephemeral PostgreSQL through `pytest-postgresql`.

Install PostgreSQL server binaries, then force the local provider:

```bash
brew install postgresql@16
FERRO_POSTGRES_PROVIDER=local uv run pytest -m "backend_matrix or postgres_only" --db-backends=postgres -q
```

If `FERRO_POSTGRES_PROVIDER=local` is not set, tests prefer an external URL:

1. `FERRO_POSTGRES_URL`
2. legacy `FERRO_SUPABASE_URL`
3. local `pytest-postgresql` fallback

Each PostgreSQL test gets an isolated schema through `ferro_search_path`, so externally managed databases can still run isolated test cases.

## How To Extend This Later

The current backend design makes a future backend, such as MySQL, more approachable but not automatic. A new backend would need:

1. A new `BackendKind` variant.
2. A typed SQLx pool and connection variant.
3. URL classification.
4. SeaQuery builder dispatch.
5. DDL type mapping in `src/schema.rs`.
6. bind and row materialization support in `src/backend.rs`.
7. schema-value casting rules in `src/operations.rs`.
8. backend-matrix test coverage.
9. docs that clearly state support level and known differences.

Avoid adding a backend by sprinkling one-off branches through query, schema, and operation code. The maintainable path is to make the backend identity explicit first, then lower shared ORM semantics through that backend.

## Troubleshooting And Gotchas

### `Engine not initialized`

You called a model or query method before `await connect(...)`. Importing models registers schema, but it does not connect to the database.

### Unsupported URL scheme

Only `sqlite:`, `postgres://`, and `postgresql://` are supported. MySQL is planned for later, not accepted by this backend.

### PostgreSQL tests use the wrong database

If `.env` contains `FERRO_POSTGRES_URL` or `FERRO_SUPABASE_URL`, the test harness will use it by default. Set `FERRO_POSTGRES_PROVIDER=local` to force `pytest-postgresql`.

### Local PostgreSQL tests skip or fail to start

`pytest-postgresql` needs server binaries such as `pg_ctl`, `postgres`, and `initdb` on `PATH`. On macOS with Homebrew, installing `postgresql@16` usually provides them.

### UUID or Decimal values fail only on PostgreSQL

Check whether the value crosses the Python/Rust boundary as JSON or as a direct PyO3 argument. Query payloads must serialize non-JSON-native Python values before `json.dumps`; direct relationship operations must preserve typed values long enough for backend-aware SQL casts.

### Runtime DDL and Alembic disagree

Start with schema metadata. If `ferro_nullable`, `format`, `primary_key`, or relationship metadata is missing from the normalized Python schema, Rust DDL and Alembic may lower the same model differently. Fix the metadata source before adding more backend-specific lowering rules.

## Mental Model

The shortest way to understand the backend is:

```text
Python owns the model contract.
Rust owns execution.
SeaQuery owns SQL shape.
SQLx owns typed database I/O.
BackendKind decides which database-specific path is legal.
```

When changing backend behavior, preserve that separation. Put shared ORM meaning in schema/query metadata, then make the backend choose the correct SQLite or PostgreSQL lowering at the execution boundary.
