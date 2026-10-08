# A plan's two sides are adapters, and a down is the up's artifacts planned back

Amends ADR-0041; extends ADR-0033. Typed plan reports are their own decision, ADR-0052.

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

ADR-0033 and ADR-0041 both say a down is "the planner run backwards". Until this decision, only the generator did that: a step's down was `plan_from_ir(after, before)` over two declared snapshots. The bridge had no declared "before". The live IR carries no check and no policy (those live in the introspected facts), and the planner accepted a live database only as the side it plans *from*. So the #533 ruling gave the bridge a hand-written inverse per op kind, `reverse_live_plan`, with its own variants (`RestoreCheck`, `RestoreRowPolicy`, `DropForeignKey`, `Irreversible`), its own renderer and its own JSON. That made two inversion mechanisms, and nothing kept them in step.

The same review found what to do with each op answered in about ten places: its phase, whether it needs a rebuild, whether it asks rows for values, whether it drops data. Each caller built its own context from its own pair of snapshots. The panel's F1, a column drop landing in the expand ahead of the backfill that reads it, came from that shape.

## A plan's sides are adapters

We decided that each side of a plan is one of two adapters, and the planner never asks which one it holds:

```rust
impl Side {
    pub fn declared(ir: IrEnvelope<SchemaIrPayload>) -> Side;
    pub fn live(ir: IrEnvelope<SchemaIrPayload>, facts: LiveFacts) -> Result<Side, PlanError>;
}
pub fn plan_from_ir(old: &Side, new: &Side, dialect: Dialect, options: PlanOptions) -> Plan;
```

- **Declared** is a modelset: the models, or a schema snapshot. Every artifact on it is ferro's own declaration, read from the IR.
- **Live** is a database, read as its IR plus the facts introspection returned beside it. A live side without facts for every table it holds is refused when it is built (`PlanError::MissingLiveFacts`), not halfway through a plan.

The planner asks the side questions instead of branching on a flag:

| Question | Declared | Live |
|---|---|---|
| a table's facts | read off the declaration | the introspected facts |
| an enum type's labels | from the declaration | from the facts |
| a check's or policy's body | the canonical expression, rendered as ferro renders it | the catalog's text, rendered as a restore |
| does this prove ferro installed the table's row security? | the declaration does | a ferro-named policy does |
| rebuild a policy body ferro cannot verify? | yes: ferro wrote it | no: warn (ADR-0019) |
| plan an enum label's removal? | yes | no: report the extra label (ADR-0011) |
| report standing live conditions? | no: the ops say it | yes |
| as the target, can it express this op? | yes | not for the two ops listed below |

A question the build finds that this table does not answer comes back to the maintainer as a design question. It is never answered with a new flag.

The plan holds its two sides and its dialect. The renderer reads every table, column and body from the plan, and no caller passes the sides a second time. Ops stay name-only, so the plan's JSON does not change.

## One verdict per op

The planner computes one verdict for each op it plans, once, and every door reads that verdict:

```rust
pub struct PlannedOp { pub op: MigrationOp, pub verdict: OpVerdict }
pub struct OpVerdict {
    pub execution: Execution,     // Native | Rebuild | Refused(Refusal) | Irreversible(String)
    pub demands_values: bool,     // asks existing rows for a value no statement supplies
    pub drops_data: bool,
    pub fails_on_rows: RowRisk,   // None | Always (cast, SET NOT NULL, unique) | WhenValidated (check, FK)
    pub recreates: bool,          // brings back a table or NOT NULL column whose rows are gone
    pub goes_with: Option<Rider>, // DroppedColumn | AddedColumn: rides that column's statement
}
```

The verdict states only what is true of the op, its two sides and the dialect. It does not know which way a file goes. A required column added with no default has `demands_values` set in both directions, and each reader acts on it:

- a migration's up sends it to the backfill;
- a migration's down marks the step `data-dependent`;
- the bridge writes it plain and marks it.

On SQLite such a column is also `Rebuild`, which a down rebuilds and the bridge refuses. Anything that depends on the door stays with that door's reader: whether constraints are staged `NOT VALID`, whether the migration has a data step, and which phase step an op lands in. The generator keeps the phase as one private table over the verdict and whether the migration has a data step. The per-op predicates that answer these questions today become private to the verdict's computation.

## A down is the up's artifacts planned back

```rust
pub fn plan_down(up: &[MigrationOp], after: &Side, before: &Side, dialect: Dialect) -> Plan;
```

A down is the planner run from the up's after-side to its before-side, keeping only the ops whose artifact the up touched. Both doors use this one function:

- A generated step's down is planned between the stages on either side of that step, and both stages are declared.
- The bridge's `downgrade()` is planned from the models (declared) to the database (live).

Renames come from the up's own rename ops, swapped, so a down needs no hints of its own.

A down is planned with destructive changes on: a column, table or index the up added is dropped by the down. That is a planning option, not a header. ADR-0033's rule that a down file never carries the `destructive` header stands, because nearly every down of an add is a drop and the header would carry no information.

The scope is the artifact, not the table or the type: a column, an index, a named constraint, a policy, a row-security flag, an enum type, one enum label, a table. An added column's scope includes the index, check and foreign key that ride its statement, so the down's drop of the column takes them with it.

As built (2026-10-08, #600): a foreign key's key is its column, not its name. The name follows the target (`fk_<table>_<column>_<to_table>`), so an up that retargets `fk_card_owner_id_team` to `fk_card_owner_id_org` would leave the down's rebuild out of scope if keys were names. A table's primary key is its own key.

Scoping is what keeps the warn-only categories one-way. The forward plan is asymmetric on purpose, and an unscoped reverse would undo things the up never did:

| The up (live → declared) | The planner backwards, before scoping | The down |
|---|---|---|
| type `status` holds `[a, x]` live and `[a, y]` declared: the up adds `y` and reports `x` | remove `y`, add `x` | removing `y` is irreversible (below), and adding `x` is out of scope |
| table `t` holds the foreign policy `audit`; the up adds `rls_t_owner` | drop `rls_t_owner` (the planner never plans a foreign name) | drop `rls_t_owner` |
| a leftover `ck_t_old` that the pass did not drop (no `migrate_destructive`) | add `ck_t_old` | nothing, because the up never touched it |
| the same leftover, dropped by the bridge, which plans destructively | add `ck_t_old` | add `ck_t_old` with the body the catalog printed |

These four cases are the required pins for `plan_down`.

## What a live target cannot express is irreversible

A live side refuses two ops as the target, and each refusal carries a reason the revision's reader can act on:

- **Removing an enum label.** Labels are append-only (ADR-0011), and rows may hold the label.
- **Putting back a row policy that applies `TO` a role list other than the default.** Ferro's `CREATE POLICY` never writes that clause, so the policy has to be restored by hand.

The refusal becomes the op's `Irreversible` verdict, and the revision renders it as a `raise` carrying the reason. Neither refusal reaches a generated down, which stays never irreversible (ADR-0033). Between two declared stages an added label stays, so the labels step's down says `nothing-to-reverse`. A declared side writes every policy it declares. A check or foreign key SQLite cannot restore in place is not a live-side refusal. It is `Rebuild` on any side, and because a revision cannot write a rebuild, the bridge's downgrade makes it irreversible with that reason, just as its upgrade refuses it. Nothing in this design produces the old "the live database holds nothing to restore" outcomes: a scoped down plans only toward what the live side holds.

## The generator plans once

For each target dialect, the generator plans the parent snapshot to the target once.

1. Each op's phase comes from its verdict.
2. One private `Stages` value builds the schemas between the steps (the expanded schema, the schema after the index steps, the contracted target) by applying each phase's ops in turn, with every demanded column held nullable until the contract.
3. Each step's up renders its ops between the stages on either side of it.
4. Each step's down is `plan_down(step_ops, stage_after, stage_before, dialect)`.

A phase is assigned once, so the refusals that guarded against a re-plan producing an op in the wrong phase are no longer needed and go. Index steps are built from the planner's own index ops (ADR-0051).

The generator also lays out the steps a person asks for (`--sql-step`, `--data-step`) on both paths, whether or not the models changed: the SQL step right before the data step, or last. A migration made only of such steps stores the **target** snapshot, as the glossary defines a schema snapshot ("the declared modelset as it was when a migration was generated"). Today Python lays out that case itself and copies the parent's snapshot.

## One decider, one renderer

- **What an added column carries.** "An added column brings its single-column index or unique, its column check and its foreign key with it" is decided by one function, `column_riders(model, column)`. The planner's skip rules, the column's rendering, the staged constraints and the verdict's `goes_with` all call it.
- **Row-security flags.** The row-security flag deciders return flags (`Vec<RowSecurityFlag>`, one of `Enable`, `Force`, `NoForce`, `Disable`), and the reconcile decision carries only names and flags. The planner turns them into ops, and only the plan's renderer writes SQL. Nothing maps rendered SQL back to an op.
- **The generator's interface.** The generator exposes `generate`, `generate_with`, `check_migrations`, their option, result and error types, and the reserved names a rebuild and a staged `NOT NULL` use. Every helper module behind them is private to the crate.

## Considered options

- **Make the per-op inverse the only down, the generator's included.** Rejected: it amends ADR-0033 and ADR-0041 to a hand-written reverse per op kind, which ADR-0041 already rejected for Alembic's own `reverse()`, and every new op would need its inverse written twice.
- **Keep two down shapes and share one renderer.** Rejected: the parity between the two doors' downs would still be maintained by hand.
- **Run the planner backwards without scoping.** Rejected: an extra live label, a foreign policy, or a leftover check the up left alone would be "restored" by a down whose up never removed it.
- **Carry a live body on the op** (`AddCheck { body }`). Rejected: it changes the plan JSON that `drift` and the translator read, for a value the plan's target side already holds.
- **Keep a side flag beside the facts.** Rejected: that is the shape being replaced. About six places branched on it, and nothing stopped a seventh.
- **Have the generator own the verdict and know the file's direction**, with the bridge calling it. Rejected: the direction would leak into every reader, and the bridge would depend on the generator for a fact the planner already has.
- **Keep re-planning each stage and check the phases afterwards.** Rejected: that is how F1 shipped. A phase checked after the fact is a refusal; a phase assigned once cannot be wrong in that way.
- **Carry the riders as fields on `AddColumn`.** Rejected: it changes the plan JSON, and every other op reads its content from the plan's sides.
- **Leave the hand-step-only migration to Python.** Rejected: two deciders of step order and two step namers, and the parent's snapshot contradicts the glossary.

## Consequences

- To be deleted:
  - the planner's `OldSide` flag, the side field on `LiveFacts`, and the normalisation "the snapshot side reads no fact";
  - `reverse_live_plan`, `ReversePlan`, `ReverseOp`, `RenderedReverseOp`, `render_reverse_plan` and the bridge's re-rendering of `unrendered` steps;
  - the exported per-op predicates and `_plan_step_verdicts`;
  - the generator's phase re-checks;
  - the hand-step layout in Python.
- Every way an op is undone, restoring a check from its catalog body included, becomes an op of the one planner rendered by the one renderer, so the two doors' downs agree by construction.
- The down of an added foreign key becomes the planner's own foreign-key drop, which ADR-0051 adds on every door.
- ADR-0041's downgrade paragraph is amended to point here. AGENTS.md I-1 is to name `plan_down`, the flag deciders and the verdict once tier C2's rewrite of items 12–16 lands.
