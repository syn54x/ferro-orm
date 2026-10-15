# CLI

The `ferro` command ships with the core package; its command line needs the `cli` extra:

```bash
pip install "ferro-orm[cli]"
```

Without the extra, `ferro` prints `ferro's CLI needs the cli extra: pip install "ferro-orm[cli]"` and exits 2. Every verb lives under `ferro migrate`; the guides show them in use ([Migrations](../guide/schema/migrations.md), [Deploying](../guide/schema/deploying.md), [Adopting](../howto/adopting-migrations.md)).

## Global options

Given before or after the verb (`ferro --database billing migrate up` or `ferro migrate up --database billing`):

| Option | What it does |
| :--- | :--- |
| `--config PATH` | The config file to read, in place of the [lookup](configuration.md#lookup-order). |
| `--database NAME` | Which configured database the verb acts on. Needed when several are configured. |
| `--url URL` | The database URL, in place of the variable `url_env` names (`DATABASE_URL` by default). Refused by the verbs that never connect (`init`, `new`, `check`). |
| `--version`, `--help` | The installed version; the help for a verb. |

The environment: `FERRO_CONFIG` names the config file (as `--config` does), and the URL is read from the variable each database's `url_env` names.

### What the output looks like

Every verb but `init` opens with one line naming the database it acts on and where that is. The verbs that connect print the database's name and its URL with any password masked (`default · sqlite:///./app.db`). `new` and `check` never connect, so they name the migrations directory instead (`default · migrations`). Everything under that line is the result.

Ferro prints each result in one of two forms, chosen by where it is printing. The blocks on this page are the **plain form**, which is what a pipe, a file or a CI log gets: ASCII markers (`ok` done, `!!` needs a look, `xx` failed or refused, `--` pending, `~~` half applied), one item to a line, nothing collapsed and no colour. A terminal gets the same words with Unicode markers, colour and some folding, which each verb's section below describes. Every refusal sentence, and every `warning:` line on stderr, is the same in both forms.

| Variable | What it does |
| :--- | :--- |
| `NO_COLOR` | On a terminal, keeps the terminal layout and its Unicode markers and strips every colour escape. |
| `FORCE_COLOR` | Prints the terminal form (colour and Unicode markers) even when the output is not a terminal. |
| `COLUMNS` | The width to lay output out in when there is no terminal to ask. |

A stream that is not UTF-8 gets ASCII everywhere: `·` becomes `-`, `→` becomes `->` and `…` becomes `...`.

## Exit codes

Every verb shares them, and a value never changes meaning:

| Code | Name | Meaning |
| :--- | :--- | :--- |
| 0 | `OK` | The command did what it was asked, or there was nothing to do. |
| 1 | `REFUSED` | The command refused, naming the fix, or a step failed. |
| 2 | `USAGE` | The command was invoked wrongly (an unknown verb or flag, a missing argument), or the `cli` extra is not installed. |
| 3 | `PENDING` | `status`: migrations are waiting to be applied. `check`: a problem in the models or the directory. |
| 4 | `NEEDS_ATTENTION` | `status`, `drift` and `baseline`: a failed or reverting step, an edited file, a database ahead of the checkout, drift; a person has to look before anything runs. |

They are importable as `ferro.cli.exit_codes.OK`, `REFUSED`, `USAGE`, `PENDING` and `NEEDS_ATTENTION`.

## `ferro migrate init`

Set a project up: write its ferro config and create its migrations directory. Asks for what the flags do not answer; with no terminal to ask on, it refuses naming the flag that answers the question.

| Flag | What it does |
| :--- | :--- |
| `--config-file PATH` | The file to write: `pyproject.toml` (gets a `[tool.ferro]` table) or `ferro.toml`. Default: `pyproject.toml` when the project has one. |
| `--models MODULES` | The dotted module(s) defining the models, comma-separated. |
| `--dialects DIALECTS` | The target dialects, comma-separated: `postgres`, `sqlite`. |
| `--directory PATH` | Where migrations live (default `migrations/`; with several databases, one subdirectory each). |
| `--database NAME` | Names the one database configured from flags. Interactively, answer `y` to "Another database?" to configure several. |

With the flags given, `init` asks nothing and prints what it wrote. It prints no database line, since it neither connects nor reads a configuration:

<!-- output: init -->
```text
models = ["blog.models"]
dialects = ["postgres", "sqlite"]
Wrote ferro.toml and created migrations/
```

Refused: a project that already has a ferro config (in either file; `init` never rewrites one), a directory holding an Alembic environment, `--url`, `--config`. Appends to an existing `pyproject.toml` as text, so the rest of the file is never reformatted.

## `ferro migrate new NAME`

Write the next migration from the models' difference with the last one. Diffs against the newest migration's schema snapshot, never a database. Prints the files it wrote, what changed, and a line per thing to do; prints `no schema change: nothing written` when nothing renders DDL.

An `Author` gains an indexed `nickname`, and `ferro migrate new author_nickname` writes:

<!-- output: new-small -->
```text
default · migrations
ok Wrote migrations/0002_author_nickname/
  01_schema.up.postgres.sql                 schema step, up (postgres)
  01_schema.down.postgres.sql               schema step, down (postgres)
  01_schema.up.sqlite.sql                   schema step, up (sqlite)
  01_schema.down.sqlite.sql                 schema step, down (sqlite)
  02_idx_author_nickname.up.postgres.sql    schema step, up (postgres)
  02_idx_author_nickname.down.postgres.sql  schema step, down (postgres)
  02_idx_author_nickname.up.sqlite.sql      schema step, up (sqlite)
  02_idx_author_nickname.down.sqlite.sql    schema step, down (sqlite)
  ir.json                                   snapshot

  1 changed model
    Author
```

On a terminal the heading is bold with a green tick, and each file's stem is coloured by what it is: a schema step cyan, a Python step magenta, a hand-written SQL step blue, `ir.json` dim (a `.down` file is its kind's colour, dimmed). The models under the count are coloured by what happened to them: new green, changed yellow, dropped and removed red, renamed blue. A list longer than 12 names shows the first 6 and `… and N more (--verbose shows them all)`; the plain form always lists them all.

| Flag | What it does |
| :--- | :--- |
| `NAME` | The migration's name: lowercase letters, digits and `_`. |
| `--sql-step NAME` | Also add a hand-written SQL step `NN_<name>.up.sql` / `.down.sql` that serves every dialect. |
| `--data-step MODEL` | Also add a Python data step `NN_backfill_<model>.py` over this model (its class name), to be written where it says `todo(...)`. |
| `--data-only` | Write only the `--data-step`: no DDL, and a full copy of the previous migration's snapshot. |
| `--no-backfill TABLE.COLUMN` | Replace the generated backfill of this column with a guard step that fails the migration while any row still needs a value. Repeatable. |
| `--verbose` | Show every name of a long change list. A terminal otherwise folds a list past 12 names. |
| `--replace NNNN_NAME` | Regenerate this migration (or `NNNN`, when one migration carries the number) on top of the newest snapshot below it, carrying its data steps and hand-written SQL steps across byte for byte; the number is kept when free. Only the newest migration can be replaced. See [Two branches, one number](../guide/schema/migrations.md#two-branches-one-number). |

Exit 0 when written (or nothing to write), 1 when refused. A primary-key change is refused, writing nothing:

<!-- refusal: primary-key-change -->
```text
changing the primary key of "author" is not generated: write it as a new table (ferro migrate new --data-step …), a backfill of parent and children, and a drop; see the Migrations docs § Changing a primary key
```

That section is [Changing a primary key](../guide/schema/migrations.md#changing-a-primary-key).

## `ferro migrate check`

Check, offline, that every model change has a migration and the migrations directory is intact. Needs no database.

| Flag | What it does |
| :--- | :--- |
| `--verbose` | Show every name of a long list of changed models. A terminal otherwise folds a list past 12 names. |

When the models match the newest migration, `check` prints one line and exits 0:

<!-- output: check-ok -->
```text
default · migrations
ok: models match 0002_author_nickname
```

Otherwise it exits 3 with one line per problem on stderr, each starting with its kind: an ungenerated model change, a broken snapshot chain, a duplicate or missing number, a step missing a target dialect's rendering, an unwritten step (`unwritten_step: 0003_fix_slugs/01_backfill_author.py:7: not written yet: write this step`). With `Author` edited since its last migration:

<!-- output: check-problem -->
```text
default · migrations
ungenerated: the models changed since 0001_create_author and no migration records it (changed models: Author); run `ferro migrate new <name>` and commit the migration it writes
```

On a terminal the ok line is a green tick and a bold `models match`, with the migration dim. Problems print under a red heading (`✘ 1 problem since 0001_create_author`) on stderr, each sentence unchanged.

## `ferro migrate up`

Apply every pending migration, in order, under the run lock. Prints one line per applied step. Exit 0, or 1 when it refused or a step failed; the next `up` resumes where this one stopped.

| Flag | What it does |
| :--- | :--- |
| `--lock-timeout DURATION` | How long to wait for another run's lock: `30s`, `500ms`, `1m`, a number of seconds, or `0` to refuse at once. Defaults to the database's [`lock_timeout`](configuration.md#lock_timeout) (`30s`); the flag wins. |

<!-- output: up -->
```text
default · sqlite:///./app.db
0002_author_nickname  01_schema               applied (0 ms)
0002_author_nickname  02_idx_author_nickname  applied (0 ms)
```

On a terminal each step is a row with a green tick, the migration, the step's stem in its kind's colour, `applied` in green and the time it took (`0.0s`) dim. A step that fails is a red row, followed on stderr by a red heading naming the step, the error in red and the fix in plain text.

## `ferro migrate down`

Revert applied migrations, newest step first, under the run lock. Prints what it would revert and asks first.

| Flag | What it does |
| :--- | :--- |
| `--to TARGET` | Where to stop: `0005` leaves `0005` fully applied, `0007:02` leaves steps `01`–`02` of `0007` applied, `0000` reverts everything. Without it, `down` reverts the latest applied migration. |
| `--all` | Revert every migration. |
| `--yes`, `-y` | Revert without asking. Without a terminal, `down` without `--yes` prints the plan and refuses (exit 1): `Not a terminal: pass --yes to revert without a prompt. Nothing was reverted.` |
| `--lock-timeout DURATION` | As for `up`. |

Reverting the latest migration, with the question skipped:

<!-- output: down -->
```text
default · sqlite:///./app.db
down reverts, in this order:
  0002_author_nickname  02_idx_author_nickname
  0002_author_nickname  01_schema
0002_author_nickname  02_idx_author_nickname  reverted (0 ms)
0002_author_nickname  01_schema               reverted (0 ms)
```

A step declared irreversible, or a migration a baseline recorded, stops the whole run before anything is reverted (exit 1). A failed down exits 1 with its step still recorded; the next `down` resumes there.

On a terminal the plan has a yellow heading (`▲ down reverts, newest first`) with the migration dim, and each reverted step is a row like `up`'s, ending in a bold `reverted 0002_author_nickname` line.

## `ferro migrate status`

Show which migrations this database has applied, without changing it or taking a lock.

| Flag | What it does |
| :--- | :--- |
| `--steps` | Print every migration's steps, not only those needing attention. |
| `--json` | Print the report as a JSON document, and nothing else on stdout. It carries `url` (the database's URL, password masked), `applied_at` on each migration and `finished_at` on each step, beside the keys it always had. |

Exit 0 when everything is applied, 3 when something is pending, 4 when something needs attention.

With everything applied, each migration is a line with the time it was applied, to the minute, in UTC:

<!-- output: status-applied -->
```text
default · sqlite:///./app.db
ok 2 applied  main._ferro_migrations
  ok 0001_create_author    applied  2026-10-15 04:31Z
  ok 0002_author_nickname  applied  2026-10-15 04:31Z
```

With something pending, `--steps` lists every migration's steps and the class each belongs to:

<!-- output: status-pending -->
```text
default · sqlite:///./app.db
-- 1 pending  main._ferro_migrations
  ok 0001_create_author    applied  2026-10-15 04:31Z
      01_schema.up.sqlite.sql  applied  expand
  -- 0002_author_nickname  pending
      01_schema.up.sqlite.sql               pending  expand
      02_idx_author_nickname.up.sqlite.sql  pending  expand
```

Without `--steps`, only the migrations whose steps need a look list them. When something needs attention (an edited file here), the report ends with a `needs attention` block and the refusal `up` would print, both on stdout:

<!-- output: status-attention -->
```text
default · sqlite:///./app.db
!! 1 edited  main._ferro_migrations
  xx 0001_create_author    applied (different checksum)  2026-10-15 04:31Z
      01_schema.up.sqlite.sql  applied (different checksum)
        applied  sha384:54bff2e2e0d1888fbb5345bd7e14b23b7c7655239d2c135bbcbe5c6208d2e33a6efc69481d2c6d1d38af59928353af73
        on disk  sha384:945a3b7670384946155bbbe7e25fc6a3d2f418e245587351c4684a24a6b6c7b95b9f08ceae04ef0ff33bb61176dcfd63
  ok 0002_author_nickname  applied  2026-10-15 04:31Z

xx needs attention
  ferro migrate: 0001_create_author/01_schema.up.sqlite.sql was edited after it was applied to this database.
    applied   sha384:54bff2e2e0d1888fbb5345bd7e14b23b7c7655239d2c135bbcbe5c6208d2e33a6efc69481d2c6d1d38af59928353af73  (2026-10-15 04:31 UTC)
    on disk   sha384:945a3b7670384946155bbbe7e25fc6a3d2f418e245587351c4684a24a6b6c7b95b9f08ceae04ef0ff33bb61176dcfd63
  An applied step is never run again. Restore the file, or accept a deliberate edit with
  `ferro migrate rerecord 0001:01`. Nothing was applied.
```

On a terminal the summary line is a coloured marker and a bold count (yellow `▲` for pending or edited, red `✘` for failed), each migration's state is coloured the same way (green applied, yellow pending, red for a failed or edited one) and the times are dim.

## `ferro migrate drift`

Compare this database with the schema snapshot of the last migration applied to it. Prints one line per difference and exits 4, or `no drift against <migration>` and exits 0. Tables the migrations never declared are ignored. Exits 4 too, naming what to run, on a database mid-migration, ahead of the checkout, or with no migration records. Takes no lock and changes nothing.

<!-- output: drift-clean -->
```text
default · sqlite:///./app.db
no drift against 0002_author_nickname
```

With the index `up` built dropped by hand:

<!-- output: drift-differences -->
```text
default · sqlite:///./app.db
xx 1 difference against 0002_author_nickname
  - idx_author_nickname index is missing
```

On a terminal the clean line is a green tick and a bold green `no drift`; differences print under a red `✘` heading, one bullet each.

## `ferro migrate baseline [TARGET]`

Record migrations as applied on a database that already has their schema (built by auto-migrate or Alembic). Checks it against the target's snapshot as `drift` does, and records every step through the target (data steps listed, not run) only when nothing differs.

| Flag | What it does |
| :--- | :--- |
| `TARGET` | The last migration to record: `0006` or `0006_add_teams`. Default: the newest. |
| `--remove` | Delete the records a baseline wrote instead (refused while a migration a run applied stands above them; refused with a target). |
| `--lock-timeout DURATION` | As for `up`. |

Exit 0 when recorded, 4 with the differences when not (there is no flag to record past them), 1 when the database already has migration records.

<!-- output: baseline -->
```text
default · sqlite:///./app.db
recorded 0001_create_author … 0002_author_slug as baseline (2 steps)
baseline checked what `ferro migrate drift` checks: objects ferro does not own (functions, triggers) were not compared
```

On a terminal the first line is a green tick and a bold `recorded …`, with the step count dim, and the note under it is dim. [When baseline says no](../howto/adopting-migrations.md#when-baseline-says-no) shows the differences it prints instead.

## `ferro migrate rerecord MIGRATION:STEP`

Accept a deliberate edit of a step this database already ran: change the step's record to the file's checksum, print both checksums, run nothing.

| Flag | What it does |
| :--- | :--- |
| `MIGRATION:STEP` | The step whose edit to accept, as `0007:01`. A schema snapshot (`ir.json`) is never re-recorded. |
| `--continue` | An edited chunked step with committed batches: keep those rows and continue from the cursor (only while the edited query pages over the same order-key columns). |
| `--restart` | An edited chunked step with committed batches: clear its cursor so the next `up` runs every row again from the start. |
| `--lock-timeout DURATION` | As for `up`. |

Exit 0, or 1 when refused (`--continue` and `--restart` together, a step with no record, a file that matches its record).

<!-- output: rerecord -->
```text
default · sqlite:///./app.db
re-recorded 0001_create_author/01_schema.up.sqlite.sql (sha384:54bff2e2e0d1888fbb5345bd7e14b23b7c7655239d2c135bbcbe5c6208d2e33a6efc69481d2c6d1d38af59928353af73 → sha384:945a3b7670384946155bbbe7e25fc6a3d2f418e245587351c4684a24a6b6c7b95b9f08ceae04ef0ff33bb61176dcfd63); nothing was run
```

On a terminal the line is a green tick and a bold `re-recorded`, with the step's stem in its kind's colour, and both checksums sit on a line of their own under it.

## Configuration refusals

Every verb but `init` reads the [project configuration](configuration.md) first, and a configuration it cannot use is refused (exit 1) before anything else, naming the fix. The project directory below is `/srv/app`.

A key with no default is missing:

<!-- refusal: missing-dialects -->
```text
/srv/app/ferro.toml: the top level of ferro.toml is incomplete; dialects has no default. Add to the top level of ferro.toml:
    dialects = ["postgres"]
```

Both config files in one directory:

<!-- refusal: both-config-files -->
```text
/srv/app holds two ferro configs, /srv/app/ferro.toml and the [tool.ferro] table of /srv/app/pyproject.toml; ferro reads one and never merges them. Remove one: delete ferro.toml, or remove [tool.ferro] from pyproject.toml
```

A key ferro does not know:

<!-- refusal: unknown-key -->
```text
/srv/app/ferro.toml: unknown key `migrations_dir` in the top level of ferro.toml; the keys allowed there are models, dialects, url_env, directory, tracking_schema, ddl_lock_timeout, lock_timeout
```

The [Configuration reference](configuration.md#refusals) lists the rest.
