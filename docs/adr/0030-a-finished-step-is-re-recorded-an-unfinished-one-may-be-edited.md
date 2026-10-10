# A finished step's file is only ever re-recorded; an unfinished step's file may be edited

Migration `0007_nickname` holds `01_add_nickname.sql`, `02_backfill_nickname.py` (chunked, 40M rows) and `03_nickname_index.sql`. The run dies at 30M rows of step 02 because of a bug in the transform. The fix is an edit to `02_backfill_nickname.py`, and after it the file's checksum no longer matches the one in its step record. A rule that refuses every checksum mismatch would refuse the fix. So the rule depends on whether the step finished, and on whether any of it was committed:

- **A finished step** (`01_add_nickname.sql`) is strictly checked. `up` never accepts a mismatch. A deliberate edit is accepted only through an explicit re-record verb, which names the migration, shows both checksums, changes the record and runs nothing.
- **An unfinished atomic data step or transactional DDL step** committed nothing, so its edited file is accepted, its checksum re-recorded, and the output says so.
- **An unfinished no-transaction step** may be half-applied, but its contract is that it is safe to re-run from its first statement (ADR-0024), so its edited file is accepted the same way.
- **An unfinished chunked data step with committed batches** is refused. Ferro cannot know whether the 30M rows written by the old code are right under the new code, so the developer chooses: continue from the cursor, or restart from the first row. Continue is offered only while the edited file is still chunked over the same order-key columns; otherwise the stored cursor is not a position in the new query and only restart is offered.
- **A step with no record** (`03_nickname_index.sql`) is free to edit.
- **A schema snapshot** is never re-recorded. Later migrations link to it by checksum and historical models are built from it, so an edit breaks the chain on disk for every database; the only way out is to restore the file.

A step record's checksum covers the bytes this database executed: the up file, in this database's dialect rendering. Fixing a broken `down` file or another dialect's rendering is not a mismatch.

## Considered options

- **Refuse every mismatch, finished or not** (Atlas's `HistoryChangedError`). Rejected: a failed step is the step most likely to need an edit, and the developer would have to revert or hand-edit the tracking table to apply a bug fix.
- **No override for a finished step**, as out-of-order migrations have none (ADR-0028). Rejected: an out-of-order migration can be regenerated at the head, but an applied file sometimes has to change, for example a migration that reached production only after a manual fix and must be corrected for fresh databases.
- **Accept the edited chunked step and continue silently.** Rejected: it leaves rows transformed by two versions of the code with no record that it happened.
- **A history table that keeps reverted and superseded records** (Flyway, Atlas). Rejected: the tracking table answers where a database stands; who deployed what and when belongs to the deploy system. A reverted step's record is removed in the transaction of its down.

## Consequences

- The tracking table is `_ferro_migrations`, one row per step keyed by migration number and step ordinal, in the connection's current schema on Postgres. A migration's name and snapshot checksum repeat on each of its step records, so a migration is applied when every step on disk has a finished record, and a snapshot edited between two steps of one migration is caught.
- The record stores the step's kind (`ddl`, `ddl-no-transaction`, `atomic`, `chunked`) because the rules above depend on what the recorded attempt was, and an edit can change the kind.
- A failure's time and error text are written after the rollback when the runner survives to write them. Nothing reads them to decide anything: started, not finished and no live run lock already means the step did not complete. They let `status` say `failed` with the error, as opposed to `interrupted`.
- The table's format number lives in a one-row side table, `_ferro_migrations_format`, because a per-row column cannot be read from an empty table. Every verb reads it first; a newer ferro upgrades the table under the run lock, and an older ferro refuses a newer format.

  Amended 2026-10-10 (#620): `format` names a **reshaping**, a change an older reader cannot read past (a column renamed, removed or retyped, a changed key), and only that is what an older ferro refuses. An **additive** change, a new nullable column, is a `minor` in the same side table: every reader selects and every writer inserts the columns it names, so an older ferro reads, writes and re-records beside a column it does not know, and a newer ferro reads the records it leaves with the new column empty. The newer ferro raises the minor under the run lock on its first mutating verb and fills in what the older one left empty on every mutating verb; readers never write. The first minor is ADR-0054's step class.
- The first mutating verb creates both tables under the run lock. `status` creates nothing.
- Raw-byte checksums make a line-ending conversion on checkout a mismatch, so the generator marks the migrations directory byte-exact in `.gitattributes`.
