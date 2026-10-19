---
description: "Move a database that auto-migrate or Alembic built onto Ferro's migrations without running any DDL on it."
---

# Adopting migrations on an existing database

This how-to moves a database that `connect(url, migrate_updates=True)` or an Alembic chain built onto [Migrations](migrations.md), without running any DDL on it and without losing a row. The tool is a **baseline**: it records migrations as applied on a database that already has their schema. It runs nothing, and it records only after checking.

## The four steps

1. **Bring the database to the old door's head.** One last start with `migrate_updates=True`, or `alembic upgrade head`, so it matches the models you are about to describe.
2. **Generate the first migration from the models.** `0001` comes from the models, never from the database:

    ```bash
    ferro migrate init
    ferro migrate new create_author
    ```

    `new` prints the files it wrote:

    <!-- output: adopt-new-initial -->
    ```text
    default · migrations
    ok Wrote migrations/0001_create_author/
      01_schema.up.postgres.sql    schema step, up (postgres)
      01_schema.down.postgres.sql  schema step, down (postgres)
      01_schema.up.sqlite.sql      schema step, up (sqlite)
      01_schema.down.sqlite.sql    schema step, down (sqlite)
      ir.json                      snapshot

      1 new model
        Author
    ```

    Commit it. Run `ferro migrate up` on a fresh database and you get the same schema the old door built.

3. **Baseline each existing database.** Point `DATABASE_URL` at it and record the migrations:

    ```bash
    ferro migrate baseline
    ```

    `baseline` checks the database against the migrations, then records them:

    <!-- output: adopt-baseline -->
    ```text
    default · sqlite:///./app.db
    recorded 0001_create_author as baseline (1 step)
    baseline checked what `ferro migrate drift` checks: objects ferro does not own (functions, triggers) were not compared
    ```

    Without a target it records every migration in the directory; `ferro migrate baseline 0001` stops at `0001`. Data steps are listed and recorded, not run.

4. **Stop using the old door on that database.** Remove the auto-migrate flags (they are now refused on it), or stop generating Alembic revisions for Ferro's tables (autogenerate now refuses them). From here, `ferro migrate new` and `ferro migrate up`.

Until it is baselined, `ferro migrate up` refuses to run `0001` over tables that already exist:

<!-- output: adopt-up-refused -->
```text
default · sqlite:///./app.db
This database has no ferro migration records, but table "author" from 0001 already exists. If it was built by auto-migrate or Alembic, run "ferro migrate baseline 0001". up will not run 0001 over it.
```

## Baseline refusals on drift {#when-baseline-says-no}

Baseline compares the database with the target migration's schema snapshot exactly as `ferro migrate drift` does. On any difference, `ferro migrate baseline 0001` records nothing, prints each one and exits 4:

<!-- output: baseline-drift -->
```text
default · sqlite:///./app.db
drift against 0001_create_author:
  author.slug column is extra
nothing was recorded
```

Each line is a piece of work, and no flag records past them: a baseline recorded over a difference would leave the next migration to run against a schema it was not written for. Either the database is not where you think it is, or the migration does not describe it. Fix whichever is wrong and baseline again:

- **The database is ahead of the target** (here, it already has the `slug` a later migration adds): baseline a later migration instead, `ferro migrate baseline` with no target or `ferro migrate baseline 0002`.
- **The database is behind the models**: finish step 1 (one more `migrate_updates` start or `alembic upgrade head`).
- **Someone changed the database by hand**: put it back, or declare the change on the models and generate a migration, then baseline that one.
- **A column has a server default nobody declared** (a *foreign default*; Ferro persists none of its own):

    On SQLite a literal default is not reported; a default that is an expression is, as here:

    <!-- output: baseline-foreign-default -->
    ```text
    default · sqlite:///./app.db
    drift against 0001_create_author:
      author.slug has default lower('unknown') in the database, none declared
    nothing was recorded
    ```

    Drop it, then baseline again. Either give the old door one last start that removes what the models never declared, `connect(url, migrate_destructive=True)`, or drop it by hand on Postgres:

    ```sql
    ALTER TABLE "author" ALTER COLUMN "slug" DROP DEFAULT;
    ```

    The column then has no default, and the next baseline records. On SQLite neither works: SQLite drops a default only by rebuilding the table, so rebuild it without the `DEFAULT` by hand (create a copy, copy the rows across, drop the table, rename the copy).

Three things are never a difference:

- Tables the snapshot does not declare: `alembic_version`, another application's tables.
- The two server defaults Ferro leaves itself: a `serial` key's sequence on Postgres (recognised by the sequence the column owns, so a renamed table's still counts), and on SQLite a literal default on a `NOT NULL` column, which `migrate_updates` keeps when it adds a required column.
- Objects Ferro does not own, such as functions and triggers. They are not compared, and the report says so.

## Databases at different points

Every database is baselined at the migration it actually matches. A production database at the head takes `ferro migrate baseline`; a staging copy restored from an older dump takes `ferro migrate baseline 0001`, then `ferro migrate up` applies the rest:

With `STAGING_URL` naming the staging copy (`sqlite:staging.db` here), `ferro migrate baseline 0001 --url "$STAGING_URL"` records the one migration it matches:

<!-- output: staging-baseline -->
```text
default · sqlite:///./staging.db
recorded 0001_create_author as baseline (1 step)
baseline checked what `ferro migrate drift` checks: objects ferro does not own (functions, triggers) were not compared
```

`ferro migrate up --url "$STAGING_URL"` then applies the rest:

<!-- output: staging-up -->
```text
default · sqlite:///./staging.db
0002_author_slug  01_schema  applied (0 ms)
```

`ferro migrate status` on a production database baselined at the head shows both migrations as `applied (baseline)`:

<!-- output: status-baselined -->
```text
default · sqlite:///./app.db
ok 2 applied  main._ferro_migrations
  ok 0001_create_author  applied (baseline)  2026-10-15 04:30Z
  ok 0002_author_slug    applied (baseline)  2026-10-15 04:30Z
```

The staging copy shows `0001_create_author` as `applied (baseline)` and `0002_author_slug` as `applied`. Baseline is refused on a database that already has migration records; `status` says where it stands.

## Coming from Alembic

The steps are the same, with Alembic as the old door:

- Step 1 is `alembic upgrade head`.
- In step 4, remove `get_metadata()` from `env.py`'s `target_metadata` once every database is baselined. Ferro's migrations ignore `alembic_version`; drop it when no database needs the Alembic chain any more.
- A project whose Alembic chain also manages its own SQLAlchemy tables keeps that chain for them: drop `get_metadata()` from `target_metadata` and keep `**ferro_options()`, which keeps Alembic off Ferro's tables and tracking tables. See [Alembic](alembic.md).

## An app that migrates at start-up

A desktop or local-first app cannot baseline its users' files by hand. The release that switches to migrations does it in its start-up code: a file with no migration records is baselined at `0001` (the schema the previous release's auto-migrate built, if `0001` was generated from those models), and then `up()` applies the rest:

```python
--8<-- "docs/examples/migrations_adopting.py:recipe"
```

A file the previous release built matches `0001` and is recorded. A brand-new, empty file matches nothing, so the baseline records nothing and `up()` creates the tables from `0001`.

`baseline()` itself decides whether the file still needs adopting. On a tracked file (an earlier start recorded it, or `up()` has run) it records nothing and raises `AlreadyTrackedError`, a `MigrationRefused` whose `applied` names the recorded migrations, `head` the newest of them and `report` the file's status. Nothing is wrong, so the app carries on to `up()`.

This also covers a race. Two instances that start together (two windows of the app, or two services' pre-deploys against one shared database) both find no records and both call `baseline()`. The run lock lets one of them record, and the other raises `AlreadyTrackedError`. Do not check `status()` first to decide whether to call `baseline()`: the answer can change before the baseline takes the lock. Every other refusal (a target the directory lacks, a lock wait that runs out) is still raised as `MigrationRefused`.

The baseline still checks the file. A file that matches neither (an even older build, or one edited by hand) is not recorded: on drift, `baseline()` raises nothing and returns a [`BaselineReport`](../../api/migrations.md#ferro.migrations.BaselineReport) whose `drift` lists the differences. `up()` then refuses to run `0001` over the file's tables, so the app fails loudly at start-up instead of serving a half-migrated file. To fail at the baseline instead, keep the report and call `report.raise_for_problems()`.

## Undoing a baseline

`ferro migrate baseline --remove` prints:

<!-- output: baseline-remove -->
```text
default · sqlite:///./app.db
removed the baseline of 0001_create_author … 0002_author_slug
```

It deletes the records the baseline wrote, and nothing else: the migrations are pending again. It is refused while a migration a run applied stands above the baseline (revert that one with `ferro migrate down` first); in code, `remove_baseline()` raises `AppliedAboveBaselineError`, whose `above` names each such migration. `down` itself never reverts a baselined migration, whose down would drop tables it never created:

<!-- output: down-baselined-refused -->
```text
default · sqlite:///./app.db
ferro migrate: 0002 was recorded by `baseline` and created nothing here; `down` can go no lower than 0002. Nothing was reverted.
```

In code, `ferro.migrations.baseline()` and `ferro.migrations.remove_baseline()` do the same as the two commands.

## See also

- [Schema management overview](overview.md): one door per database
- [Deploying migrations](deploying.md): what comes after the baseline
- [CLI reference](../../reference/cli.md#ferro-migrate-baseline-target) and [Migrations API reference](../../api/migrations.md#ferro.migrations.baseline): every flag and parameter of `baseline`
