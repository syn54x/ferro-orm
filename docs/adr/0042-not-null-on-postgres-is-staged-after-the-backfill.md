# `NOT NULL` on Postgres is staged after the backfill

`Author` gains a required `slug`. ADR-0040's contract step was one line, `ALTER TABLE "author" ALTER COLUMN "slug" SET NOT NULL`, and on a populated table that line scans every row while holding an `ACCESS EXCLUSIVE` lock: reads and writes wait for the whole scan. On Postgres the generator now writes the contract in two steps around a temporary check:

```text
migrations/0008_author_slug/
  01_expand.up.postgres.sql          ALTER TABLE "author" ADD COLUMN "slug" TEXT;
  02_backfill_author.py              @chunked(... where slug == None ...)   author.slug = todo("...")
  03_add_constraint.up.postgres.sql  ALTER TABLE "author" ADD CONSTRAINT "_ferro_notnull_author_slug"
                                       CHECK ("slug" IS NOT NULL) NOT VALID;
  04_contract.up.postgres.sql        ALTER TABLE "author" VALIDATE CONSTRAINT "_ferro_notnull_author_slug";
                                     ALTER TABLE "author" ALTER COLUMN "slug" SET NOT NULL;
                                     ALTER TABLE "author" DROP CONSTRAINT "_ferro_notnull_author_slug";
```

`ADD CONSTRAINT … NOT VALID` lands instantly and refuses `NULL` on every write from then on. `VALIDATE CONSTRAINT` scans under `SHARE UPDATE EXCLUSIVE`, so writers keep going. `SET NOT NULL` is then instant, because Postgres 12+ trusts the validated check instead of scanning again. The end state is byte-identical to the one-line form: the temporary check is gone, and the snapshot, the drift check and Alembic autogenerate never see it.

## After the backfill, not before it

The ticket that produced this decision proposed the check in the expand step, as a write-side guard so no `NULL` could land behind the chunked step's cursor. That placement does not survive one fact: Postgres enforces a `NOT VALID` check on every `UPDATE` of a row, not only on writes to the checked column. Placed before the backfill, the check fails every ordinary application write to a row the backfill has not reached yet, for as long as the backfill runs. pgroll can place it there only because a trigger fills the column on each such write, and ADR-0040 ruled triggers out.

Placed after the backfill, the window ADR-0040 describes stays open during the backfill and is handled as before: a row written behind the cursor fails the contract (now at `VALIDATE`), the message names the recovery, and the backfill is re-run. The runner fills the message's count with one `SELECT count(*) … WHERE "slug" IS NULL`, since Postgres reports only that some row violated the check. The window closes for good once the check is installed.

## Two steps, because the add's lock lasts until commit

`ADD CONSTRAINT` takes `ACCESS EXCLUSIVE` and holds it until its transaction commits. If the `VALIDATE` shares that transaction, the scan runs under the lock and nothing was gained. A step is a transaction, so the commit is a step boundary: the add-constraint step installs the check and commits; the contract validates, sets `NOT NULL` and drops the check. Not more than two: a commit between `VALIDATE`, `SET NOT NULL` and `DROP` changes nothing, and a failed `VALIDATE` rolls the contract back, so a failed contract already means "the validate failed and nothing else ran". Not one no-transaction step: Postgres has no `ADD CONSTRAINT IF NOT EXISTS`, so the file could not be re-run from its first statement (ADR-0024).

Each down restores its step's pre-state exactly. The add-constraint step's down is `DROP CONSTRAINT`; the contract's down is `ADD CONSTRAINT … NOT VALID` then `DROP NOT NULL` (there is no un-validate, and nothing needs one). `down --to 0008:01` walks 04 → 03 → 02 and leaves no check behind.

## Where it applies

- **Always, on Postgres, for a table that already exists.** No flag and no config key: an opt-in would make the table-freezing form the one most projects ship. A column on a table the same migration creates is not staged.
- **Both model edits.** A brand-new required field (with an expand) and `slug: str | None` → `slug: str` (no expand) are staged identically; nothing hangs on the expand step.
- **The temporary name is `_ferro_notnull_<table>_<column>`**, truncated like every other ferro name, and deliberately not `ck_*`: check reconciliation and the drift check would read a `ck_*` leftover as a declared check the model dropped. The `_ferro_` prefix follows `_ferro_new_<table>` (ADR-0034).
- **SQLite** has no equivalent; `SET NOT NULL` is a table rebuild there (ADR-0034). Under shared step numbers (ADR-0037) a Postgres+SQLite project gets a `-- ferro: not-applicable` rendering for the add-constraint step. A project whose declared dialects would all render it that way (SQLite-only) gets no add-constraint step at all.
- **The Alembic bridge renders the plain op.** It translates the same planner op (ADR-0041), but a revision's `upgrade()` is one transaction, where staging buys nothing. `op.alter_column("author", "slug", nullable=False)`, marked data-dependent, stays. Staging is a property of ferro's runner (one step, one commit), not of the planner's op.

## Considered options

- **The check in the expand step (the ticket's proposal).** Rejected: fails live application updates to not-yet-backfilled rows for the length of the backfill. Safe only when writers are stopped, which is when it is not needed.
- **Opt-in by flag.** Rejected: the unsafe default is what most people would ship, and I-6 treats that as a stop-gap.
- **One no-transaction step holding all four statements.** Rejected: not re-runnable from its first statement.
- **Three steps, `VALIDATE` alone in the middle.** Not taken: a failed contract already isolates the failing statement, and the extra step costs a record, two files and a `status` line.
- **A `ck_*` name for the temporary check.** Rejected: collides with check reconciliation's ownership test.

## Consequences

- A Postgres `NOT NULL` migration has one more step than its SQLite rendering has work for. `status` and `down --to` address the same step numbers everywhere (ADR-0037).
- A run that dies between the add-constraint step and the contract leaves `_ferro_notnull_*` on the table; `up` resumes at the contract and removes it. The name says whose it is.
- ADR-0040's "same rule on Postgres and SQLite" now means the same *recovery* rule; the statement that fails differs.
- Staging a foreign key or a check added to an existing table (`NOT VALID` → `VALIDATE`) is the same shape; ADR-0043 decides it, and its validate rides in this contract step when the migration has one.
