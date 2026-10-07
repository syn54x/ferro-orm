# Testing Migrations

Two kinds of test touch the schema, and they want different things:

- **Application tests** want a database shaped like the models, fast and fresh. Keep `connect(url, auto_migrate=True)` for them, as the [Testing how-to](../../howto/testing.md) sets up. A throwaway database that never ran a migration is not managed by migrations, so the [one-door rule](overview.md#one-door-per-database) does not refuse it.
- **Migration tests** want the database at a particular migration, rows seeded the way the table stood then, one migration applied, and the result read back. That is `ferro.migrations.testing.harness`.

Know the boundary of the first kind: a schema auto-migrate builds from today's models is the schema at the head of your migrations only while the two agree, which `ferro migrate check` proves in CI. It never exercises a migration's own SQL, its backfill, or its down. Those are what the harness is for.

## A migration test

Take a project whose `0002_author_slug` adds a required `slug` and backfills it:

=== "Assignment"

    ```python
    --8<-- "docs/examples/migrations_testing.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/migrations_testing_annotated.py:models"
    ```

The test stands the database at `0001`, seeds an author through `0001`'s historical models, applies exactly `0002`, and reads the row back through `0002`'s:

```python
--8<-- "docs/examples/migrations_testing.py:tests"
```

The fixture gives each test a fresh, empty database, and the test connects to it; the harness reads the project's configuration from the working directory, as every `ferro migrate` command does:

```python
import pytest


@pytest.fixture
def database_url(tmp_path):
    return f"sqlite:{tmp_path / 'test.db'}?mode=rwc"
```

For Postgres, give each test its own schema with `?ferro_search_path=...`, as [Testing Against Postgres](../../howto/testing.md#testing-against-postgres) shows, and run the same tests on both dialects your migrations target: the harness never skips one. Both tests run in [`docs/examples/migrations_testing.py`](https://github.com/syn54x/ferro-orm/blob/main/docs/examples/migrations_testing.py).

## The harness

`harness(*, settings=None, database=None, using=None)` binds the project's configuration (`FerroSettings()` by default), the configured database (the only one by default), and the open connection (the default one), and keeps no other state: every call reads where the database stands from its tracking table.

| Call | What it does |
| :--- | :--- |
| `await h.apply_through("0002")` | Applies every migration up to and including `0002`, from wherever the database stands. |
| `await h.apply("0002")` | Applies exactly `0002`; refused unless the database stands at its parent, `0001`. |
| `await h.revert_to("0001")` | Reverts down to `0001`, leaving it applied, with no prompt. |
| `await h.revert_all()` | Reverts every migration. |
| `async with h.models_at("0001") as models:` | The historical models of `0001`, to seed or read rows the way that migration left the tables. Takes no lock and changes nothing. |
| `await h.round_trip()` | Applies every migration, reverts every one, applies them again, and checks for drift after each stop; returns a `RoundTripResult`. |

Migrations are named by number (`"0002"`) or full name (`"0002_author_slug"`). There is no step-level target (`"0002:03"`): no snapshot describes the state between two steps. Every step runs through the same runner `ferro migrate up` uses, under its run lock (only the apply, revert and round-trip calls take it), and a call that cannot do exactly what it was asked raises `MigrationRefused` naming where the database stands and what was asked.

### The round trip

```python
result = await harness().round_trip()
assert result.irreversible is None
```

It proves every down reaches its parent. An irreversible step ends the downward walk at its migration, reported rather than failed:

```python
RoundTripResult(
    applied=["0001_create_author", "0002_add_slug"],
    reverted_to="0002_add_slug",
    irreversible=("0002_add_slug", "01_schema", "the index is shared"),
)
```

Everything above it round-tripped; everything below was applied only.

### What the harness does not do

It never makes a database fresh (that is your fixture's job) and has no `reset()`. It carries what the application API refuses on purpose (a target on the way up, a revert with no prompt, historical models outside a data step) because a test is about one migration and is deleted with it (ADR-0045). Those calls stay out of `ferro.migrations.up()`.

## See Also

- [Testing how-to](../../howto/testing.md) — fixtures for application tests
- [Data steps and backfills](data-steps.md) — historical models and backfills
- [Migrations API reference](../../api/migrations.md#the-test-harness)
