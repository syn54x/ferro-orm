# Step numbers are shared across dialects, and a skipped backfill is a generated guard

`Author.slug` becomes required and `Post` gains a table check, in a project that targets Postgres and SQLite. Postgres needs three steps. SQLite needs a fourth, because a new check is a table rebuild there and each rebuild is its own step (ADR-0034):

```text
migrations/0008_author_slug/
  01_expand.up.postgres.sql          01_expand.up.sqlite.sql
  01_expand.down.postgres.sql        01_expand.down.sqlite.sql
  02_rebuild_post.up.sqlite.sql      02_rebuild_post.up.postgres.sql     <- "-- ferro: not-applicable"
  02_rebuild_post.down.sqlite.sql    02_rebuild_post.down.postgres.sql   <- "-- ferro: not-applicable"
  03_backfill_author.py
  04_contract.up.postgres.sql        04_contract.up.sqlite.sql
  04_contract.down.postgres.sql      04_contract.down.sqlite.sql
  ir.json
```

ADR-0026 left the layout of the per-dialect renderings to the CLI. Two decisions settle it.

## One step number means one step on every dialect

A DDL step's rendering is named `NN_<name>.<up|down>.<dialect>.sql`. Step numbers are dense and shared by every target dialect, so `0008:03` is the backfill on a Postgres database and on a SQLite one, and the tracking table, `status` and `down --to 0008:03` say the same thing everywhere. A step one dialect has no work for still has a file for that dialect, holding the single line `-- ferro: not-applicable`; the runner records it as a finished step that ran nothing. A step with no file for the connection's dialect is therefore always an error, never a guess.

The generator always writes the dialect suffix, in a single-dialect project too: a Postgres-only project that later adds SQLite must not find old files that silently claim to be SQLite renderings. An **unsuffixed** `NN_<name>.up.sql` is allowed for a hand-written step whose SQL is portable, and serves every dialect; a step with both an unsuffixed and a suffixed file is refused.

A data-only migration (`new --data-only`) carries a full copy of its parent's `ir.json`, so every migration has a real snapshot with a parent checksum and nothing downstream has a "same as parent" case.

## A backfill the developer does not need becomes a guard, not a gap

Making `Author.nickname` required scaffolds a backfill whose value is a `todo(...)`, refused at load (ADR-0035). A developer who knows no row holds `NULL` regenerates with `--no-backfill author.nickname`, and the generator writes a complete, generated step in the backfill's place:

```text
0008_author_nickname_required/           0008_author_nickname_required/
  01_backfill_author.py   <- todo(...)     01_guard_author.py   <- fails if any author has no nickname
  02_contract.up.postgres.sql              02_contract.up.postgres.sql
```

The claim "no row needs a value" is checked on every database the migration reaches, before the schema changes, and identically on both dialects. The same flag covers the always-scaffolded enum label removal step. Deleting a step file is never the mechanism: step numbers have no gaps, so a missing number is a lost file.

## Considered options

- **Absence means "this dialect has nothing to do".** Rejected: the runner could no longer tell a SQLite-only step from a deleted Postgres file, which hollows out ADR-0026's missing-rendering refusal.
- **A subdirectory per dialect, each numbered on its own.** Rejected: the shared `.py` data steps have no position that fits both sequences, and a step address would mean different things on different databases.
- **A manifest file listing steps, kinds and dialects.** Rejected: a second source of truth beside the file names, which can disagree with them.
- **One file with a section per dialect.** Rejected: checksums are per file, so fixing the SQLite half would invalidate the record on every Postgres database.
- **A dialect-neutral DDL file rendered at apply time.** Already rejected by ADR-0026; the neutral description exists as the pair of schema snapshots, and the `.sql` files are its frozen printout.
- **Skip a backfill by deleting its file, or by hand-swapping a commented assertion in the template.** Rejected: the first needs gaps in step numbers; the second is the guard step written by hand each time.

## Consequences

- A two-dialect project sees one-line placeholder files wherever SQLite rebuilds and Postgres does not. A single-dialect project never sees one.
- Skipping a backfill after seeing the scaffold means deleting the unapplied migration directory and running `new` again; the scaffold's header and `new`'s output both print the exact command.
- The header vocabulary (`no-transaction`, `foreign-keys-off`, `destructive`, `data-dependent`) gains `not-applicable`.
