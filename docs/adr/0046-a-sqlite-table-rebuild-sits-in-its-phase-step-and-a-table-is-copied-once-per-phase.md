# A SQLite table rebuild sits in its phase step, and a table is copied once per phase

`Author` gains a required, indexed `slug` and a table check over it, in a project that targets Postgres and SQLite:

```python
class Author(Model):
    slug: str = Field(index=True)
    __ferro_checks__ = (Check("slug_nonempty", lambda author: author.slug != ""),)
```

ADR-0042, ADR-0043 and ADR-0044 split the Postgres rendering into five steps. SQLite cannot add a table check or `NOT NULL` in place (ADR-0034), so two of those steps are table rebuilds there, each under the step number its Postgres twin already has:

```text
migrations/0008_author_slug/
  01_expand.up.postgres.sql          ADD COLUMN "slug" TEXT;
                                     ADD CONSTRAINT "ck_author_slug_nonempty" CHECK ("slug" <> '') NOT VALID;
  01_expand.up.sqlite.sql            -- ferro: foreign-keys-off
                                     -- ferro: data-dependent
                                     CREATE TABLE "_ferro_new_author" (…, "slug" TEXT, CONSTRAINT "ck_author_slug_nonempty" CHECK ("slug" <> ''));
                                     INSERT INTO "_ferro_new_author" (…) SELECT … FROM "author";
                                     DROP TABLE "author";
                                     ALTER TABLE "_ferro_new_author" RENAME TO "author";
                                     CREATE INDEX "idx_author_…" …;            <- the parent snapshot's indexes only
  02_backfill_author.py
  03_idx_author_slug.up.postgres.sql -- ferro: no-transaction
                                     DROP INDEX CONCURRENTLY IF EXISTS "idx_author_slug";
                                     CREATE INDEX CONCURRENTLY "idx_author_slug" ON "author" ("slug");
  03_idx_author_slug.up.sqlite.sql   CREATE INDEX "idx_author_slug" ON "author" ("slug");
  04_add_constraint.up.postgres.sql  ADD CONSTRAINT "_ferro_notnull_author_slug" CHECK ("slug" IS NOT NULL) NOT VALID;
  04_add_constraint.up.sqlite.sql    -- ferro: not-applicable
  05_contract.up.postgres.sql        -- ferro: data-dependent
                                     VALIDATE CONSTRAINT "ck_author_slug_nonempty";
                                     VALIDATE CONSTRAINT "_ferro_notnull_author_slug";
                                     ALTER COLUMN "slug" SET NOT NULL;
                                     DROP CONSTRAINT "_ferro_notnull_author_slug";
  05_contract.up.sqlite.sql          -- ferro: foreign-keys-off
                                     -- ferro: data-dependent
                                     CREATE TABLE "_ferro_new_author" (…, "slug" TEXT NOT NULL, CONSTRAINT "ck_author_slug_nonempty" …);
                                     INSERT INTO "_ferro_new_author" (…) SELECT … FROM "author";
                                     DROP TABLE "author";
                                     ALTER TABLE "_ferro_new_author" RENAME TO "author";
                                     CREATE INDEX "idx_author_…" …;
                                     CREATE INDEX "idx_author_slug" ON "author" ("slug");   <- built in 03, recreated here
  ir.json
```

ADR-0034 and ADR-0037 were decided before the staging shapes and said a rebuild is its own step and a table is copied once per migration. Read beside ADR-0042–0044 they placed and counted steps differently, so `status` and `down --to 0008:SS` would have meant different things depending on which the generator followed. Four decisions reconcile them.

## A rebuild lives in the phase step whose Postgres twin carries the change

A rebuild sits inside the `schema` or `expand` step, or the `contract` step, under the shared step number (ADR-0037); there is no `NN_rebuild_<table>` step. Every change that rebuilds on SQLite has a Postgres statement in the same phase (a check add is `NOT VALID`, a rename inside a `ck_`/`fk_` name is `RENAME CONSTRAINT` on the child, the down of a nullable foreign-key add is `DROP COLUMN`), and a change with no statement on Postgres has none on SQLite either (a type change within an affinity class), so a dedicated step has no remaining case. `validate` and `add_constraint` steps render `-- ferro: not-applicable` on SQLite, an index step renders the plain statement; none of the three ever holds a rebuild.

When one phase rebuilds two tables, a rename that drags into a child's `fk_` name or two models edited in one migration, the step's SQLite file holds both rebuilds and the native statements, and runs as one transaction: one `foreign_keys=OFF`, one `foreign_key_check`, one record. ADR-0034's "each rebuilt table is its own step, so a failure on the second resumes there" is struck: the resume point saved re-copying the first table, which is a cost, not a correctness property, and a phase is one commit on Postgres already. In exchange `down --to 0008:01` names the same schema state on every dialect, and a Postgres-only project never has its step numbering shaped by how many tables SQLite would have copied.

## A table is copied at most once per phase step

A table is rebuilt at most once in each of `schema`/`expand` and `contract` that has non-native SQLite work for it, and every change that phase makes to the table, native ones included, folds into that rebuild. ADR-0034's "fold" is per phase step, not per migration. In the example `author` is copied twice, because the table check is non-native in the expand and `NOT NULL` is non-native in the contract. A plain required column with no check is still one copy: native `ADD COLUMN` in the expand, rebuild in the contract. A step is one set of schema changes on every dialect and only its spelling differs, so the check guards the backfill's writes on SQLite from step 01 on, as the `NOT VALID` check does on Postgres.

The failure posture is ADR-0040's, unchanged: the contract rebuild failing on a remaining `NULL` names `down --to 0008:02`, fix, `up`, and the runner fills the message's count with `SELECT count(*) … WHERE "slug" IS NULL`, as ADR-0042 has it on Postgres, since SQLite's error names the column and not the rows.

## A rebuild recreates the table and its indexes as they stand after its step

A rebuild's `DROP TABLE` takes every index with it, and an index on a table that already exists is built by an index step after the data steps (ADR-0044). So the expand's rebuild recreates the parent snapshot's indexes on that table, with the step's own renames applied (adds and drops belong to the later index steps), and the contract's rebuild recreates the target snapshot's. Had the expand recreated `idx_author_slug`, step 03 would fail on a duplicate name; had the contract not, the index built in 03 would be gone.

The planner therefore renders a rebuild's `CREATE TABLE` against the shape its step leaves behind: for the expand that is the union snapshot (ADR-0025: the new column nullable), for the contract the target snapshot. ADR-0034's I-1 pin holds per shape: the `CREATE TABLE` inside the expand's rebuild is byte-identical to the create pass's rendering of the union model, and the contract's to the target model, apart from the table name.

## The down of a rebuild restores the table as it stood before the step

ADR-0034 said the down of a rebuild is a rebuild to the parent snapshot's table; that held when a migration had one rebuild. The contract rebuild's down restores the shape the preceding step left (`slug` nullable, the check present, `idx_author_slug` present), which is ADR-0042's "each down restores its step's pre-state exactly". Only the expand's rebuild reaches the parent snapshot. ADR-0033's "a down reaches the parent snapshot" is about the whole down walk and still holds.

## Considered options

- **A rebuild as its own step, with a Postgres `not-applicable` placeholder (ADR-0034, ADR-0037's worked example).** Rejected: ADR-0043 had already placed the rebuild in the phase step so that a step named `validate` never holds one, and no change rebuilds on SQLite while Postgres has no statement in that phase, so the step would hold what the phase step already holds under a second number.
- **One step per rebuilt table within a phase.** Rejected: it buys a resume point after the first copy at the price of step numbers that differ by how many tables SQLite copies, and of a phase that is one commit on Postgres and several on SQLite.
- **One copy per migration, every non-native expand change deferred into the contract's rebuild.** Rejected: step 01's SQLite file would no longer hold the change its Postgres file holds, `down --to 0008:01` would land on different schemas per dialect, and the check would not guard the backfill's writes on SQLite.
- **A rebuild recreates every index the target snapshot declares.** Rejected: duplicates the index step's build when the rebuild is the expand's, and an index on a backfilled column would be maintained through every backfill write, which ADR-0044 moved the index step to avoid.

## Consequences

- ADR-0034 is amended: a rebuild is not its own step, the fold is per phase step, and the down of a rebuild restores the table as it stood before the step. ADR-0037's worked example is rewritten to the five-step layout above.
- The planner carries the shape each DDL step leaves behind, for the rebuild's `CREATE TABLE` and its index list; the expand's shape is the union snapshot ADR-0025 already requires.
- A two-dialect migration whose expand has a non-native SQLite change copies the table twice. The copy is O(rows) and runs in the migration's own transactions; nothing is scanned under a lock Postgres would not also take.
- The glossary's *Table rebuild* no longer says "one DDL step per rebuilt table"; it names the step the rebuild sits in and what the rebuild recreates.
