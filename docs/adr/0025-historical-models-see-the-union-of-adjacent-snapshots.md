# Historical models see the union of a migration's two adjacent snapshots

A data step runs between its migration's expand and contract DDL steps, when the live table holds every old column not yet dropped and every new column already added. Neither the previous migration's schema snapshot (no new columns) nor the migration's own (old columns already gone) describes that table. We decided that historical models are built from the **union** of the two snapshots' columns, so a backfill that reads `name` and writes `first_name` sees both without ferro storing an intermediate snapshot or replaying the migration's SQL to derive one. A same-named column whose declaration differs between the two snapshots is the one ambiguous case; the builder refuses it, because the safe expand/contract shape (add new column, copy, drop, rename) never produces it.

## Considered options

- **The previous migration's snapshot alone** (the original historical-models decision). Rejected: a backfill of a column this migration adds cannot see the column.
- **An intermediate `ir.json` written by the generator after the expand steps.** Rejected: a second snapshot per migration, unavailable to hand-written `--empty` migrations, and a second file format for the integrity floor to checksum.

## Amended by the model-change casebook (#481)

Two cases the union alone gets wrong:

- **A column present only in the migration's own snapshot is nullable in the historical model**, whatever the declaration says. The expand step created it nullable and every row holds `NULL` until the backfill runs; a historical class that took the own snapshot's `NOT NULL` literally would hydrate `None` into a required field on every row.
- **A rename is one column under two names, not two columns.** The plain union of `name` (previous) and `full_name` (own) describes a table that never exists. When a rename hint is honoured, the union applies it; a rename that reaches a data step without a hint is drop-plus-add, and the historical model sees only the new column, which is what the live table holds. The same rule covers renamed enum labels.
