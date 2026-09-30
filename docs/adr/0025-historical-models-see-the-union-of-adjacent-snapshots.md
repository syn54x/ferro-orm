# Historical models see the union of a migration's two adjacent snapshots

A data step runs between its migration's expand and contract DDL steps, when the live table holds every old column not yet dropped and every new column already added. Neither the previous migration's schema snapshot (no new columns) nor the migration's own (old columns already gone) describes that table. We decided that historical models are built from the **union** of the two snapshots' columns, so a backfill that reads `name` and writes `first_name` sees both without ferro storing an intermediate snapshot or replaying the migration's SQL to derive one. A same-named column whose declaration differs between the two snapshots is the one ambiguous case; the builder refuses it, because the safe expand/contract shape (add new column, copy, drop, rename) never produces it.

## Considered options

- **The previous migration's snapshot alone** (the original historical-models decision). Rejected: a backfill of a column this migration adds cannot see the column.
- **An intermediate `ir.json` written by the generator after the expand steps.** Rejected: a second snapshot per migration, unavailable to hand-written `--empty` migrations, and a second file format for the integrity floor to checksum.
