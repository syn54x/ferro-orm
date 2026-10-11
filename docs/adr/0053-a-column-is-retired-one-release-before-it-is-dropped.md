# A column is retired one release before it is dropped

A server that migrates in its deploy step runs the migration while the previous release's instances still serve, so every change must be survivable by the previous release's code. ADR-0040 covers adding a required column across two releases. Removing a column had no shape: ferro's `INSERT` and `UPDATE` name every column the model declares (`save_bind_payload` is a full `model_dump`), so the moment release B's migration runs `DROP COLUMN "legacy"`, release A, whose model still declares `legacy: str | None`, fails on every write for the whole overlap window, and a one-release rollback is impossible. "Make it nullable first" does not help: release A still names the column.

We decided that a column leaves the schema in two releases, and that the first is a declaration on the field:

```python
class Merchant(Model):
    identity_key: str | None = Field(retired=True)          # or Annotated[str | None, Field(retired=True)]
    source: Annotated[Source | None, ForeignKey(retired=True)]

class Status(StrEnum):
    ACTIVE = "active"
    CANCELED = "canceled"
    __ferro_retired_labels__ = frozenset({"canceled"})
```

A **retired column** stays in the schema snapshot, nullable, and leaves the running model: the metaclass strips the field, so `merchant.identity_key` raises `AttributeError`, `Merchant(identity_key=…)` raises `TypeError` naming the field as retired (ferro's own check, ahead of Pydantic's `extra` handling, which would drop the keyword silently), a query proxy over it raises its usual `AttributeError` with "retired in this release", and no `INSERT`, `UPDATE` or `SELECT` the model issues names it. Retiring a required column generates `ALTER COLUMN … DROP NOT NULL` (a table rebuild on SQLite); retiring a nullable one renders no DDL and writes no migration (ADR-0027). Nothing else about the column changes: its index, unique, check and foreign key, and every table check, row policy, composite index or unique that names it, stay as declared, so the one change per retirement is the relaxation old code needs. Deleting the declaration in the next release generates the `DROP COLUMN`, destructive, as today, and when the column was the last of a native enum type, the `DROP TYPE` the planner already plans after it. Historical models expose a retired column as an ordinary column, since a data step is the one place this release may touch it (the online rename recipe: add the new column, dual-write in app code, retire the old, drop the old; `renamed_from` stays the single-release rename). The create pass, a generated `0001`, drift and every other door read the column from the one IR, so a fresh database has the column an old release expects.

A **retired label** keeps its `StrEnum` member, so rows that hold it still hydrate, stays in the Postgres type and in any `db_check` column check, and is refused on assignment (construction or attribute set), the loud and earliest point; hydration bypasses validation (I-2), so saving a hydrated row that still holds the label writes it back unchanged, which is not a write of the label. Deleting the member and the declaration generates today's label removal.

## Considered options

- **A class-level mapping (`__ferro_retired_columns__ = {"source": str}`)**, or a `Retired[str]` type marker. Rejected: the column needs its type, `db_type`, index and check to stay in the snapshot, and the field already holds them; a second table of column facts, or a generic with nothing else to do, duplicates them.
- **Keep the field on the Pydantic model, always `None`, and drop it only from SQL.** Rejected: a field that exists and is never written is the quiet handling of a named case; the point of retiring is that this release's code cannot touch the column.
- **A retired column that keeps `NOT NULL` behind a server default.** Rejected: ferro persists no server default (ADR-0027), and the new release's `INSERT` omits the column, so nullable is the only shape that admits it.
- **Retiring also drops the column's index, unique, check or policies.** Rejected: old code is still writing the column under every constraint it was declared with, and a nullable column with any of them accepts the new release's `NULL`; a second schema change in the retire release is one more thing to review and nothing old code needs.
- **Refuse deleting a field that was not retired first.** Rejected: not every project does rolling deploys; the destructive marker and review are the gate, as for every drop.

## Consequences

- `Field(retired=True)` on a non-optional annotation, on a primary key, or together with a live `renamed_from` (old code names the old column) is a `TypeError` at class creation naming the fix; so is a `BackRef` over a retired `ForeignKey` (it renders no DDL; delete it). `BackRef` and `ManyToMany` cannot be retired: one is not a column, the other is a table.
- `__ferro_retired_labels__` holds label strings, validated against the members like `__ferro_renamed_labels__`'s keys.
- Retiring a required column is an `expand`-class change (ADR-0054); dropping the column is `contract`.
- One casebook case (retire a required column with an index, then delete it) carries the change through pins (a)–(g).
