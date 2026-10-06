# A migration is applied whole; staging across releases is the developer's editing cadence

`Author` gains a required `slug`. The generator writes one migration with three steps, because the three need different transaction shapes:

```text
migrations/0008_author_slug/
  01_expand.up.postgres.sql      ALTER TABLE "author" ADD COLUMN "slug" TEXT;
  02_backfill_author.py          @chunked(... where slug == None, order by id ...)   author.slug = todo("...")
  03_contract.up.postgres.sql    ALTER TABLE "author" ALTER COLUMN "slug" SET NOT NULL;
  ir.json                        # slug NOT NULL: the state after 03
```

A team doing a rolling deploy does not want `03_contract` yet: old code is still inserting authors without a slug. ADR-0038 gives the application two in-process calls, and both do the wrong thing with a contract that sits in the directory but should wait. `require_applied()` refuses to start while anything is pending, and `up()` at start-up applies the contract at once.

## A migration has no held half

`up` applies every pending step of every pending migration. No step can be marked to wait, and neither `up()` nor `require_applied()` takes a target. The steps inside one migration exist for transaction class and resumability (ADR-0024), never to stage a deploy.

A contract that should wait a release is a second migration, and the developer gets it by editing the model twice:

```text
release 1   slug: str | None     ->  0008_author_slug            01_schema   (ADD COLUMN, nullable)
release 2   slug: str            ->  0009_author_slug_required   01_backfill_author, 02_contract
```

Each snapshot is a state the models really declared. Between the releases, old code runs against a database one whole migration *ahead* of it, which ADR-0038 refuses unless the application passes `allow_ahead=True`; the rolling-deploy recipe in the docs names that flag. The one-migration shape is for deploys where writers stop, or where every writer already supplies the value.

## Rows written behind a backfill fail the contract, and the backfill is re-run

Inside the one-migration shape, a live writer can insert an author with no slug behind the chunked step's cursor. Ferro installs no triggers and no dual writes to catch that row. Two rules make the outcome exact instead:

- A generated backfill's query is always "the rows that still need a value" (`slug IS NULL`), so running it again touches only what is left.
- The contract's `SET NOT NULL` is a data-dependent step. When it fails on a remaining `NULL`, its refusal names the recovery: `ferro migrate down --to 0008:01`, which reverts nothing (the backfill's down is nothing-to-reverse) and clears the backfill's record, then `ferro migrate up`.

The recovery rule is the same on Postgres and SQLite.

See ADR-0045: the rejection of targets stands for the application API; a test is about one migration by name, so the migration test harness carries `apply_through`, `apply` and `revert_to`, never a step-level target.

Amended by ADR-0042: on Postgres the contract's `SET NOT NULL` is staged after the backfill (an `add_constraint` step installs `CHECK (slug IS NOT NULL) NOT VALID`; the contract validates it, sets `NOT NULL` and drops it), so the late row fails at `VALIDATE` and the table is never scanned under an exclusive lock. The check is *not* placed in the expand: Postgres enforces a `NOT VALID` check on every `UPDATE`, so a check ahead of the backfill would fail live writes to rows the backfill has not reached.

## Considered options

- **A hold marker on a step (`-- ferro: hold`) that `up` stops at and *pending* ignores.** Rejected: a held database matches neither the parent snapshot nor the migration's own, so the drift check would need a third, undeclared state to compare against, and *pending* would mean two things.
- **Targets on the in-process calls (`up(to=...)`, `require_applied(through=...)`).** Rejected: the application would carry a migration address in code that has to change with every release, and the same undeclared in-between state appears.
- **A `--split` flag that writes the expand and the contract as two migrations at once.** Rejected: the second migration is pending the moment it is written, so it changes nothing about the problem, and the first migration's snapshot would describe models nobody declared.
- **Triggers or dual writes during the backfill (pgroll, Reshape).** Rejected: ferro would own objects on the user's tables that no model declares, on Postgres only.

## Consequences

- A zero-downtime required column costs two model edits and two releases. The docs say so in the rolling-deploy recipe; generated files carry no deploy advice.
- *Pending* keeps one meaning: a step in the directory with no finished record.
- A contract that fails on late rows costs one `down` and one `up`, with no hand-written SQL.
