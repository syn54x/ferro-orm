# Adopting Migrations on an Existing Database

Your database already exists: `connect(url, migrate_updates=True)` built it, or an Alembic chain did. You want [Migrations](../guide/schema/migrations.md) from now on, without running any DDL on it and without losing a row. A **baseline** records migrations as applied on a database that already has their schema; it runs nothing, and it records only after checking.

## The four steps

1. **Bring the database to the old door's head.** One last start with `migrate_updates=True`, or `alembic upgrade head`, so it matches the models you are about to describe.
2. **Generate the first migration from the models.** `0001` comes from the models, never from the database:

    ```text
    $ ferro migrate init
    $ ferro migrate new initial
    migrations/0001_initial/
      01_schema.up.postgres.sql
      ...
    ```

    Commit it. Run `ferro migrate up` on a fresh database and you get the same schema the old door built.

3. **Baseline each existing database.**

    ```text
    $ ferro migrate baseline
    recorded 0001_initial as baseline (1 step)
    baseline checked what `ferro migrate drift` checks: column defaults and objects ferro does not own were not compared
    ```

    Without a target it records every migration in the directory; `ferro migrate baseline 0001` stops at `0001`. Data steps are listed and recorded, not run.

4. **Stop using the old door on that database.** Remove the auto-migrate flags (they are now refused on it), or stop generating Alembic revisions for Ferro's tables (autogenerate now refuses them). From here, `ferro migrate new` and `ferro migrate up`.

Until it is baselined, `ferro migrate up` refuses to run `0001` over tables that already exist:

```text
This database has no ferro migration records, but table "author" from 0001 already exists. If it was built by auto-migrate or Alembic, run "ferro migrate baseline 0001". up will not run 0001 over it.
```

## When baseline says no

Baseline compares the database with the target migration's schema snapshot exactly as `ferro migrate drift` does. Any difference and it records nothing, prints each one and exits 4:

```text
$ ferro migrate baseline 0001
drift against 0001_create_author:
  author.slug column is extra
nothing was recorded
```

Each line is a piece of work, and there is no flag that records past them (ADR-0031): either the database is not where you think it is, or the migration does not describe it. Fix whichever is wrong and baseline again:

- **The database is ahead of the target** (here, it already has the `slug` a later migration adds): baseline a later migration instead, `ferro migrate baseline` with no target or `ferro migrate baseline 0002`.
- **The database is behind the models**: finish step 1 (one more `migrate_updates` start or `alembic upgrade head`).
- **Someone changed the database by hand**: put it back, or declare the change on the models and generate a migration, then baseline that one.

Tables the snapshot does not declare (`alembic_version`, another application's tables) are never a difference. Column defaults and objects Ferro does not own are not compared, and the report says so.

## Databases at different points

Every database is baselined at the migration it actually matches. A production database at the head takes `ferro migrate baseline`; a staging copy restored from an older dump takes `ferro migrate baseline 0001`, then `ferro migrate up` applies the rest:

```text
$ ferro migrate baseline 0001 --url "$STAGING_URL"
recorded 0001_create_author as baseline (1 step)
...
$ ferro migrate up --url "$STAGING_URL"
0002_author_slug  01_expand  applied (0 ms)
...
```

`status` shows what each database was baselined at:

```text
$ ferro migrate status
default (sqlite) · main._ferro_migrations

0001_create_author  applied (baseline)
0002_author_slug    applied (baseline)
```

Baseline is refused on a database that already has migration records; `status` says where it stands.

## Coming from Alembic

The steps are the same, with Alembic as the old door:

- Step 1 is `alembic upgrade head`.
- In step 4, remove `get_metadata()` from `env.py`'s `target_metadata` once every database is baselined. Ferro's migrations ignore `alembic_version`; drop it when no database needs the Alembic chain any more.
- A project whose Alembic chain also manages its own SQLAlchemy tables keeps that chain for them: drop `get_metadata()` from `target_metadata` and keep `**ferro_options()`, which keeps Alembic off Ferro's tables and tracking tables. See [Alembic](../guide/schema/alembic.md).

## An app that migrates at start-up

A desktop or local-first app cannot baseline its users' files by hand. The release that switches to migrations does it in its start-up code: a file with no migration records is baselined at `0001` (the schema the previous release's auto-migrate built, if `0001` was generated from those models), and then `up()` applies the rest:

```python
import ferro
import ferro.migrations


async def start(database_url: str) -> None:
    await ferro.connect(database_url)
    report = await ferro.migrations.status()
    if all(migration.state == "pending" for migration in report.migrations):
        # Nothing recorded yet. A file the previous release built matches 0001
        # and is recorded; a brand-new, empty file matches nothing and stays
        # unrecorded, and up() creates it.
        try:
            await ferro.migrations.baseline(target="0001")
        except ferro.migrations.AlreadyTrackedError:
            pass  # another instance baselined it first
    await ferro.migrations.up()
```

The `try` covers a race. Two instances that start together (two windows of the app, or two services' pre-deploys against one shared database) both read "no records" and both call `baseline()`. The run lock lets one of them record. The other then finds a tracked database and raises `AlreadyTrackedError`, a `MigrationRefused` whose `applied` names the recorded migrations, `head` the newest of them and `report` the database's status. Nothing is wrong, so it carries on to `up()`. Every other refusal (a target the directory lacks, a lock wait that runs out) is still raised as `MigrationRefused`.

The baseline still checks the file. A file that matches neither (an even older build, or one edited by hand) is not recorded, and `up()` then refuses to run `0001` over its tables: the app fails loudly at start-up instead of serving a half-migrated file.

## Undoing a baseline

```text
$ ferro migrate baseline --remove
removed the baseline of 0001_create_author … 0002_author_slug
```

It deletes the records the baseline wrote, and nothing else: the migrations are pending again. It is refused while a migration a run applied stands above the baseline (revert that one with `ferro migrate down` first); in code, `remove_baseline()` raises `AppliedAboveBaselineError`, whose `above` names each such migration. `down` itself never reverts a baselined migration, whose down would drop tables it never created:

```text
ferro migrate: 0002 was recorded by `baseline` and created nothing here; `down` can go no lower than 0002. Nothing was reverted.
```

In code, `ferro.migrations.baseline()` and `ferro.migrations.remove_baseline()` do the same as the two commands.

## See Also

- [Schema Management overview](../guide/schema/overview.md) — one door per database
- [Deploying migrations](../guide/schema/deploying.md) — what comes after the baseline
- [CLI reference](../reference/cli.md#ferro-migrate-baseline-target)
