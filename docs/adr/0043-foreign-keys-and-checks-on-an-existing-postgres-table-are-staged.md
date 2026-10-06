# Foreign keys and checks on an existing Postgres table are staged

`Post` gains a foreign key on its existing, populated `author_id` column and a table check:

```python
class Post(Model):
    author: Annotated[Author, ForeignKey()]
    __ferro_checks__ = (Check("title_set", lambda post: post.title != ""),)
```

Until this decision the generator wrote one data-dependent step holding two `ADD CONSTRAINT`s. The plain `ADD FOREIGN KEY` takes `SHARE ROW EXCLUSIVE` on **both** `post` and `author` and holds it through a full scan of `post`; the plain `ADD CHECK` takes `ACCESS EXCLUSIVE` and scans. Writers on two tables wait for the length of the scan. On Postgres the generator now writes:

```text
migrations/0010_post_author_fk/
  01_schema.up.postgres.sql    ALTER TABLE "post" ADD CONSTRAINT "fk_post_author_id_author"
                                 FOREIGN KEY ("author_id") REFERENCES "author" ("id") NOT VALID;
                               ALTER TABLE "post" ADD CONSTRAINT "ck_post_title_set"
                                 CHECK ("title" <> '') NOT VALID;
  02_validate.up.postgres.sql  -- ferro: data-dependent
                               ALTER TABLE "post" VALIDATE CONSTRAINT "fk_post_author_id_author";
                               ALTER TABLE "post" VALIDATE CONSTRAINT "ck_post_title_set";
```

`NOT VALID` takes the same locks for no scan and releases them at commit; from that moment every new write that violates the constraint is refused. `VALIDATE CONSTRAINT` scans under `SHARE UPDATE EXCLUSIVE` (plus `ROW SHARE` on `author` for the foreign key), so writers keep going on both tables. The end state is byte-identical to the one-step form.

## The criterion is the table, not the column

The ticket asked about a column that already has rows. Postgres scans the table even when the column is brand new and all-`NULL`; the validation does not know the column was just added. So the rule is ADR-0042's: every foreign key or check added to a table that already exists is staged, including an FK or check on a new column of that table, a `db_check=True` column check, and the `ADD` half of a rebuild (an `on_delete` retarget, a check body change). A table the same migration creates is not staged. Two consequences: on an existing Postgres table a new column's check is no longer inline on `ADD COLUMN` but a separate `ADD CONSTRAINT … NOT VALID` (new tables keep it inline), and a required FK column's constraint is added `NOT VALID` in the expand and validated in the contract, still checking the backfill's writes as it goes.

Always, with no flag and no config key: an opt-in would make the table-freezing form the one most projects ship.

## The real constraint, directly

The constraint is added under its own `fk_*` / `ck_*` name, and `VALIDATE` flips only its validity flag. ADR-0042 needed a temporary `_ferro_notnull_*` name because its end state had no check at all; here the end state *is* the constraint, so there is nothing to drop.

Between the two steps the catalog shows the constraint as `NOT VALID`, which is what `pg_get_constraintdef` prints. Before this decision that text read as *body drift* for a check (a drop-and-re-add rebuild, which scans under `ACCESS EXCLUSIVE`) and was invisible for a foreign key (introspection never read `convalidated`). Both were accidents. A declared constraint that exists live but is not validated is drift, and the answer is a validate: live introspection carries `validated` for checks and foreign keys alike, the one planner emits a validate op for it, the reconciliation pass executes `VALIDATE CONSTRAINT`, `drift` reports it, and the generator's validate step renders the statement through the same function (I-1: one renderer, three doors, and the statement has a runtime twin to pin against). The check body normalizer stays body-only; validity is its own field. SQLite has no unvalidated constraints.

`drift` itself refuses, with exit 4, on a database whose tracking table holds a `failed`, `running` or `interrupted` step: drift is defined against the last applied snapshot, and a database mid-migration is at neither that snapshot nor the next, so its own partial work is not drift. The refusal names `ferro migrate status`. The reconciliation pass never meets a constraint between steps, since a tracked database refuses auto-migrate (ADR-0038).

## Where the add and the validate live

The add rides in the step the schema change already has: the schema step, or the expand step when the migration has data steps. Committing there is all the lock rule needs. The validate needs a later step, and takes the one that exists when it can: it rides in the contract step when the migration has one, ahead of the contract's own `VALIDATE` of the staged `NOT NULL` check; otherwise the migration gets a **validate step**, named `validate`. Not more steps than commits require, and not a step named `contract` in a migration with no data steps.

```text
migrations/0011_post_editor/               # a required FK column, Postgres
  01_expand.up.postgres.sql          ADD COLUMN "editor_id" INTEGER;
                                     ADD CONSTRAINT "fk_post_editor_id_author" … NOT VALID;
  02_backfill_post.py
  03_add_constraint.up.postgres.sql  ADD CONSTRAINT "_ferro_notnull_post_editor_id" CHECK ("editor_id" IS NOT NULL) NOT VALID;
  04_contract.up.postgres.sql        VALIDATE CONSTRAINT "fk_post_editor_id_author";
                                     VALIDATE CONSTRAINT "_ferro_notnull_post_editor_id";
                                     ALTER COLUMN "editor_id" SET NOT NULL;
                                     DROP CONSTRAINT "_ferro_notnull_post_editor_id";
```

Each down restores its step's pre-state exactly. The schema or expand step's down is unchanged: `DROP CONSTRAINT`, or the `DROP COLUMN` that takes the foreign key with it. The validate step's down is `DROP CONSTRAINT` followed by `ADD CONSTRAINT … NOT VALID`; there is no un-validate, and `nothing-to-reverse` would leave a state the parent step never had.

## A failed validate

The validate step is the data-dependent one. Postgres reports only that some row violated the constraint, so the runner fills ADR-0040's message with one count per kind: for a foreign key, the rows whose non-null value matches no referenced row (`post.author_id references no author in 3 rows`); for a check, the rows where the body is false (`post violates ck_post_title_set in 3 rows`). The recipe is to fix those rows and run `ferro migrate up`, which resumes at the validate step; a `NOT VALID` constraint lets a correcting `UPDATE` or `DELETE` through.

No data step is scaffolded. Ferro knows the shape of a demanded value (write one) but not of an orphan or a violating row (delete it, reparent it, null it out?), so a `todo` scaffold would be a guard with no content. `ferro migrate new --data-step Post` adds one when the developer wants it.

## Where it applies

- **SQLite** adds a constraint to an existing table by table rebuild (ADR-0034), and the rebuild *is* the validation: the copy fails on a violating check, and `foreign_key_check` before commit fails on an orphan. Under shared step numbers (ADR-0037) the rebuild sits in the schema or expand step and the validate step renders `-- ferro: not-applicable`; a project whose dialects would all render it that way (SQLite-only) gets no validate step. The data-dependent marker therefore sits on step 01 in the SQLite file and step 02 in the Postgres file: each file tells its own truth, and a step named `validate` never holds a rebuild.
- **The Alembic bridge renders the plain op**, `op.create_foreign_key` / `op.create_check_constraint`, marked data-dependent, in the revision's one transaction (ADR-0042's reasoning: staging is a property of ferro's runner, not of the planner's op).
- **Unique constraints are not staged.** Postgres has no `NOT VALID` for them; the online form is `CREATE UNIQUE INDEX CONCURRENTLY` plus `ADD CONSTRAINT … UNIQUE USING INDEX`, which cannot run in a transaction and so belongs to the `CONCURRENTLY` decision, not this one (ADR-0044, which also finds that a ferro unique is an index and never a constraint, so the second statement is not written).

## Considered options

- **Stage only a column that already has rows.** Rejected: Postgres scans regardless, and the new-FK-column-on-a-big-table case, the most common one, would keep the two-table write-blocking lock.
- **A temporary `_ferro_*` constraint, as ADR-0042 uses.** Rejected: the end state here is the constraint itself; a temporary would be added, validated, dropped and re-added for nothing.
- **Treat `NOT VALID` as body drift, or normalize it away.** Rejected: the first gives two kinds of constraint two verdicts for the same state and rebuilds under the heaviest lock; the second hides a gap in what the database guarantees.
- **Always a separate validate step, even beside a contract.** Not taken: the contract already validates and already carries the data-dependent marker; a second step costs a record, two files and a `status` line for no extra commit.
- **`nothing-to-reverse` for the validate step's down.** Rejected: a down reaches the parent step's exact state (ADR-0042).
- **Scaffold a data step ahead of the validate.** Rejected: ferro cannot know the fix, and an empty `todo` is a guard with no content.
- **Put the SQLite rebuild in the validate step so the marker lines up across dialects.** Rejected: the step's name would lie on one dialect.

## Consequences

- A Postgres migration that adds a foreign key or a check to an existing table has one more step than its SQLite rendering has work for, unless it already has a contract step. `status` and `down --to` address the same step numbers everywhere (ADR-0037).
- A run that dies between the add and the validate leaves a `NOT VALID` constraint that already refuses bad writes; `up` resumes at the validate. `drift` refuses until the migration finishes or is reverted.
- Live introspection gains a `validated` flag for foreign keys and checks, and the planner gains a validate op whose rendering is shared by the generator, the reconciliation pass and the drift check.
- Casebook A11 (inline check on a new column) and C1 (the FK of a required FK column validates `NULL`s trivially) are amended as described above.
- ADR-0042's closing consequence points here.
