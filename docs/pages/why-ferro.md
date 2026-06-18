# Why Ferro?

## The Problem

Python ORMs are convenient, but they come with a performance tax. Traditional ORMs like SQLAlchemy, Django ORM, and Tortoise spend significant CPU time in Python code:

- **SQL generation** — building query strings, escaping values, assembling JOINs
- **Row parsing** — converting database rows into Python objects
- **Object instantiation** — calling `__init__`, running validators, populating attributes
- **GIL contention** — all of the above happens while holding the Global Interpreter Lock

For simple CRUD this overhead is acceptable. But when you process thousands of rows per request, run high-concurrency workloads, or care about tail latency in services, the Python tax becomes the bottleneck.

## How Ferro is Different

Ferro moves the expensive parts out of Python and into a Rust engine, connected to Python through a PyO3 FFI bridge.

### Rust Core

- **SQL generation**: Sea-Query builds parameterized SQL in Rust
- **Row hydration**: SQLx executes queries and parses rows GIL-free
- **Minimal copying**: data flows from database → Rust → Python with zero-copy intent
- **Bundled drivers**: SQLite and PostgreSQL support is compiled into the engine — no separate driver packages

When you call `User.where(lambda t: t.age >= 18).all()`, Python only builds a small filter AST. SQL generation, execution, and row parsing all happen in Rust; Python receives hydrated `User` objects at the end.

### Pydantic-Native

Unlike ORMs that wrap Pydantic or use it as a serialization layer, Ferro models *are* Pydantic models:

- Models inherit directly from `pydantic.BaseModel`
- Validation runs in `pydantic-core` (also Rust)
- Type hints work exactly as your IDE and type checker expect
- No adapter layer between your ORM models and your API schemas

If you already use FastAPI or any Pydantic-heavy stack, your database models and your validation models are the same objects.

### Async-First

Ferro is built on `sqlx-core` and `pyo3-async-runtimes`:

- True async from Rust to Python — no sync wrappers or thread pools
- Connection pooling handled by SQLx
- Concurrent query execution without blocking the event loop

## What You Give Up

Ferro is not the right choice for every project. Be honest with yourself about these trade-offs:

- **Python 3.13+ only.** Ferro targets modern Python and does not support older interpreters.
- **Async-only API.** There is no synchronous interface. If your application is sync (e.g., classic Flask or scripts without an event loop), Ferro is a poor fit.
- **Young feature set.** Ferro covers models, queries, mutations, relationships, transactions, and Alembic-based migrations — but some features common in mature ORMs are not implemented yet, including eager loading (`prefetch`/`select_related`), aggregations beyond `count()` and `exists()`, and partial column selects. See the [Roadmap](roadmap.md) for what's planned.
- **Smaller ecosystem.** Fewer third-party integrations, plugins, and Stack Overflow answers than SQLAlchemy or Django.
- **Rust at the bottom.** You never need Rust to *use* Ferro, but contributing to or extending the engine requires it, and building from source needs a Rust toolchain.

## Comparison

| | Ferro | SQLAlchemy 2.0 | Django ORM | Tortoise ORM |
|---|---|---|---|---|
| **Core** | Rust (SQLx + Sea-Query) | Python | Python | Python |
| **Async support** | Native, async-only | Native (opt-in) | Limited | Native |
| **Type safety** | Pydantic models | Typed declarative API | Dynamic | Basic Pydantic integration |
| **Learning curve** | Low | High | Low | Low |
| **Migrations** | Alembic (optional extra) | Alembic | Built-in | Aerich |
| **Runtime dependencies** | Pydantic only | Several | Django | Several |
| **Ecosystem maturity** | Young | Very mature | Very mature | Moderate |
| **Backends** | SQLite, PostgreSQL | Many dialects | Many | Several |

Ferro's architecture is designed to make bulk reads, large result sets, and row hydration fast by keeping that work in Rust and outside the GIL. For single-row operations, network and disk latency dominate and every ORM performs similarly — choose based on ergonomics and ecosystem, not microbenchmarks.

## When to Choose Ferro

Choose Ferro when:

- You're building **async services** — FastAPI, Starlette, Litestar, or anything on asyncio
- Your codebase is **Pydantic-heavy** and you want one model class for validation and persistence
- You move **lots of rows** — data pipelines, bulk ingestion, read-heavy APIs
- You want a **small dependency footprint** (Pydantic is the only runtime dependency)
- You're on **SQLite or PostgreSQL** and Python 3.13+

Choose something else when:

- You need a **sync API** or support for Python < 3.13
- You need **dialects beyond SQLite/PostgreSQL** (MySQL, MSSQL, Oracle) — use SQLAlchemy
- You're inside a **Django project** — the integrated Django ORM is the pragmatic choice
- You rely on features Ferro hasn't shipped yet — check the [Roadmap](roadmap.md) before committing
- You need **maximum query flexibility** for deeply complex SQL — SQLAlchemy Core is hard to beat

Migrating from SQLAlchemy? There's a [dedicated guide](howto/migrate-from-sqlalchemy.md). Otherwise, the best way to evaluate Ferro is the [Quickstart Tutorial](getting-started/quickstart.md) — it takes about 10 minutes.
