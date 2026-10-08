# A plan's two sides are adapters, and a down is the up's artifacts planned back

Amends ADR-0041; extends ADR-0033.

A model drops its check, and a project writes the change twice, once as a migration and once as an Alembic revision against a database that holds the check:

```python
class Card(Model):
    flavor: str | None = None
    # __ferro_checks__ = (Check("flavor_set", lambda card: card.flavor != None),)   # removed
```

```sql
-- up, on both doors
ALTER TABLE "card" DROP CONSTRAINT "ck_card_flavor_set";
-- down, on both doors
ALTER TABLE "card" ADD CONSTRAINT "ck_card_flavor_set" CHECK ((flavor IS NOT NULL));
```

ADR-0033 and ADR-0041 both say a down is "the planner run backwards". Until this decision only the generator did that: a step's down was `plan_from_ir(after, before)` over two declared snapshots. The bridge had no declared "before". The live IR carries no check and no policy (they live in the introspected facts), and the planner accepted a live database only as the side it plans *from*. So the #533 ruling gave the bridge a hand-written inverse per op kind instead: `reverse_live_plan`, with its own variants (`RestoreCheck`, `RestoreRowPolicy`, `DropForeignKey`, `Irreversible`), its own renderer and its own JSON. That meant two inversion mechanisms, and nothing kept them in step.

## A plan's sides are adapters

We decided that each side of a plan is one of two adapters, and the planner never asks which one it holds.

- **Declared**: a modelset, either the models or a schema snapshot. Every artifact on it is ferro's own declaration, read from the IR.
- **Live**: a database, read as its IR together with the facts introspection returned beside it. Building one without facts for every table it holds is an error at construction, not halfway through a plan.

The planner asks the side questions instead of branching on a flag:

- the facts of a table;
- an enum type's labels;
- a check's or a policy's body;
- whether it proves ferro installed a table's row security;
- whether it rebuilds a policy body ferro cannot verify;
- whether it plans label removals or only reports extra labels;
- whether it reports standing live conditions or leaves them to the ops;
- whether, as the target, it can express an op at all.

Each answer belongs to the adapter. The declared side proves row security by its declaration, rebuilds the bodies ferro wrote, and plans label removals. The live side needs a ferro-named policy as proof, warns about bodies it cannot verify (ADR-0019), and warns about extra labels (ADR-0011).

A body comes from the side it is planned toward. A declared side gives the canonical expression, which is rendered as ferro renders it today. A live side gives the text the catalog printed, which is rendered as a restore. Ops stay name-only, and the plan holds its two sides, so the renderer asks the plan for the body and the wire shape of an op does not change.

## A down is the up's artifacts planned back

A down is the planner run from the up's after-side to its before-side, keeping only the ops whose artifact the up touched. One function does it for both doors. A generated step's down is planned between the stages on either side of that step, both declared. The bridge's `downgrade()` is planned from the models (declared) to the database (live). The down is always destructive, since it removes what the up added. Renames come from the up's own rename ops, swapped, so a down needs no hints of its own.

The scope is the artifact, not the table or the type: a column, an index, a named constraint, a policy, a row-security flag, an enum type, one enum label, a table. An added column's scope includes the index, check and foreign key that ride its statement, so the down's drop of the column takes them with it.

Scoping is what keeps the warn-only categories one-way. The forward plan is asymmetric on purpose, and an unscoped reverse would undo things the up never did:

| The up (live → declared) | The planner backwards, before scoping | The down |
|---|---|---|
| type `status` holds `[a, x]` live and `[a, y]` declared: the up adds `y` and reports `x` | remove `y`, add `x` | removing `y` is irreversible (below), and adding `x` is out of scope |
| table `t` holds the foreign policy `audit`; the up adds `rls_t_owner` | drop `rls_t_owner` (the planner never plans a foreign name) | drop `rls_t_owner` |
| a leftover `ck_t_old` that the pass did not drop (no `migrate_destructive`) | add `ck_t_old` | nothing, because the up never touched it |
| the same leftover, dropped by the bridge, which plans destructively | add `ck_t_old` | add `ck_t_old` with the body the catalog printed |

## What a live target cannot express is irreversible

A live side refuses two ops as the target, each with a reason the revision's reader can act on:

- **Removing an enum label.** Labels are append-only (ADR-0011), and rows may hold the label.
- **Putting back a row policy that applies `TO` a role list other than the default.** Ferro's `CREATE POLICY` never writes that clause, so the policy has to be restored by hand.

The refusal is the op's irreversible verdict, and the revision renders it as a `raise` carrying the reason. A check or foreign key that SQLite cannot restore in place is not a live-side refusal. It is a table rebuild on any side, and a revision cannot write a rebuild, so the bridge's downgrade makes it irreversible with that reason, as its upgrade refuses it. The old "the live database holds nothing to restore" outcomes no longer exist: a scoped down only plans toward what the live side holds.

## Considered options

- **Make the per-op inverse the only down, the generator's included.** Rejected: it amends ADR-0033 and ADR-0041 to a hand-written reverse per op kind, which ADR-0041 already rejected for Alembic's own `reverse()`, and every new op would need its inverse written twice.
- **Keep two shapes and share one renderer.** Rejected: the parity between the two doors' downs would still be maintained by hand.
- **Run the planner backwards without scoping.** Rejected: an extra live label, a foreign policy, or a leftover check the up left alone would be "restored" by a down whose up never removed it.
- **Carry a live body on the op** (`AddCheck { body }`). Rejected: it changes the plan JSON that `drift` and the translator read, for a value the plan's target side already holds.
- **Keep a side flag beside the facts.** Rejected: that is the shape being replaced. About six places branched on it, and nothing stopped a seventh.

## Consequences

- `reverse_live_plan`, `ReversePlan`, `ReverseOp`, `RenderedReverseOp`, `render_reverse_plan` and the bridge's re-rendering of `unrendered` steps are deleted. The planner's `OldSide` flag, `LiveFacts`' side field and the normalisation "the snapshot side reads no fact" are deleted too.
- Every way an op is undone, restoring a check from its catalog body included, is now an op of the one planner rendered by the one renderer. The parity of the two doors' downs holds by construction.
- The down of an added foreign key is the planner's own foreign-key drop, which ADR-0051 adds on every door.
- ADR-0041's downgrade paragraph is amended to point here, and AGENTS.md I-1 names the down's one function.
