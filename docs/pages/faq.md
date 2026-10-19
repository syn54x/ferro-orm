---
description: "Short answers about what Ferro is, what it supports and how to fix its most common errors."
---

# FAQ

Short answers about what Ferro is, what it supports and how to fix its most common errors. The answers share one pair of models, `Author` and `Post`, declared here once:

=== "Assignment"

    ```python
    --8<-- "docs/examples/faq.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/faq_annotated.py:models"
    ```

An `Author` has many `Post` rows, and each `Post` points back at its `Author`. Every block below runs inside `async with engines.session():`; see [Connections › Sessions](guide/connections.md#sessions) for what a session is.

## What Ferro is

Ferro is an async Python ORM with a Rust core. Models are Pydantic V2 `BaseModel` subclasses; SQL generation, connection pooling, query execution and row hydration run in compiled Rust (PyO3, SQLx and Sea-Query). You write ordinary async Python and the engine stays out of sight. See [Architecture](concepts/architecture.md).

To see it in use, ask for the authors named Ada Lovelace:

```python {test=run prelude=reference}
from ferro import engines

async with engines.session():
    authors = await Author.where(lambda author: author.name == "Ada Lovelace").all()
    assert [author.email for author in authors] == ["ada@example.com"]
```

The query returns `Author` instances, and the assert shows the one it found.

## Production readiness

Ferro is pre-1.0. The core feature set (models, queries, relationships, transactions, named connections, raw SQL, auto-migration and the Alembic bridge) is implemented and tested against SQLite and PostgreSQL on every change. Pre-1.0 means APIs may still shift between minor versions, the community is small, and fewer problems are documented than for SQLAlchemy or Django ORM.

Ferro suits new projects and services where you control the upgrade cadence and have good test coverage. Pin your version, read the [changelog](changelog.md) before you upgrade, and report what you hit.

## License

Ferro is licensed under Apache-2.0. The terms are in the repository's [`LICENSE`](https://github.com/syn54x/ferro-orm/blob/main/LICENSE) file.

## Python version

Ferro requires Python 3.13 or later. It targets current Python on purpose, so the codebase uses modern typing features and current Pydantic and PyO3 releases instead of compatibility shims. The floor may widen as the project matures; supporting older interpreters is not a near-term goal.

## Rust

You do not need to know Rust. Ferro's API is Python, and it ships as a prebuilt wheel. Rust matters only if you build from source or contribute to the engine; see [Contributing](contributing.md).

## Synchronous code

Ferro is async-only. Every database operation is a coroutine (`await Author.all()`), and there is no sync facade. A synchronous script can call `asyncio.run(...)` once per entry point, but a synchronous server would pay for an event loop per call, which defeats the purpose. Ferro is designed for async frameworks such as FastAPI, Litestar and aiohttp.

## Supported databases

Ferro supports SQLite and PostgreSQL, including hosted providers such as Supabase. MySQL and other databases are not supported, and an unsupported URL scheme fails at `connect()`. See [Backends](concepts/backends.md).

## Migrations

Ferro has two ways to change a schema, plus a bridge to Alembic:

- **Auto-migrate at connect time.** `connect(url, auto_migrate=True)` creates missing tables. `migrate_updates=True` also alters existing ones as far as an in-place statement can, and `migrate_destructive=True` drops columns the model no longer declares. Use it in tests and development.
- **Migrations.** `ferro migrate new` writes each change as a numbered, reviewable directory of SQL (one rendering per target dialect) and Python data steps, and `ferro migrate up` applies it. Renames, backfills, SQLite table rebuilds and a way back down live here. Install the commands with `pip install "ferro-orm[cli]"`.
- **The Alembic bridge.** A project already on Alembic can keep it: the [Alembic bridge](guide/schema/alembic.md) writes revisions from the same planner.

See [Schema management](guide/schema/overview.md) for choosing between them.

## Multiple databases

Ferro registers each pool under a name, and you route to it explicitly:

```python
--8<-- "docs/examples/faq.py:databases"
```

The default connection answers inside a session, and `using("analytics")` is its own explicit route to the second database. Automatic read/write splitting, cross-database joins and distributed transactions are out of scope. See [Multiple databases](howto/multiple-databases.md) for the full recipe.

## Raw SQL

Ferro runs parameterized raw SQL with `execute`, `fetch_all` and `fetch_one`, at the top level or on the handle that `transaction()` yields:

```python {test=run prelude=reference}
from ferro import fetch_all

rows = await fetch_all("SELECT title FROM post WHERE views > ?", 1000)
assert rows == [{"title": "Notes on the Analytical Engine"}]
```

Rows come back as plain dicts of primitives. Raw SQL is an escape hatch, not a typed query path. See [Raw SQL](guide/raw-sql.md).

## Eager loading

Ferro has no `prefetch_related` or `select_related`. A forward foreign key loads in the same statement with `include()`:

```python {test=run prelude=reference}
posts = await (
    Post.where(lambda post: post.views > 100)
    .order_by(lambda post: post.views, "desc")
    .include(lambda post: post.author)
    .all()
)
assert [(post.title, post.author.name) for post in posts] == [
    ("Notes on the Analytical Engine", "Ada Lovelace"),
    ("The Education of a Computer", "Grace Hopper"),
]
```

Each `post.author` arrives populated, with no second query. Reverse and many-to-many collections are not populated yet, so awaiting `author.posts` in a loop issues one query per author (the N+1 pattern); see the [roadmap](roadmap.md#query-features). See [Queries](guide/queries.md#populating-relations-with-include) for the full contract.

## Speed

For CPU-bound ORM work (bulk inserts, hydrating large result sets, heavy concurrent query loads), Ferro is meaningfully faster than pure-Python ORMs, because that work runs in Rust with the GIL released. For network- or disk-bound work (single-row fetches, slow queries, remote databases), wait time dominates and any async ORM performs similarly. Ferro publishes no benchmark multipliers; measure your own workload. See [Performance](concepts/performance.md).

## Troubleshooting

### `Engine not initialized`

You ran a schema operation such as `ferro.create_tables()` before connecting. Importing models registers their schemas but does not open a database connection, so call `await ferro.connect(...)` during application startup, before the first call.

### `No database route for this operation`

You ran a query outside a session, with no `using=` or `session=`. Open a session around the work, as the first block on this page does, or route with `using=`. See [Connections › Sessions](guide/connections.md#sessions).

### `defines a relationship ... but ... does not have that field defined as BackRef()`

Relationships are declared on both sides and cross-validated when you connect. The full message reads `Model 'P' defines a relationship to 'A' with related_name='posts', but 'A' does not have that field defined as BackRef()/Field(back_ref=True)`. It has two common causes:

- The target model has no reverse field. A `ForeignKey(related_name="posts")` needs the target to declare `posts: Relation[list["Post"]] = BackRef()`, as `Author` does above.
- The model named in a forward reference (`"Author"`) was never imported, so it is not registered. Import every model module before `connect()`.

See [Relationships](guide/relationships.md).

### `ModelDoesNotExist`

`Model.get(pk)` raises `ferro.ModelDoesNotExist`, a `LookupError` subclass, when no row has that primary key. If a missing row is an expected case, use `Model.get_or_none(pk)`, which returns `None`:

```python {test=run prelude=reference}
from ferro import ModelDoesNotExist

try:
    await Post.get(99)
except ModelDoesNotExist as error:
    assert str(error) == "No Post record found for primary key 99"
else:
    raise AssertionError("expected ModelDoesNotExist")

assert await Post.get_or_none(99) is None
```

`get()` raises with the model and key in the message, and `get_or_none()` returns `None` for the same key.

## Reporting bugs and asking questions

Report bugs and request features on [GitHub Issues](https://github.com/syn54x/ferro-orm/issues).
