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

Refused: a project that already has a ferro config (in either file; `init` never rewrites one), a directory holding an Alembic environment, `--url`, `--config`. Appends to an existing `pyproject.toml` as text, so the rest of the file is never reformatted.

## `ferro migrate new NAME`

Write the next migration from the models' difference with the last one. Diffs against the newest migration's schema snapshot, never a database. Prints the files it wrote, what changed, and a line per thing to do; prints `no schema change: nothing written` when nothing renders DDL.

| Flag | What it does |
| :--- | :--- |
| `NAME` | The migration's name: lowercase letters, digits and `_`. |
| `--sql-step NAME` | Also add a hand-written SQL step `NN_<name>.up.sql` / `.down.sql` that serves every dialect. |
| `--data-step MODEL` | Also add a Python data step `NN_backfill_<model>.py` over this model (its class name), to be written where it says `todo(...)`. |
| `--data-only` | Write only the `--data-step`: no DDL, and a full copy of the previous migration's snapshot. |
| `--no-backfill TABLE.COLUMN` | Replace the generated backfill of this column with a guard step that fails the migration while any row still needs a value. Repeatable. |

Exit 0 when written (or nothing to write), 1 when refused. A primary-key change is refused, writing nothing:

<!-- refusal: primary-key-change -->
```text
changing the primary key of "author" is not generated: write it as a new table (ferro migrate new --data-step …), a backfill of parent and children, and a drop; see the Migrations docs § Changing a primary key
```

That section is [Changing a primary key](../guide/schema/migrations.md#changing-a-primary-key).

## `ferro migrate check`

Check, offline, that every model change has a migration and the migrations directory is intact. Needs no database.

```text
$ ferro migrate check
ok: models match 0002_author_slug
```

Exit 0, or 3 with one line per problem on stderr, each starting with its kind: an ungenerated model change, a broken snapshot chain, a duplicate or missing number, a step missing a target dialect's rendering, an unwritten step (`unwritten_step: 0003_fix_slugs/01_backfill_author.py:7: not written yet: write this step`).

## `ferro migrate up`

Apply every pending migration, in order, under the run lock. Prints one line per applied step. Exit 0, or 1 when it refused or a step failed; the next `up` resumes where this one stopped.

| Flag | What it does |
| :--- | :--- |
| `--lock-timeout DURATION` | How long to wait for another run's lock: `30s` (default), `500ms`, `1m`, a number of seconds, or `0` to refuse at once. |

## `ferro migrate down`

Revert applied migrations, newest step first, under the run lock. Prints what it would revert and asks first.

| Flag | What it does |
| :--- | :--- |
| `--to TARGET` | Where to stop: `0005` leaves `0005` fully applied, `0007:02` leaves steps `01`–`02` of `0007` applied, `0000` reverts everything. Without it, `down` reverts the latest applied migration. |
| `--all` | Revert every migration. |
| `--yes`, `-y` | Revert without asking. Without a terminal, `down` without `--yes` prints the plan and refuses (exit 1): `Not a terminal: pass --yes to revert without a prompt. Nothing was reverted.` |
| `--lock-timeout DURATION` | As for `up`. |

A step declared irreversible, or a migration a baseline recorded, stops the whole run before anything is reverted (exit 1). A failed down exits 1 with its step still recorded; the next `down` resumes there.

## `ferro migrate status`

Show which migrations this database has applied, without changing it or taking a lock.

| Flag | What it does |
| :--- | :--- |
| `--steps` | Print every migration's steps, not only those needing attention. |
| `--json` | Print the report as a JSON document. |

Exit 0 when everything is applied, 3 when something is pending, 4 when something needs attention.

## `ferro migrate drift`

Compare this database with the schema snapshot of the last migration applied to it. Prints one line per difference and exits 4, or `no drift against <migration>` and exits 0. Tables the migrations never declared are ignored. Exits 4 too, naming what to run, on a database mid-migration, ahead of the checkout, or with no migration records. Takes no lock and changes nothing.

## `ferro migrate baseline [TARGET]`

Record migrations as applied on a database that already has their schema (built by auto-migrate or Alembic). Checks it against the target's snapshot as `drift` does, and records every step through the target (data steps listed, not run) only when nothing differs.

| Flag | What it does |
| :--- | :--- |
| `TARGET` | The last migration to record: `0006` or `0006_add_teams`. Default: the newest. |
| `--remove` | Delete the records a baseline wrote instead (refused while a migration a run applied stands above them; refused with a target). |
| `--lock-timeout DURATION` | As for `up`. |

Exit 0 when recorded, 4 with the differences when not (there is no flag to record past them), 1 when the database already has migration records.

## `ferro migrate rerecord MIGRATION:STEP`

Accept a deliberate edit of a step this database already ran: change the step's record to the file's checksum, print both checksums, run nothing.

| Flag | What it does |
| :--- | :--- |
| `MIGRATION:STEP` | The step whose edit to accept, as `0007:01`. A schema snapshot (`ir.json`) is never re-recorded. |
| `--continue` | An edited chunked step with committed batches: keep those rows and continue from the cursor (only while the edited query pages over the same order-key columns). |
| `--restart` | An edited chunked step with committed batches: clear its cursor so the next `up` runs every row again from the start. |
| `--lock-timeout DURATION` | As for `up`. |

Exit 0, or 1 when refused (`--continue` and `--restart` together, a step with no record, a file that matches its record).

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
/srv/app/ferro.toml: unknown key `migrations_dir` in the top level of ferro.toml; the keys allowed there are models, dialects, url_env, directory, tracking_schema, ddl_lock_timeout
```

The [Configuration reference](configuration.md#refusals) lists the rest.
