# Migrating to v0.12.0

`v0.12.0` is the first public release built on Ferro's IR-first architecture.
Query execution, schema/migration planning, codecs, hydration, and connection
routing now flow through one shared intermediate representation (IR) instead of
several independent code paths.

## Why this matters

A single source of truth removes whole classes of drift bugs:

- **Predictable schema diffs.** Runtime DDL and the Alembic autogenerate bridge
  derive from the same IR, so migrations stop proposing phantom changes.
- **Typed query execution.** Queries compile through typed IR rather than ad-hoc
  JSON, so bind and null semantics behave the same on SQLite and PostgreSQL.
- **Explicit runtime state.** Connection and transaction routing are scoped to a
  session you control, instead of hidden process-global state.

Your model definitions do not change. The migration below is about *how you
call* a few APIs, not how you declare your data.

## What you need to do

`v0.12.x` ships a **compatibility window**: the older call styles still work,
but each one now emits a `DeprecationWarning` whose message ends with
`Planned removal: v0.13.0`. Treat `v0.12.x` as your window to migrate before the
old surfaces are removed in `v0.13.0`.

Turn deprecation warnings into failures on a migration branch so nothing slips
through:

```bash
uv run pytest -W error::DeprecationWarning
```

Then work through the three changes below.

### 1. Use lambda predicates in `where()`

Operator-style predicates (`Model.field == value`) are deprecated. They never
type-checked cleanly — static checkers read `User.age >= 18` as a `bool`, while
`where()` expects a `QueryNode` — and they now warn at runtime. Lambda
predicates are the recommended style; `col()` is a type-safe bridge when you
want to keep the operator shape on a single comparison.

=== "Before (deprecated)"

    ```python
    adults = await User.where(User.age >= 18).all()
    admins = await User.where(User.role == "admin").all()
    ```

=== "After (recommended)"

    ```python
    adults = await User.where(lambda t: t.age >= 18).all()
    admins = await User.where(lambda t: t.role == "admin").all()
    ```

=== "After (`col()` bridge)"

    ```python
    from ferro.query import col

    adults = await User.where(col(User.age) >= 18).all()
    ```

### 2. Run operations inside a session

Calling unqualified operations (`User.all()`, `ferro.execute(...)`) without an
active session relied on implicit default-connection routing. That fallback is
deprecated. Wrap request- or task-scoped work in a session so routing, the
identity map, and transactions are explicit and isolated under concurrency.

=== "Before (deprecated)"

    ```python
    import ferro

    # Implicit default-connection routing (now warns).
    users = await User.where(lambda t: t.active == True).all()  # noqa: E712
    await ferro.execute("UPDATE users SET active = TRUE")
    ```

=== "After (ambient session)"

    ```python
    import ferro

    async with ferro.engines.session("app"):
        users = await User.where(lambda t: t.active == True).all()  # noqa: E712
        await ferro.execute("UPDATE users SET active = TRUE")
    ```

=== "After (explicit handle)"

    ```python
    import ferro

    async with ferro.engines.session("app") as session:
        users = await session.query(User).where(lambda t: t.active == True).all()  # noqa: E712
    ```

Need to target a specific connection from inside another session? Pass
`session=` explicitly — it overrides the ambient session.

### 3. Build Alembic metadata from `get_metadata()`

The private JSON-derivation helpers `ferro.migrations.alembic._build_sa_table`
and `ferro.migrations.alembic._map_to_sa_type` are deprecated. Schema metadata
now derives from the IR through the public `get_metadata()` entry point — use it
directly in your Alembic `env.py`.

=== "Before (deprecated)"

    ```python
    from ferro.migrations.alembic import _build_sa_table, _map_to_sa_type
    ```

=== "After (recommended)"

    ```python
    from ferro.migrations import get_metadata

    target_metadata = get_metadata()
    ```

## Deprecated surfaces at a glance

| Deprecated surface | Replacement | Removed in |
| --- | --- | --- |
| `Model.where(Model.field OP value)` | `where(lambda t: ...)` or `col(Model.field)` | `v0.13.0` |
| Unqualified ORM/raw operations outside an active session | `async with ferro.engines.session("name")` or explicit `session=` | `v0.13.0` |
| `ferro.migrations.alembic._build_sa_table` | `ferro.migrations.get_metadata()` | `v0.13.0` |
| `ferro.migrations.alembic._map_to_sa_type` | `ferro.migrations.get_metadata()` | `v0.13.0` |

## Verifying your migration

Once your call sites are updated, confirm no deprecation warnings remain on the
paths you exercise:

```bash
uv run pytest -W error::DeprecationWarning
```

A clean run means your codebase is ready for `v0.13.0`, where these
compatibility shims are removed.
