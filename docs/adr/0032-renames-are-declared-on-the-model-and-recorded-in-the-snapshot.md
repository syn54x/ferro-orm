# Renames are declared on the model and recorded in the snapshot

A developer renames `Author.name` to `full_name`. A diff of the two schema snapshots sees a column that vanished and a column that appeared, and the DDL for that is `DROP COLUMN "name"` plus `ADD COLUMN "full_name"`: every author's name is gone. No diff can tell that from a rename. The developer has to say so, and says so on the model:

```python
class Writer(Model):                                    # was Author
    __ferro_renamed_from__ = "author"                   # table
    full_name: str = Field(renamed_from="name")         # column
    editor: Annotated[User, ForeignKey(renamed_from="author")]   # FK field; author_id → editor_id follows

class Status(StrEnum):
    __ferro_renamed_labels__ = {"blocked": "banned"}    # new label → old label
```

A rename hint is part of the declared modelset, so the migration's schema snapshot carries it and nothing else records the rename. One liveness rule serves the generator and the historical-model builder: a hint is **live** when the previous snapshot holds the old name and lacks the new one. A live hint renders `RENAME`; any other hint is inert, in the migration that follows and in every later one, so it can be left in the code or deleted without generating anything.

Amended by ADR-0047 (2026-10-07): the reconciliation pass honours the same hints, with liveness read from the live database (old name live, new name absent) instead of the previous snapshot.

A drop and an add with no hint are rendered as a drop and an add, marked destructive, and the generator's summary names `renamed_from`. The generator never matches a vanished column to a new one by type.

An enum type's rename takes no hint. When every column of a vanished type now declares one and the same new type, `ALTER TYPE "status" RENAME TO "authorstatus"` and the swap-type recipe end in the identical schema and data, so rendering the rename is no guess about intent.

## Considered options

- **Ask at generation time** ("did you rename `name` to `full_name`?", Django's shape). Rejected: an agent or a CI job cannot answer a prompt, the answer is not in the pull request's diff, and deleting and regenerating the migration asks again.
- **A flag on the command** (`--rename author.name=full_name`). Rejected for the same last two reasons: the rename lives in shell history, not beside the model edit it explains.
- **Hand-edit the generated drop and add into a rename.** Rejected: the snapshot would not know, so the historical model for a data step in that migration would be built with both columns (ADR-0025), a table that never exists.
- **A separate `renames` record in the migration.** Rejected: a second record that can disagree with the snapshot, and a second file for the integrity floor to checksum.
- **Infer a rename when types match.** Rejected: `nickname` dropped and `handle` added in one edit is two columns as often as it is one.
- **Refuse a drop and an add on one table until the developer declares which it is.** Rejected: the honest two-column case would need a second, "not a rename" declaration; the destructive marker and the summary line already make the loss loud.

## Consequences

- A rename drags every ferro-owned name derived from the old one, in one step, with no opt-out: a column rename renames its `idx_`, `uq_`, `ck_` and, for an FK field, `fk_` names; a table rename renames every `idx_`, `uq_`, `ck_` and `rls_` name on the table, every `fk_<child>_<col>_<table>` on tables that reference it, and the join tables derived from it. Stale names would be reported as drift forever. The old and new names both come from the naming functions the create pass uses.
- The generator refuses a hint whose old name the model still declares, and two declarations claiming one old name. A hint naming something neither the previous snapshot nor the model holds is inert and silent, which is every hint after its migration.
- A rename and a type or nullability change in one edit render in that order in one step. A column hint inside a renamed table resolves against the old table. A column renamed twice before generating names the last name the snapshot knows.
- A hint is not a schema change by itself: adding or removing an inert one generates no migration (ADR-0027).
- An enum label rename is `ALTER TYPE … RENAME VALUE` on Postgres and nothing on SQLite, where the labels are text in rows; whether that case scaffolds a data step there is the SQLite decision's.
