# Every door plans a redefined index and a removed foreign key

Extends ADR-0013 and ADR-0044; follows from ADR-0050.

Two model edits that the generator, the reconciliation pass and `drift` read differently today:

```python
class SubscriptionInvoiceLine(Model):
    # was (("billing_period_start", "billing_period_end"),)
    __ferro_composite_indexes__ = (("billing_period_start", "billing_period_end", "customer_id"),)
    team_id: int   # was team: Annotated[Team, ForeignKey(...)]
```

Ferro has no hand-picked index names. An index's name is built from its table's name and its columns' names, `idx_<table>_<col1>_<col2>…` (`uq_` for a unique), and cut to Postgres's 63-character limit (`composite_index_name`, AGENTS.md I-1). Two different definitions can therefore get the same name in two ways:

- **Truncation.** The table above is `subscriptioninvoiceline`. Both column groups produce a name longer than 63 characters, and both are cut to the same 59 characters plus `_idx`: `idx_subscriptioninvoiceline_billing_period_start_billing_pe_idx`.
- **Underscore joins.** The groups `("order_id", "kind")` and `("order", "id_kind")` on one table both join to `idx_<table>_order_id_kind`.

A live database can also hold a ferro-named index whose definition differs from the declaration, written by hand or by an older build.

The generator saw the first edit, because its index steps diffed indexes on their own, by definition, and built the new index over the old one. The planner compared indexes by name only. So the reconciliation pass left the old index in place, `drift` reported nothing, and the Alembic bridge wrote nothing.

The second keeps the column `team_id` and stops declaring its foreign key. No door planned anything: the planner only walked the foreign keys the target declares. The column kept its constraint, `drift` was silent, and a generated down that should have dropped a foreign key the up added to an existing column left it standing. Those are an AGENTS.md I-1 gap (one artifact, decided differently per door) and an I-6 gap (a declared change dropped silently).

We decided that the planner plans both, and every door consumes the planner's op.

- **A redefined index.** When a ferro-owned index keeps its name and changes its columns or its uniqueness, the planner plans `RedefineIndex { table, name }`: drop the index, then create it as declared. The pass runs it under `migrate_updates` and does not gate it on `migrate_destructive`, because nothing a row holds is discarded. Making an index unique can fail on existing rows, so a duplicate fails the statement with the count and the fix named, as a unique index step does. The generator writes it as the change's own index step (ADR-0044), and its index steps are to be built from the planner's index ops and nothing else. `drift` reports it. The bridge writes it as Alembic's drop and create.
- **A removed foreign key.** When a ferro-owned foreign key on a column that both sides keep is no longer declared, the planner plans `DropForeignKey { table, column, name }`. The pass runs it only under `migrate_destructive`, where ADR-0013's ladder puts every other constraint and index removal. The generator always writes it (review is the gate). On SQLite it is a table rebuild (ADR-0034). `drift` reports it. The bridge writes `op.drop_constraint`.

Each op is to be pinned on all three doors (the pass, `drift`, the bridge) and in the generator, and the Auto-migrate guide's table of what each flag does is to gain both rows. Both causes of a same-name redefinition, truncation and an underscore join, are to get a pin each, as is a live ferro-named index with a different definition.

## Considered options

- **Keep the generator's own index diff.** Rejected: two deciders for one artifact, and the pass and `drift` stay blind to a redefinition.
- **Gate a redefinition on `migrate_destructive`.** Rejected: the ladder gates removals of what the model no longer declares. A redefined index is still declared, and leaving the old definition in place keeps a query plan or a uniqueness rule the model no longer states.
- **Overload `RebuildIndex`.** Rejected: that op rebuilds an index the live database marks invalid, is planned only against a live database, and has no declared change behind it.
- **Leave a removed foreign key to Alembic or to hand-written SQL.** Rejected: the constraint is ferro-owned by its `fk_` name, and the down of an added foreign key needs the drop (ADR-0050).

## Consequences

- `staging::index_ops`, its `IndexOp` and the undo logic in `schema_shape` are to be deleted, and the generator's phase table is to assign the planner's index ops to index steps.
- A database brought up by the pass before this change, holding an old definition under a ferro-owned name, will get the redefinition on its next `migrate_updates` connect, and `drift` will report it until then.
- The bridge's hand-written `DropForeignKey` inverse is to be replaced by the planner's op.
