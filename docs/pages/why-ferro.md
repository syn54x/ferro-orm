---
description: "What Ferro's Rust engine buys you, what it costs and when another ORM is the better choice."
---

# Why Ferro

Ferro is an async Python ORM that moves SQL generation, execution and row parsing into a Rust engine, so Python only builds a small filter description and receives finished model instances. This page explains what that buys, what it costs and when another ORM is the better choice.

Here is a query on a blog's `Post` model. It runs inside an `async with engines.session():` block (see [Sessions](guide/connections.md#sessions)):

```python {test=run prelude=reference}
popular = await Post.where(lambda post: post.views >= 1000).all()
assert [post.title for post in popular] == ["Notes on the Analytical Engine"]
```

Python builds the predicate from the lambda. Rust renders the parameterized SQL, runs it and hands back `Post` objects, here the one post with at least 1000 views.

## Where Python ORMs spend time

Python ORMs are convenient and carry a performance tax. SQLAlchemy, Django ORM and Tortoise spend CPU time in Python on SQL generation (query strings, escaping, JOINs), row parsing (turning rows into objects) and object instantiation (`__init__`, validators, attributes). All of it runs while holding the Global Interpreter Lock.

For simple CRUD this overhead is acceptable. When a request processes thousands of rows, a service runs many concurrent queries or tail latency matters, it becomes the bottleneck.

## Rust core

Sea-Query builds the SQL in Rust, and SQLx runs it and parses rows outside the GIL. Rust then fills each model's fields directly, without calling `__init__`, so rows do not pass through Pydantic's constructor. SQLite and PostgreSQL support is compiled into the engine, so there are no driver packages.

## Pydantic models

Ferro models are Pydantic models:

- Models inherit from `pydantic.BaseModel`.
- Validation runs in `pydantic-core`, which is also Rust.
- Type hints work as your IDE and type checker expect.
- There is no adapter layer between your database models and your API schemas.

If you already use FastAPI or another Pydantic-heavy stack, your database models and your validation models are the same objects.

## Async API

Ferro is built on `sqlx-core` and `pyo3-async-runtimes`:

- The async path runs from Rust to Python, with no sync wrappers or thread pools.
- SQLx handles connection pooling.
- Queries run concurrently without blocking the event loop.

## Trade-offs

Ferro is not the right choice for every project:

- Ferro supports Python 3.13 and newer only.
- The API is async-only. If your application is synchronous, such as classic Flask or scripts without an event loop, Ferro is a poor fit.
- The feature set is young. Ferro covers models, queries, mutations, relationships (including forward-FK population with `include()`), partial selects, grouped aggregations, transactions and migrations. Some features of mature ORMs are missing: `having()`, reverse and many-to-many collection population, and the `*`, `/` and `.concat()` operators in update expressions. The [Roadmap](roadmap.md) lists what is planned.
- The ecosystem is smaller: fewer integrations, plugins and Stack Overflow answers than SQLAlchemy or Django.
- Contributing to the engine, or building from source, needs a Rust toolchain. Using Ferro does not.

## Comparison with other ORMs

| | Ferro | SQLAlchemy 2.0 | Django ORM | Tortoise ORM |
|---|---|---|---|---|
| Core | Rust (SQLx and Sea-Query) | Python | Python | Python |
| Async support | Native, async-only | Native (opt-in) | Limited | Native |
| Sync API | No | Yes | Yes | No |
| Type safety | Pydantic models | Typed declarative API | Dynamic | Basic Pydantic integration |
| Migrations | Built in (`ferro migrate`), plus an Alembic bridge | Alembic | Built in | Aerich |
| Runtime dependencies | Pydantic and pydantic-settings | Several | Django | Several |
| Ecosystem maturity | Young | Very mature | Very mature | Moderate |
| Backends | SQLite, PostgreSQL | Many dialects | Many | Several |

Ferro keeps bulk reads, large result sets and row hydration in Rust and outside the GIL. For single-row operations, network and disk latency dominate and every ORM performs similarly, so choose on ergonomics and ecosystem rather than microbenchmarks.

## When to choose Ferro

Choose Ferro when:

- You are building async services on FastAPI, Starlette, Litestar or anything on asyncio.
- Your codebase is Pydantic-heavy and you want one model class for validation and persistence.
- You move many rows: data pipelines, bulk ingestion, read-heavy APIs.
- You want a small dependency footprint: Pydantic and pydantic-settings.
- You are on SQLite or PostgreSQL and Python 3.13 or newer.

Choose something else when:

- You need a sync API or Python older than 3.13.
- You need dialects beyond SQLite and PostgreSQL (MySQL, MSSQL, Oracle). Use SQLAlchemy.
- You work inside a Django project. The Django ORM is the pragmatic choice.
- You rely on features Ferro has not shipped. Check the [Roadmap](roadmap.md) first.
- You need maximum query flexibility for deeply complex SQL. SQLAlchemy Core is hard to beat.

Migrating from SQLAlchemy? Read [Migrate from SQLAlchemy](howto/migrate-from-sqlalchemy.md). Otherwise start with the [Quickstart](getting-started/quickstart.md).
