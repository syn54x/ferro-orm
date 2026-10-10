# A baseline is verified against the snapshot and has no override

A project has `User` and `Team` models and a production database that `connect(auto_migrate=True)` built two years ago, or that Alembic built and that carries `alembic_version`. The developer generates `0001_initial`, whose DDL step is `CREATE TABLE user …; CREATE TABLE team …`. Production already has those tables, so `0001_initial` must count as applied there without being run.

`ferro migrate baseline` does that, and only after checking. It runs the drift check against the schema snapshot of the last migration it is asked to record, and writes the step records only when the check reports nothing:

```
ferro migrate baseline: this database does not match 0001_initial.
  user.nickname   column is missing
  team.slug       live type is varchar, 0001_initial declares text
Baseline records a migration as applied only when the database already has its schema.
Fix the database or the models, then run baseline again. Nothing was recorded.
```

There is no flag that records past a non-empty report. The report is the list of work: close the gap with the door that built the database (the reconciliation pass, or one last Alembic revision), or change the models and regenerate `0001`.

`0001` itself is generated from the models against the empty modelset, exactly as on a new project. The live database is checked against it and is never an input to it.

## Considered options

- **Record without checking** (Django's `migrate --fake`, Alembic's `stamp`, Flyway's `baseline`, Prisma's `resolve --applied`). Rejected: a baseline over a database missing a column succeeds, and the failure arrives several migrations later as "column already exists" or "column does not exist", far from its cause. Django's `--fake-initial` checks only that the table names exist.
- **Check, with a `--force`.** Rejected: the override would be used on exactly the databases whose mismatch nobody understood, and every later migration assumes the snapshot is true of the database.
- **Generate `0001` by introspecting the live database.** Rejected: it needs a second source of snapshots that cannot see defaults or anything ferro does not own, and a new database built from that `0001` would differ from one built from the models (ADR-0023: a snapshot is what the models said).
- **Make every generated DDL step idempotent (`IF NOT EXISTS`) so `0001` can simply run.** Rejected: the guard checks a name, not a shape, so a `user` table missing three columns passes; `ADD CONSTRAINT`, SQLite's `ADD COLUMN`, type changes and `SET NOT NULL` have no guard at all, so only some steps would be idempotent; and a transactional DDL step already runs exactly once because its step record commits with it. Guards stay where no record can vouch for the step: inside no-transaction steps (ADR-0024). This is also what Django, Alembic, Rails, Prisma, Atlas and Flyway do.
- **A flag on `up`** instead of a verb. Rejected: a switch that records without running does not belong on the command deploy scripts call.
- **Read `alembic_version` as a sanity check.** Rejected: ferro cannot know Alembic's head without loading the Alembic environment, and the revision label establishes nothing the drift check does not establish from the schema itself. Ferro never reads or writes that table.

## Consequences

- The drift check ignores live tables the snapshot does not declare (`alembic_version`, a hand-made `audit_log`, an extension's tables). Without that, no Alembic-built database could ever be baselined. A table the snapshot declares and the database lacks is drift.
- The check sees what the reconciliation pass sees. It does not see column defaults or objects the user owns, and a successful baseline says so in its output.
- `baseline` takes a target migration, defaulting to the head when the directory holds one migration. It records every step of every migration through the target, data steps included, and lists the data steps it recorded without running. It refuses when the tracking table already holds a record.
- A step record carries its origin, `run` or `baseline`. `status` shows `installed (baseline)`. `down` stops at a baselined migration, because its down steps would drop tables the migration never created.
- `up` against a database with no step records, where a table of the first pending migration's snapshot already exists, refuses before running anything and names `baseline`.
- Adoption is a cutover per database: bring it to the old door's head, baseline it, stop using the old door there. A change the old door makes afterwards is drift. Ferro adds no interlock between itself and Alembic.

Amended at the epic's close (2026-10-07, #577): `status` says `applied (baseline)`, and a run-origin step `applied`, matching the glossary's *Step record*; *installed* is no longer a status word.

Amended 2026-10-10 (the Pinch epic, #618 and #622):

- **The refusal of a tracked database is typed.** Two services running one pre-deploy concurrently both read "no records" and both call `baseline()`; the run lock serialises them, the first records, the second is refused and must go on to `up()`. The core raises that refusal with `kind="already_tracked"`, and `ferro.migrations` raises `AlreadyTrackedError(MigrationRefused)` carrying `.applied`, `.head` and the status report in `report=`, so the caller branches on the type and never on the message. `remove_baseline()`'s refusal (a run applied a migration above the baseline) is `AppliedAboveBaselineError` with `.above`, `kind="applied_above_baseline"`. The CLI's exit code and text are unchanged. The documented recipe becomes `status()` → `try: baseline() except AlreadyTrackedError: pass` → `up()`. The idempotent alternative (return a report when the records already hold what this call would write) was rejected: it is false the moment the concurrent runner's `up()` adds run-origin rows above the baseline.
- **Column defaults are compared.** "It does not see column defaults" no longer holds: the live read carries each column's default, and a *foreign default* (ADR-0027, as amended) is drift, so `baseline` refuses it and `drift` reports it (`author.slug has default '' in the database, none declared`). The successful-baseline sentence and the adopting howto now say only that objects ferro does not own (functions, triggers) were not compared. A hand-set default blocks adoption until it is dropped (one last `migrate_destructive` start on the old door, or `ALTER TABLE … ALTER COLUMN … DROP DEFAULT`); declarable server defaults are out of scope and a separate issue.
