# On SQLite, a change `ALTER TABLE` cannot express is a table rebuild, and the runner owns its bracketing

Amended by ADR-0046: a rebuild sits inside the `schema`/`expand` or `contract` step whose Postgres twin carries the change, never in its own step; the fold is per phase step, so a table may be copied once in the expand and once in the contract; a rebuild recreates the table and its ferro-owned indexes as they stand after its step; and the down of a rebuild restores the table as it stood before the step.

Migration `0004` adds a table check to a model:

```python
class Invoice(Model):
    amount: int
    __ferro_checks__ = (Check("amount_positive", lambda invoice: invoice.amount > 0),)
```

The Postgres rendering is one `ALTER TABLE … ADD CONSTRAINT`. SQLite has no such statement, so the SQLite rendering builds the table again under a temporary name, copies the rows, and swaps it in:

```sql
-- ferro: foreign-keys-off
-- ferro: data-dependent
CREATE TABLE "_ferro_new_invoice" (… CONSTRAINT "ck_invoice_amount_positive" CHECK ("amount" > 0));
INSERT INTO "_ferro_new_invoice" ("id", "amount") SELECT "id", "amount" FROM "invoice";
DROP TABLE "invoice";
ALTER TABLE "_ferro_new_invoice" RENAME TO "invoice";
CREATE INDEX "idx_invoice_…" ON "invoice" (…);
```

We decided the in-house migration system writes this rebuild for every change SQLite cannot alter in place, in both directions, instead of stating a boundary. SQLite is a first-class target dialect (ADR-0026), and a migration with a Postgres rendering and a SQLite refusal is not one chain.

The file holds only the statements. The `foreign-keys-off` header tells the runner to do the rest, on one dedicated connection:

1. `PRAGMA foreign_keys=OFF`, then read the pragma back and refuse if it is still on.
2. Check the live table (see below).
3. `BEGIN IMMEDIATE`, run the file.
4. `PRAGMA foreign_key_check`; any row it returns fails the step, naming the table and row.
5. Write the step record in the same transaction and commit.
6. Restore the pragma. On any failure, roll back and close the connection rather than return it to the pool.

So a rebuild is an ordinary atomic DDL step: a failed copy rolls back everything, and the record lands with the schema or not at all.

## Native or rebuild

One table in the planner decides, separately for each direction:

| Change | SQLite rendering |
| :-- | :-- |
| Add an optional column, or a required one with a literal default | `ADD COLUMN`, with its inline `CHECK` and `REFERENCES` |
| Add a required foreign-key column, or a column with an expression default | rebuild |
| Drop a plain column | drop its `idx_`/`uq_` indexes, then `DROP COLUMN` |
| Drop a column that carries a foreign key, appears in a check, or is in the primary key | rebuild |
| Rename a column or table that carries no `ck_`/`fk_` name built from the old name | `RENAME`, indexes dropped and recreated under the new names |
| Rename a column or table whose name is inside a `ck_`/`fk_` name | rebuild, of the table and of each child whose `fk_` name changes |
| Add or drop an index or unique | `CREATE` / `DROP INDEX` |
| Type change across affinity classes, nullability, any check or foreign-key change, primary key | rebuild |

Adding a nullable foreign-key column is native on the way up and a rebuild on the way down. The down of a rebuild is a rebuild to the table as it stood before the step (ADR-0046; for a migration's first DDL step that is the parent snapshot's table, ADR-0033).

A rebuild sits inside the phase step whose Postgres twin carries the change (ADR-0046). All changes that step makes to one table fold into a single rebuild, native ones included, so the table is copied once per phase step; a step that rebuilds two tables holds both rebuilds and runs as one transaction. The rebuild recreates the ferro-owned indexes on the table as they stand after its step, so an index an *index step* builds later is not created twice (ADR-0044, ADR-0046).

A type change copies with `CAST("col" AS <type>)`. A rebuild carries the same `destructive` and `data-dependent` markers as its Postgres twin (ADR-0032).

## What the runner checks first

The generator never opens a database, so the file copies only the columns the snapshot declares and recreates only ferro's indexes. Before a `foreign-keys-off` step the runner compares the live table with the parent snapshot and refuses, naming each object, when the table has an undeclared column, an index ferro does not own, or a trigger. Nothing is carried and there is no override: the developer drops the object or writes it into a migration. Views are left to SQLite, which re-parses them at the rename and fails the transaction if one breaks.

## Enum labels

Labels are text in rows on SQLite; there is no type.

- A label rename renders `UPDATE "<table>" SET "<col>" = '<new>' WHERE "<col>" = '<old>'` for each column of the enum, and the reverse `UPDATE` as its down. It is the SQLite spelling of `RENAME VALUE`, generated from the rename hint, not a data step.
- A label removal runs the shared, dialect-neutral data step and then nothing.
- The down of a label add is `-- ferro: nothing-to-reverse SQLite stores enum labels as text; there is no type to remove the label from`.
- In all three, a column check that lists the labels has a changed body, which is a rebuild like any other check change.

## Considered options

- **A stated boundary: warn, emit a `TODO`, or refuse** (sqldef's `sqlite3def` emits nothing; ADR-0014's posture for the reconciliation pass). Rejected for the reviewed door: nine of the fifteen SQLite warn-skips are this one operation, and without it a project that tests on SQLite cannot apply the chain it ships.
- **A no-transaction step that writes its own pragmas and `BEGIN`/`COMMIT`** (Atlas's plan shape, and what ADR-0024 first pencilled in). Rejected on three counts. A failure mid-file leaves an open transaction and enforcement off on a pooled connection. A rebuild is not re-runnable from its first statement (after a crash between `COMMIT` and the record, the copy selects columns that no longer exist), which ADR-0024 requires of a no-transaction step. And `PRAGMA foreign_key_check` returns rows instead of raising, so a file the runner merely executes commits over violations.
- **`PRAGMA defer_foreign_keys=ON` inside an ordinary transaction.** Rejected: it postpones violation checks only. `DROP TABLE` still deletes the rows first, and that delete still cascades into `ON DELETE CASCADE` children, which is ferro's default action.
- **Rebuild for every change** (Alembic batch mode's `recreate="always"`). Rejected: an O(rows) copy for an `ADD COLUMN`.
- **Carry foreign triggers and indexes from `sqlite_schema`** (the sqlite.org recipe's step 3). Rejected: their SQL may name what the rebuild changes, and a committed file cannot hold what only the live database knows.
- **Rebuild in the reconciliation pass too.** Rejected, as ADR-0014 did: a copy of every row that can drop a column belongs behind review.
- **Wait for native `ALTER COLUMN … SET/DROP NOT NULL`** (SQLite 3.53). Not available: ferro bundles 3.46.0, and sqlx 0.9.0 caps `libsqlite3-sys` below the release that bundles 3.53. Nullability is a rebuild until the bundled SQLite moves.

## Consequences

- The generator renders for the SQLite ferro bundles, the only one the runner executes through. When that version gains a native form, newly generated migrations use it; committed migrations are never rewritten.
- The reconciliation pass keeps warning and skipping on SQLite. Its warnings name an in-house migration where they named Alembic's batch mode.
- With no pass output to compare a rebuild against, the I-1 pin is: the `CREATE TABLE` inside a rebuild is byte-identical to the create pass's rendering of that model apart from the table name, and a database that went through the rebuild shows no drift against one created fresh.
- Prerequisite in the reconciliation pass, inherited by the door through I-1: a column check renders inline on SQLite in `CREATE TABLE` and in `ADD COLUMN`, and `ADD COLUMN` renders `REFERENCES` when the column's default is NULL. Today both are elided with a warning.
- `DROP TABLE` takes no header. The generator orders child changes first, so nothing ferro declares references the table when it goes, and enforcement stays on.
- The temporary name `_ferro_new_<table>` can never be left behind, since the step is one transaction.
- A no-transaction step is now Postgres's case only (ADR-0024, amended).
