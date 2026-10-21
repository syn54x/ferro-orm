---
description: "How a database comes to match your models: auto-migrate for development, migration files for production."
---

# Overview of schema management

Schema management is how a database comes to match your models, which are the source of truth for its schema. Ferro gives you two doors for it:

- **Auto-migrate** changes the database straight from the models when you call `connect()`. No files, nothing to review.
- **Migrations** write each change down as a numbered directory of SQL and Python steps that you review, commit and apply. Ferro writes them for you from the difference between your models and the last migration.

Say you add one field:

=== "Assignment"

    ```python
    --8<-- "docs/examples/migrations_quickstart.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/migrations_quickstart_annotated.py:models"
    ```

With auto-migrate, the next `connect(url, migrate_updates=True)` runs `ALTER TABLE "author" ADD COLUMN "nickname" varchar` and creates its index, and carries on. With migrations, `ferro migrate new author_nickname` writes the same change into `migrations/0002_author_nickname/`, rendered once per database dialect you target (on Postgres the index builds `CONCURRENTLY`, in a step of its own), and `ferro migrate up` runs them when you say so.

## The ladder: `auto_migrate`, `migrate_updates`, migrations {#the-ladder}

| Rung | How you ask for it | What it does | Reach for it when |
| :--- | :--- | :--- | :--- |
| 1. Auto-create | `connect(url, auto_migrate=True)` | Creates missing tables. Never touches a table that exists. | Tests, scripts, a first prototype |
| 2. Auto-update | `connect(url, migrate_updates=True)`, optionally `migrate_destructive=True` | Also alters existing tables to match the models, as far as an in-place statement can. | Development while the schema is still moving |
| 3. Migrations | `ferro migrate new`, then `ferro migrate up` (`pip install "ferro-orm[cli]"`) | Reviewed, numbered files: schema steps, data steps, and a way back down. Covers every change the models can express, on Postgres and SQLite. | Any database whose data you would mind losing. **Recommended for production.** |

Rungs 1 and 2 are the first door ([Auto-migrate](auto-migrate.md)); each flag implies the ones before it. Rung 3 is the second door ([Migrations](migrations.md)).

!!! tip "Already on Alembic?"
    Ferro's [Alembic bridge](alembic.md) is a supported alternative to rung 3 when a project already runs Alembic or keeps SQLAlchemy tables beside its Ferro models. Its revisions come from the same planner auto-migrate uses. It does not write backfills or SQLite table rebuilds; see [what each door covers](#what-each-door-covers).

## One door per database

A database is managed by auto-migrate **or** by migrations, never both. Once a database has run a migration, it carries the tracking table `_ferro_migrations`, and `connect()` refuses every auto-migrate flag on it before running any DDL:

<!-- prints: migration_errors.py:tracked -->
```text
RunRefused: connect(auto_migrate=…) is refused: main is governed by ferro migrations (main._ferro_migrations). Use ferro migrate up, or drop the tracking tables to leave migrations.
```

Nothing has run, and the message names the two ways out. `main` is SQLite's schema; on Postgres the message names the schema the connection works in, such as `public`.

The rule is per database, not per project. A throwaway test database built with `connect(url, auto_migrate=True)` still works in a project that has migrations, because that database has never run one.

| You have | And you try | What happens |
| :--- | :--- | :--- |
| A database with migrations applied | `connect(..., auto_migrate=True)` (or either stronger flag), `create_tables()`, `migrate()` | Refused before any DDL, text above |
| A database with migrations applied | `alembic revision --autogenerate` over Ferro models | Refused, naming `ferro migrate new` ([Alembic](alembic.md#what-autogenerate-refuses)) |
| A database auto-migrate or Alembic built | `ferro migrate up` | Refused, naming `ferro migrate baseline` ([adopting migrations](adopting-migrations.md)) |
| A database auto-migrate built | `alembic revision --autogenerate` | Works: the first revision is empty when the database matches the models, since both doors plan with the same planner |

## What each door covers

The three doors plan with one planner, so a change two of them make leaves the same schema. They differ in which changes they make at all:

- **Auto-update** alters a table in place, as far as one statement can. It never drops a table, warns rather than rebuilding a SQLite table, and adds a required column to a table with rows only when the field has a literal default.
- **Migrations** cover every change the models can express, on Postgres and SQLite: [SQLite table rebuilds](migrations.md#sqlite-table-rebuilds), [backfills](data-steps.md), [renames](renames-and-retirements.md#rename-hints) and label removals. A primary-key change is refused with [the recipe](migrations.md#changing-a-primary-key).
- **The Alembic bridge** writes the same statements as a revision, refuses at autogenerate a change that needs a SQLite table rebuild, and leaves backfills to you.

The [door coverage table](../../reference/migrations.md#what-each-door-covers) lists every change, door by door.

## Choosing a door {#choosing}

- **Starting out, or writing tests:** `auto_migrate=True`. Tests keep this posture even in a project with migrations; see [Testing migrations](testing.md).
- **Developing, the schema still moving, the data disposable:** `migrate_updates=True`.
- **The first time you would mind losing the data:** `ferro migrate init`, then `ferro migrate new initial`. If the database already exists, follow [Adopting migrations on an existing database](adopting-migrations.md); it runs no DDL.
- **A server:** apply migrations in the deploy step and refuse to start behind them; see [Deploying migrations](deploying.md).
- **A local-first app that ships a SQLite file to its users:** migrations, applied at start-up with `await ferro.migrations.up()` ([Deploying migrations](deploying.md#apps-that-migrate-at-start-up)).

## See also

- [Auto-migrate](auto-migrate.md) · [Migrations](migrations.md) · [Data steps and backfills](data-steps.md) · [Deploying migrations](deploying.md) · [Testing migrations](testing.md) · [Alembic](alembic.md)
- [CLI reference](../../reference/cli.md) and [Configuration reference](../../reference/configuration.md)
- [Migrations API reference](../../api/migrations.md)
