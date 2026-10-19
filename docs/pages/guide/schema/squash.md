# Squashing a feature branch's migrations

`ferro migrate squash` replaces a range of migrations, from the one you name through the newest, with the one migration `new` would have written across them. A feature branch collects one migration per task. Main stands at `0001_create_author`, the [Migrations](migrations.md) loop's `Author`, and the teams branch has added four on top of it:

- `0002_add_teams` creates `Team`, with a `tmp_code` column one task needed for a while;
- `0003_team_slug` adds a required `slug`, with the backfill `02_backfill_team.py` written by hand;
- `0004_drop_tmp` drops `tmp_code` again;
- `0005_fix_rows` is a hand-written SQL step, `fix_rows`, that trims team names.

Four migrations, three of them `ALTER TABLE`s on a table the first one created, are the history of the work, not the change main needs. Before the branch's pull request to main, replace them with one. `ferro migrate squash teams --from 0002` writes:

<!-- output: squash-teams -->
```text
default · migrations
ok Wrote migrations/0002_teams/  (replaces 0002_add_teams … 0005_fix_rows, now deleted)
  01_schema.up.postgres.sql    schema step, up (postgres)    regenerated
  01_schema.down.postgres.sql  schema step, down (postgres)  regenerated
  01_schema.up.sqlite.sql      schema step, up (sqlite)      regenerated
  01_schema.down.sqlite.sql    schema step, down (sqlite)    regenerated
  02_fix_rows.up.sql           SQL step, up                  carried, review it (was 0005_fix_rows/01_fix_rows.up.sql)
  02_fix_rows.down.sql         SQL step, down                carried, review it (was 0005_fix_rows/01_fix_rows.down.sql)
  03_backfill_team.py          data step                     carried, review it (was 0003_team_slug/02_backfill_team.py)
  ir.json                      snapshot                      0005_fix_rows's, chained from 0001_create_author, replaces 0002_add_teams … 0005_fix_rows

  1 new model
    Team
  - team.tmp_code existed only inside the range (0002_add_teams through 0004_drop_tmp): review the carried steps for it
```

`0002_teams` creates `team` with its `slug` in one `CREATE TABLE`, and `tmp_code` never appears. The four directories are gone.

## What is regenerated

The DDL, every file with a dialect suffix, is planned once, from `0001_create_author`'s snapshot to `0005_fix_rows`'s, by the planner `new` uses. A squash writes exactly the files `new` would write across the range.

A squash reads those snapshots and the migrations directory. It never imports the models, so a model change no migration records is still `new`'s to write, and it never opens a database (`--url` is refused). Its `ir.json` is the last snapshot under a new parent, and it records which migrations it replaced: the squash's *witness*.

## What is carried

Every data step and hand-written SQL step of the range keeps its body byte for byte, in chain order, placed as `--replace` places it (a SQL step before the data steps, a data step before the contract). Each is marked `carried, review it` with the file it was.

Read each one. Here `fix_rows` now runs before the backfill it used to follow, and a step written after one task's contract now runs before the one contract.

A backfill still byte for byte as the generator scaffolded it, `todo(...)` and all, that the squashed plan does not scaffold again is dropped with a note. Had `02_backfill_team.py` been left unwritten, the note would read `02_backfill_team.py from 0003_team_slug was the generator's unwritten scaffold and the squashed plan needs no backfill of team.slug: dropped`, since a `slug` on a table the squash creates needs no backfill. A step with any hand-written byte is always carried, and a carried unwritten step still refuses to load until it is written.

## Names that existed only inside the range

A table, column, enum label or constraint the range added and then removed (`team.tmp_code` here) does not exist in the squashed migration, so a carried step that touches it now fails. Where a carried step's declaration proves it, the squash refuses and writes nothing: a `@chunked` step whose query names the dead name in its model, order keys or filter columns or labels. Had `02_backfill_team.py` selected its rows by `tmp_code` (`lambda team: team.tmp_code == None`), the squash would say:

<!-- output: squash-dead-name -->
```text
default · migrations
ferro migrate squash: 0003_team_slug/02_backfill_team.py names team.tmp_code, which existed only inside the range (added by 0002_add_teams, removed by 0004_drop_tmp): delete the step, or start the range at 0004_drop_tmp, the migration that removed it (pass --from 0004_drop_tmp). Nothing was written.
```

Nothing was written, and the refusal names both ways on. An `@atomic` step declares nothing the squash can read, and a SQL step is SQL, so neither is ever refused: each dead name gets the note in the first output instead, and reading the carried steps for it is yours.

## The range

- The range is always a tail, `--from` through the newest migration, because the migration after a middle range would chain from a snapshot that no longer exists.
- The squash takes the first migration's number and the name you give it.
- A name that would rewrite a replaced migration's own directory (`ferro migrate squash add_teams --from 0002` writing `0002_add_teams`) is refused, since every reader would then take the squash for the original.
- A range that adds nothing and carries nothing is refused: delete the directories.
- A migration that shares the first number of the range, because two branches each generated it, is refused too: resolve the shared number with [`--replace`](migrations.md#duplicate-migration-numbers-and-replace) before squashing.

The [CLI reference](../../reference/cli.md#ferro-migrate-squash-name-from-target) lists every flag and refusal, and what to do with the hidden directory a squash killed halfway leaves.

## Renames inside the range

A column renamed in one task, whose hint a later task deleted, is still a rename after the squash. Say the branch's first task renamed `nickname` to `handle` and generated `0002_rename_handle`:

=== "Assignment"

    ```python
    --8<-- "docs/examples/migrations_squash.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/migrations_squash_annotated.py:models"
    ```

Its migration generated, the hint is inert, so the second task deleted it and added `Team` (`0003_add_teams`):

=== "Assignment"

    ```python
    --8<-- "docs/examples/migrations_squash.py:later"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/migrations_squash_annotated.py:later"
    ```

Planned from `0001`'s snapshot to `0003`'s models alone, `nickname` would be dropped and `handle` added, and every author's nickname lost. The squash composes the hints of every snapshot in the range into its own, so `ferro migrate squash teams --from 0002` writes one rename:

```sql
-- 0002_teams/01_schema.up.postgres.sql
ALTER TABLE "author" RENAME COLUMN "nickname" TO "handle";

ALTER INDEX "idx_author_nickname" RENAME TO "idx_author_handle";

CREATE TABLE IF NOT EXISTS "team" ( "id" serial PRIMARY KEY NOT NULL, "name" varchar NOT NULL, "slug" varchar NOT NULL );
```

The column and its index are renamed, and every author keeps a nickname, now under `handle`. Renames compose:

- `a→b` then `b→c` is `a→c`.
- A renamed column the range later drops is a drop of the old name.
- A column added and then renamed is an add of the new name.
- A rename whose old name a later task adds again cannot be one migration. It is refused, naming the `--from` that starts the range at the task that adds it again, which leaves the rename below the squash.

The example `docs/examples/migrations_squash.py` in the repository runs this end to end, inheritance and revert included.

## Inheritance on databases that ran the originals

Your development database applied `0002_add_teams` through `0005_fix_rows`. After you pull the squash, `ferro migrate status` shows the one migration it has no records for, and what `up` will do with it:

<!-- output: squash-status-inheritable -->
```text
default · sqlite:///./app.db
-- 1 pending  main._ferro_migrations
  ok 0001_create_author  applied  2026-10-15 04:31Z
  -- 0002_teams          pending (inheritable)  run `ferro migrate up`; this database applied 0002_add_teams … 0005_fix_rows and nothing will be run
```

`0002_teams` is pending and *inheritable*. `ferro migrate up` runs nothing:

<!-- output: squash-up-inherits -->
```text
default · sqlite:///./app.db
inherited 0002_teams: this database had applied 0002_add_teams … 0005_fix_rows; nothing was run
```

That is *inheritance*. The run reads the tracking table and finds a finished record for every step of every migration the squash's witness names, each under the snapshot the witness names. It swaps those records for the squash's own, in one transaction, and `status` shows `0002_teams` as `applied (inherited)`.

- There is no flag and no prompt: the committed squash is the consent.
- Nothing checks the live schema (`ferro migrate drift` is the verb for doubting it). The records already say the database stands at the snapshot the squash ends at, and its rows are kept.
- `down` inherits the same way before it reverts, and an inherited step's down runs as a run's does.

Until that `up`, the squashed migration is pending. `require_applied()` refuses to start with `PendingMigrationsError`, whatever `allow_ahead` says, because a migration the database has no record of is pending, never ahead. Each developer's database, and the branch's integration database, meets the squash once and runs `ferro migrate up`.

A database that applied only part of the range (`0002` through `0004`, not `0005_fix_rows`) cannot be inherited: it does not stand at the squash's end. `up` and `down` refuse it, and `status` reports the same refusal under `needs attention`, naming both ways out:

<!-- output: squash-partial-witness -->
```text
default · sqlite:///./app.db
ferro migrate: 0002_teams replaces migrations this database applied only part of: it applied 0002_add_teams, 0003_team_slug and 0004_drop_tmp but not 0005_fix_rows, so it cannot inherit 0002_teams. To keep its rows, check out the commit that holds 0005_fix_rows, run `ferro migrate up` there to finish it, then run `ferro migrate up` here, which inherits 0002_teams. Or revert instead: on the commit that applied them run `ferro migrate down --to 0001`, then `ferro migrate up` here; the revert drops what they created and fails on an irreversible step. Nothing was changed.
```

Take the first if you can: finishing the range keeps every row. The second drops the branch's tables and their rows, and stops at an irreversible step.

## Squash timing

A squash rewrites the history of the branch's own migrations, the way a rebase rewrites its commits. Do it once, before the pull request to main, not each time a task lands on the integration branch. The databases that applied the originals are your team's development databases, and each inherits the squash once. A production database never meets a squash, because the migrations it runs are the ones main already holds.

!!! note "Older ferro releases"
    A ferro release from before `squash` cannot read a database that inherited a squash. Read [squashes and older releases](deploying.md#squashes-and-older-releases) before you roll one out beside them.

## `squash` vs `--replace`

Both regenerate migrations and carry their hand-written steps; they answer different situations:

| | `ferro migrate squash NAME --from …` | `ferro migrate new NAME --replace …` |
| :--- | :--- | :--- |
| Plans | from the snapshot below the range to the range's last snapshot | from the new parent's snapshot to the models |
| Replaces | a tail of migrations, `--from` through the newest | the newest migration |
| A database that applied the originals | inherits the squash, running nothing | reverts with the files that went up, then runs the new migration ([duplicate migration numbers](migrations.md#duplicate-migration-numbers-and-replace)) |

A squash keeps what the range did and shortens it; `--replace` puts your migration back on top of someone else's. The [CLI reference](../../reference/cli.md#ferro-migrate-squash-name-from-target) lists every flag of both.

## See also

- [Migrations](migrations.md): the loop, and `--replace` for two branches that share a number
- [Data steps and backfills](data-steps.md): the steps a squash carries
- [CLI reference](../../reference/cli.md#ferro-migrate-squash-name-from-target): every `squash` flag and refusal
