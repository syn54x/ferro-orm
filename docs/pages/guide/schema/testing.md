---
description: "Test a migration by seeding a database at the previous migration, applying the next one and reading the rows back."
---

# Testing migrations

A migration test stands a database at one migration, seeds rows the way the tables stood then, applies the next migration and reads the result back. Take a project whose `0002_author_slug` adds a required `slug` and backfills it from the name:

=== "Assignment"

    ```python
    --8<-- "docs/examples/migrations_testing.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/migrations_testing_annotated.py:models"
    ```

The test seeds an author through `0001`'s historical models, applies exactly `0002`, and reads the row back through `0002`'s:

```python
--8<-- "docs/examples/migrations_testing.py:tests"
```

`pytest` runs both tests and reports `2 passed`. A backfill that wrote the wrong value fails the first one at its `assert`; a down that does not reach its parent fails the second.

Two kinds of test touch the schema, and they want different things:

- **Application tests** want a database shaped like the models, fast and fresh. Keep `connect(url, auto_migrate=True)` for them, as the [Testing how-to](../../howto/testing.md) sets up. A throwaway database that never ran a migration is not managed by migrations, so the [one-door rule](overview.md#one-door-per-database) does not refuse it.
- **Migration tests** want the database at a particular migration. That is `ferro.migrations.testing.harness`.

A schema auto-migrate builds from today's models matches the head of your migrations only while the two agree, which `ferro migrate check` proves in CI. It never runs a migration's own SQL, its backfill or its down; the harness does.

## The fixture and pytest settings

Each test gets a fresh, empty database from a fixture in `conftest.py`, and connects to it itself:

```python
--8<-- "docs/examples/migrations_testing.py:fixture"
```

The fixture closes the connection after the test, so the next test can connect again. The harness reads the project's configuration from the working directory, as every `ferro migrate` command does, so run `pytest` from the project root.

The tests are `async def`. With pytest-asyncio, set its mode to `auto` in `pyproject.toml` so it runs them without a decorator each:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

Without it, pytest-asyncio runs only the tests marked `@pytest.mark.asyncio`. [Configuring pytest-asyncio](../../howto/testing.md#configuring-pytest-asyncio) covers the other settings. The fixture, the tests and these settings run as written in [`docs/examples/migrations_testing.py`](https://github.com/syn54x/ferro-orm/blob/main/docs/examples/migrations_testing.py).

For Postgres, give each test its own schema with `?ferro_search_path=...`, as [Testing Against Postgres](../../howto/testing.md#testing-against-postgres) shows, and run the same tests on both dialects your migrations target: the harness never skips one.

## The harness

`harness(*, settings=None, database=None, using=None)` binds the project's configuration (`FerroSettings()` by default), the configured database (the only one by default) and the open connection (the default one). It keeps no other state: every call reads where the database stands from its tracking table.

| Call | What it does |
| :--- | :--- |
| `await migrations.apply_through("0002")` | Applies every migration up to and including `0002`, from wherever the database stands. |
| `await migrations.apply("0002")` | Applies exactly `0002`; refused unless the database stands at its parent, `0001`. |
| `await migrations.revert_to("0001")` | Reverts down to `0001`, leaving it applied, with no prompt. |
| `await migrations.revert_all()` | Reverts every migration. |
| `async with migrations.models_at("0001") as models:` | The historical models of `0001`, to seed or read rows the way that migration left the tables. Takes no lock and changes nothing. |
| `await migrations.round_trip()` | Applies every migration, reverts every one, applies them again, and checks for drift after each stop; returns a `RoundTripResult`. |

Migrations are named by number (`"0002"`) or full name (`"0002_author_slug"`). There is no step-level target (`"0002:03"`): no snapshot describes the state between two steps. Every step runs through the same runner `ferro migrate up` uses, under its run lock (only the apply, revert and round-trip calls take it). A call that cannot do exactly what it was asked raises `MigrationRefused`, naming where the database stands and what was asked. Each call has its entry in the [API reference](../../api/migrations.md#the-test-harness).

### The round trip

`round_trip()` walks the whole lineage in one call and proves every down reaches its parent. An irreversible step ends the downward walk at its migration, reported rather than failed. Declare the backfill's down `@irreversible("the old slugs are gone")`, and the round trip stops there:

```python
--8<-- "docs/examples/migrations_testing.py:irreversible"
```

The walk down stopped at `0002_author_slug`, which stays applied, so `0001_create_author` below it was applied but never reverted. A migration above the irreversible one would still have round-tripped.

### What the harness does not do

It never makes a database fresh (that is your fixture's job) and has no `reset()`. It carries what the application API refuses on purpose: a target on the way up, a revert with no prompt, historical models outside a data step. A test is about one migration and is deleted with it, so those calls stay out of `ferro.migrations.up()`.

## See also

- [Testing how-to](../../howto/testing.md): fixtures for application tests
- [Data steps and backfills](data-steps.md): historical models and backfills
- [Migrations API reference](../../api/migrations.md#the-test-harness): `harness`, `Harness`, `RoundTripResult`
