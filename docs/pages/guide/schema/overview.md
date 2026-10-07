# Schema Management

Your models are the source of truth for your schema. Ferro gives you two doors for getting a database to match them:

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

With auto-migrate, the next `connect(url, migrate_updates=True)` runs `ALTER TABLE "author" ADD COLUMN "nickname" varchar` and creates its index, and carries on. With migrations, `ferro migrate new author_nickname` writes those same statements into `migrations/0002_author_nickname/`, once per database dialect you target, and `ferro migrate up` runs them when you say so.

## The ladder

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

```text
connect(auto_migrate=…) is refused: main is governed by ferro migrations (main._ferro_migrations). Use ferro migrate up, or drop the tracking tables to leave migrations.
```

(`main` is SQLite's schema; on Postgres it names the schema the connection works in, such as `public`.)

The rule is per database, not per project. A throwaway test database built with `connect(url, auto_migrate=True)` still works in a project that has migrations, because that database has never run one.

| You have | And you try | What happens |
| :--- | :--- | :--- |
| A database with migrations applied | `connect(..., auto_migrate=True)` (or either stronger flag), `create_tables()`, `migrate()` | Refused before any DDL, text above |
| A database with migrations applied | `alembic revision --autogenerate` over Ferro models | Refused, naming `ferro migrate new` ([Alembic](alembic.md#what-autogenerate-refuses)) |
| A database auto-migrate or Alembic built | `ferro migrate up` | Refused, naming `ferro migrate baseline` ([adopting migrations](../../howto/adopting-migrations.md)) |
| A database auto-migrate built | `alembic revision --autogenerate` | Works: the first revision is empty when the database matches the models, since both doors plan with the same planner |

## What each door covers

| Change | Auto-update | Migrations | Alembic bridge |
| :--- | :--- | :--- | :--- |
| Add a table, a nullable column, an index, an enum label | ✅ | ✅ | ✅ |
| Add a check, a foreign key, change a type or nullability, on Postgres | ✅ | ✅ | ✅ |
| The same on SQLite (needs a table rebuild) | ⚠️ warns, no DDL | ✅ generated [table rebuild](migrations.md#sqlite-table-rebuilds) | ❌ refused at autogenerate |
| Rename a column (`renamed_from`) or an enum label (`__ferro_renamed_labels__`) | ✅ | ✅ [declared on the model](migrations.md#renames) | ✅ same hints |
| Rename a table (`__ferro_renamed_from__`) | ✅ `RENAME TABLE` under `migrate_updates`, indexes and checks renamed with it; without `migrate_updates` the pass warns and creates nothing | ✅ | ✅ |
| Drop a column | with `migrate_destructive` | ✅ marked `-- ferro: destructive` | ✅ marked `# ferro: destructive` |
| Drop a table | ❌ | ✅ marked `-- ferro: destructive` | ✅ marked `# ferro: destructive` |
| Add a new required column to a table with rows | only with a literal default (backfills existing rows) | ✅ [expand, backfill, contract](data-steps.md) generated | ⚠️ the plain op, marked `# ferro: data-dependent`; the backfill is yours to write |
| Make a nullable column required | Postgres: `SET NOT NULL`, fails if any row is `NULL` (write it as a migration with a backfill); SQLite: warns, no DDL | ✅ expand, backfill, contract generated | ⚠️ the plain op, marked `# ferro: data-dependent` |
| Remove an enum label rows may hold | warns, no DDL | ✅ [backfill and contract](data-steps.md#removing-an-enum-label) generated | ❌ not written; [Migrations](data-steps.md#removing-an-enum-label) generate it |
| Python data steps over the models as they were | ❌ | ✅ [data steps](data-steps.md) | hand-written `op.execute(...)` |
| Change a primary key | ❌ | refused, with [the recipe](migrations.md#changing-a-primary-key) | ❌ refused, with the same recipe |

## Choosing

- **Starting out, or writing tests:** `auto_migrate=True`. Tests keep this posture even in a project with migrations; see [Testing migrations](testing.md).
- **Developing, the schema still moving, the data disposable:** `migrate_updates=True`.
- **The first time you would mind losing the data:** `ferro migrate init`, then `ferro migrate new initial`. If the database already exists, follow [Adopting migrations on an existing database](../../howto/adopting-migrations.md); it runs no DDL.
- **A server:** apply migrations in the deploy step and refuse to start behind them; see [Deploying migrations](deploying.md).
- **A local-first app that ships a SQLite file to its users:** migrations, applied at start-up with `await ferro.migrations.up()` ([Deploying migrations](deploying.md#apps-that-migrate-at-start-up)).

## Where the old page went

This group replaces the single *Schema Migrations* page. Its sections now live here:

| Old section | Now |
| :--- | :--- |
| Three Ways to Manage Schema | [The ladder](#the-ladder) |
| Auto-Migration, `migrate_updates`, label addition, `migrate_destructive`, `migrate()`, safety guidance | [Auto-migrate](auto-migrate.md) |
| Alembic for Production | [Alembic](alembic.md) |
| Choosing a Workflow | [Choosing](#choosing) |

## See Also

- [Auto-migrate](auto-migrate.md) · [Migrations](migrations.md) · [Data steps and backfills](data-steps.md) · [Deploying migrations](deploying.md) · [Testing migrations](testing.md) · [Alembic](alembic.md)
- [CLI reference](../../reference/cli.md) and [Configuration reference](../../reference/configuration.md)
- [Migrations API reference](../../api/migrations.md)
