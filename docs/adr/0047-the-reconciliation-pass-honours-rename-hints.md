# The reconciliation pass honours rename hints

Amends ADR-0010 and ADR-0011; extends ADR-0032.

A developer renames a model, a field and an enum label in one edit, on a project that runs `connect(auto_migrate=True, migrate_updates=True)` against Postgres:

```python
class Writer(Model):                                    # was Author
    __ferro_renamed_from__ = "author"
    full_name: str = Field(renamed_from="name")

class Status(StrEnum):
    __ferro_renamed_labels__ = {"blocked": "banned"}
```

ADR-0032 decided these hints for the generator and the snapshot, and ADR-0041 for the Alembic bridge. Nothing recorded what the auto-migrate passes do with them. Without the hints the pass would create an empty `writer` beside the old `author` (the create pass), add an empty `full_name` and warn that `name` is left over, and append `blocked` while warning that `banned` is extra. Every door would read the same model differently, and the pass would be the one that loses the rows. We decided the pass honours the hints:

```sql
ALTER TABLE "author" RENAME TO "writer";                          -- and every idx_/uq_/ck_/fk_/rls_ name derived from it
ALTER TABLE "writer" RENAME COLUMN "name" TO "full_name";         -- and its idx_/uq_/ck_/fk_ names
ALTER TYPE "status" RENAME VALUE 'banned' TO 'blocked';
```

## Liveness is read from the live database

ADR-0032 defines a hint as live when the *previous snapshot* holds the old name and lacks the new one. The pass and the bridge have no snapshot. For them a hint is live when the **live database** holds the old name (a table, a column, or a label of the live enum type) and lacks the new one. Every other hint is inert and silent, including the case where both names are live. Neither door guesses which of two live objects is the real one. A refused hint (one whose old name the model still declares, or two declarations claiming one old name) is refused on every door with the generator's own refusal.

## What runs, and under which flag

- **Table, column and foreign-key field hints** render the same `RENAME` the generator writes, through the same planner, under `migrate_updates`. They drag every derived ferro-owned name with them as ADR-0032 describes.
- **An enum label hint on Postgres** renders `ALTER TYPE … RENAME VALUE` under `migrate_updates`. The change is to the catalog only: rows holding the label follow it, nothing is scanned and nothing is lost, so it is the same kind of change as a column rename. After the rename the label-addition decision sees the renamed label set, so the pass neither appends the new label again nor warns that the old one is extra.
- **An enum label hint on SQLite** is a change to rows, because there labels are text in rows. The pass changes schema, never rows (ADR-0014), so a live hint becomes one warning naming `ferro migrate new`, whose migration relabels the rows. The warning is decided under `migrate_updates` only: from the column's `ck_` check when the column has one, without reading a row, and from a probe of the column's rows only when it has none. Plain `auto_migrate` reads no rows and says nothing about enum drift, as ADR-0011 has it.
- **The create pass holds back a hinted table.** A table whose `__ferro_renamed_from__` names a live table is not created, and neither is any table whose foreign keys reach it, directly or through further held-back tables (the hold-back is transitive). Under `migrate_updates` the reconciliation pass renames the old table and then creates the held-back dependants. Without `migrate_updates`, nothing is created for them, and one warning names both doors: `migrate_updates=True`, or a migration.

## Amendments

- **ADR-0010**: the create pass "brings missing tables into existence", except a table a live rename hint says already exists under its old name, and the tables that depend on it.
- **ADR-0011**: label addition stays append-only, and removal stays reviewed-migration territory. A **hinted** label rename on Postgres is the one other change the pass makes to a live enum type. The ownership argument is restated. The rename runs only on the developer's explicit hint and only while the old label is live and the new one absent, so misattributing a type by derivation cannot rename anything the developer did not name.

## Amended by the runtime deepening (2026-10-07, #511 tier A)

Every door decides which live tables it reads with one rule. A live read planned against a declared modelset (the pass against the models, `drift` and `baseline` against a snapshot, the bridge against the models) reads every declared table that is live, plus the old table of each live table rename hint, minus the tables the caller excludes (the pass excludes the tables its create pass just built), plus the tables the caller adds (the bridge adds the tables a revision drops). A live table is a base table: never a view, and never SQLite's own `sqlite_*` tables. The same rule decides whether a hint is live and whether a database with no records already holds a model's table (the adoption refusal), so a view named like a model gets the same answer on every door. One Rust function decides that table list, and the pass calls it directly. Over FFI it is `_live_schema_ir(using, declared_json, extra_tables_json=None)`, so a caller no longer passes a table list that repeats its declared modelset.

## Considered options

- **Ignore hints in the pass and leave renames to migrations.** Rejected: the pass would create or drop where the other doors rename, and on a table rename the create pass would build an empty twin that the next migration's drift check then reports.
- **Keep the pass append-only for labels on both dialects and warn on Postgres too.** Rejected: on Postgres the rename touches only the catalog, the same as the column renames the pass already performs, and warning there would make the pass the only door that sees a live hint and ignores it.
- **Relabel SQLite rows on connect.** Rejected: the pass is a schema pass, and an `UPDATE` over every row on application boot is a data migration with no review, no cursor and no down.
- **Probe SQLite rows on every connect, plain `auto_migrate` included, without consulting the column's check first.** Rejected: once the rows are relabelled the probe reads the whole column on every boot for as long as the hint stays, ADR-0032 lets a hint stay forever, and plain `auto_migrate` reads no rows (ADR-0011). The probe that remains runs only under `migrate_updates`, and only for a column with no `ck_` check to decide from.

## Consequences

- Under `migrate_updates`, a model edit that carries a rename hint means the same thing on the pass, the bridge and the generator.
- A hint can be left in the code after every database has been renamed. It is then inert on every door. On SQLite under `migrate_updates` it still costs one row probe per boot per hinted label when the column has no `ck_` check, and deleting the hint ends that.
