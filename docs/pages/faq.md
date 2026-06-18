# Frequently Asked Questions

## General

### What is Ferro?

Ferro is an async Python ORM with a Rust core. Models are Pydantic V2 `BaseModel` subclasses; SQL generation, connection pooling, query execution, and row hydration happen in compiled Rust (PyO3 + SQLx + Sea-Query). You write ordinary async Python — the Rust engine is invisible at the API level. See [Architecture](concepts/architecture.md).

### Is Ferro production-ready?

Ferro is pre-1.0. The core feature set — models, queries, relationships, transactions, named multi-database connections, raw SQL, auto-migration, and the Alembic bridge — is implemented and tested against both SQLite and PostgreSQL on every change. But pre-1.0 means what it says: APIs may still shift between minor versions, the community is small, and you'll find fewer battle scars documented than for SQLAlchemy or Django ORM.

A reasonable posture: well suited to new projects and services where you control the upgrade cadence and have good test coverage; pin your version, read the [changelog](changelog.md) before upgrading, and report what you hit.

### What license is Ferro under?

Apache-2.0.

### Why does Ferro require Python 3.13+?

Ferro is a young project and deliberately targets current Python rather than carrying compatibility shims for older interpreters. That keeps the codebase on modern typing features and current Pydantic and PyO3 releases. The floor may widen as the project matures, but supporting older Pythons is not a near-term goal.

### Do I need to know Rust?

No. Ferro's API is 100% Python and ships as a prebuilt wheel. Rust only enters the picture if you build from source or contribute to the engine itself.

## Features

### Does Ferro support synchronous code?

No — Ferro is async-only. Every database operation is a coroutine (`await User.all()`). There is no sync facade and none is planned; if your application is synchronous, you'd be running an event loop per call (`asyncio.run(...)`), which works for scripts but defeats the purpose in a server. Ferro is designed for async frameworks like FastAPI, Litestar, and aiohttp.

### Which databases are supported?

SQLite and PostgreSQL (including hosted providers such as Supabase). MySQL and other databases are not currently supported — unsupported URL schemes fail at `connect()` time. See [Backends](concepts/backends.md).

### What's the migrations story?

Two tiers:

- **Auto-migration at connect time** — `connect(url, auto_migrate=True)` creates missing tables. As of 0.11.0, `migrate_updates=True` also adds missing columns (and reconciles type/nullability drift on PostgreSQL), and `migrate_destructive=True` drops model-removed columns. Great for development and simple deployments.
- **Alembic bridge** — for versioned, reviewable production migrations and anything auto-migrate can't express (renames, primary-key changes, complex transforms). Install with `pip install "ferro-orm[alembic]"`.

See [Migrations](guide/migrations.md).

### Does Ferro support multiple databases?

Yes — register each pool under a name and route explicitly:

```python
import ferro

await ferro.connect(APP_DATABASE_URL, name="app", default=True)
await ferro.connect(SERVICE_DATABASE_URL, name="service")

users = await User.all()                    # default connection
jobs = await Job.using("service").all()     # explicit routing
```

Automatic read/write splitting, cross-database joins, and distributed transactions are out of scope.

### Does Ferro support raw SQL?

Yes. `execute`, `fetch_all`, and `fetch_one` (top-level or on the handle yielded by `transaction()`) run parameterized raw SQL. Rows come back as plain dicts of primitives — it's an escape hatch, not a typed query path. See [Queries](guide/queries.md).

### Does Ferro have eager loading (`prefetch_related` / `select_related`)?

Not yet — it's on the [roadmap](roadmap.md). Today, awaiting a relationship attribute issues a query, so be deliberate about relationship access inside loops (the classic N+1 pattern).

## Performance

### Is Ferro faster than other Python ORMs?

For CPU-bound ORM work — bulk inserts, hydrating large result sets, heavy concurrent query loads — yes, meaningfully: that work runs in Rust with the GIL released. For network- or disk-bound operations (single-row fetches, slow queries, remote databases), wait time dominates and any async ORM performs similarly. We intentionally don't publish benchmark multipliers; measure your own workload. See [Performance](concepts/performance.md) for what to optimize and how to benchmark fairly.

## Troubleshooting

### I get "Engine not initialized"

You ran a query before connecting. Importing models registers their schemas but does not open a database connection — call `await ferro.connect(...)` during application startup, before the first query.

### I get "Relationship resolution failed" or an error about `related_name`

Relationships are declared on both sides and cross-validated when you connect. Two common causes:

- The model named in a string/forward reference (`"Author"`) was never imported, so it isn't registered — import all your model modules before `connect()`.
- The target model is missing the reverse field: a `ForeignKey(related_name="posts")` requires the target to declare `posts: Relation[list["Post"]] = BackRef()`. The error message names the model and field to add.

See [Relationships](guide/relationships.md).

### I get `ModelDoesNotExist`

`Model.get(pk)` raises `ferro.ModelDoesNotExist` (a `LookupError` subclass) when no row matches the primary key. If absence is an expected case, use `Model.get_or_none(pk)`, which returns `None` instead:

```python
user = await User.get_or_none(user_id)
if user is None:
    ...  # handle missing row
```

### Where do I report bugs or ask questions?

Bugs and feature requests: [GitHub Issues](https://github.com/syn54x/ferro-orm/issues). Questions and discussion: [GitHub Discussions](https://github.com/syn54x/ferro-orm/discussions).
