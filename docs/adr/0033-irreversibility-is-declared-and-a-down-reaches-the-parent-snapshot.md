# Irreversibility is declared, and a down reaches the parent snapshot or refuses

Migration `0006` has a data step that overwrites `card.legacy_flavor`. The old values are gone, so the step cannot be reversed, and its author says so where a reader looks for the reverse:

```python
def up(ctx): ...

@irreversible("up overwrote legacy_flavor; the old values are gone")
def down(ctx):
    pass
```

Every step has a down. A data step's `.py` must define `down`; a hand-written `.sql` step must ship a `.down.sql`; the generator writes one beside every DDL step it generates. A down is one of three things:

- **work**: SQL, or a `down` body, atomic or chunked like any `up`;
- **nothing to reverse**: `@nothing_to_reverse("01's down drops card.flavor")`, or a `.down.sql` holding `-- ferro: nothing-to-reverse <reason>`;
- **irreversible**: `@irreversible("<reason>")`, or `-- ferro: irreversible <reason>`.

The reason is required in both declarations. A missing down, an empty `.down.sql` with no directive, and an undecorated `def down(ctx): pass` are refused when the migration is loaded: each is indistinguishable from a forgotten one.

Both declarations are read when the step file is loaded, before a run executes anything. `down --to 0005` with `0006:02` irreversible refuses at plan time, reverts nothing, and quotes the reason. There is no flag to skip the step. The way through is to write the down: a step record's checksum covers only the up file (ADR-0030), so a down added or fixed later is never a mismatch.

A generated DDL down is the one planner run backwards, from the migration's schema snapshot to its parent's. The rule it obeys: after the down, the database shows no drift against the parent snapshot, or the down fails. It never lands on a relaxed schema.

- A dropped `NOT NULL` column comes back `NOT NULL`, marked `-- ferro: data-dependent`: it succeeds on an empty table and fails on a populated one, where the developer edits the down to supply values. A down restores schema, never data.
- An added enum label stays. Postgres cannot drop a value, and an extra live label is not drift (ADR-0011), so the down is `nothing-to-reverse`.
- Row security the migration introduced is torn down: its policies dropped, `FORCE` and `ENABLE` reversed. The reconciliation pass treats those flags as one-way because it cannot know who set them; a migration whose parent snapshot has no row security on the table can.
- A rename's down is the reverse rename with every owned name (ADR-0032).

Amended by ADR-0050 (2026-10-07): the down is the planner run from the step's after-state to its before-state, keeping only the ops whose artifact the step's up touched, through the one function the Alembic bridge's `downgrade()` uses too. It is planned with destructive changes on (it drops what the up added), and its file still never carries the `destructive` header.

A generated down is never irreversible. Only a person declares that.

## Considered options

- **Forward-only: no `down` at all** (Atlas, sqldef, Prisma in production). Rejected: the development loop is generate, apply, dislike, revert, regenerate, and the Alembic peer offers a downgrade. `down` is a tool a project may use, not a promise that every migration reverses.
- **`raise ctx.Irreversible(...)` inside `down()`** (Django's `IrreversibleError` shape). Rejected: a raise exists only once the function runs, by which time every later step has been reverted and the database is stranded inside a migration. The decorator keeps what the raise was wanted for, a required `down` with the reason in plain sight, and the planner can read it.
- **Absence means irreversible.** Rejected: nobody weighs a function that is not there, and a scaffolded backfill, whose reverse is rightly empty, would make every required-column migration irreversible.
- **A skip flag for irreversible steps.** Rejected: the records would say a step is reverted whose effects stand.
- **Re-add a dropped `NOT NULL` column as nullable.** Rejected: the down would succeed and leave drift against the snapshot it claims to restore.
- **Rebuild the enum type to remove an added label.** Rejected as the generated down: it needs a data step for the rows holding the label, which is the label-removal shape a developer writes on purpose.
- **Store down SQL in the tracking table so any checkout can revert.** Rejected: a second copy that goes stale the moment a down file is fixed. A database ahead of the checkout is refused, naming the migrations the checkout lacks.

## Consequences

- `down` with no target reverts the latest applied migration, a partly applied one included. `--to 0005` leaves `0005` fully applied. `--to 0007:02` leaves steps 01 and 02 of `0007` applied: expand, backfill and contract share a migration, and undoing a contract must not cost the backfill. Steps revert in reverse order under one run lock, and `up` resumes from wherever a down stopped.
- A reverted step's record is removed in the transaction of its down. A chunked down keeps the record, as reverting with the down's own cursor, until its last batch; `up` refuses while a record is reverting. Reverting a chunked `up` that only got partway runs the down's whole query.
- A run stops at the first down that fails. That step's record stands, with the failure written to its diagnostics.
- A data step's down sees the same historical models as its up (ADR-0025): the steps after it are already reverted.
- `down` refuses at plan time to go below a baselined migration (ADR-0031). `baseline --remove` deletes the baseline records, touches no schema, and refuses while a run-origin migration above them is applied.
- Down files carry `data-dependent` where it applies and never `destructive`: nearly every down of an add is a drop.
- `down` asks for confirmation before executing; `--yes` skips the prompt.
- A deliberate floor ("never revert below this") is an irreversible step. Baseline is not that mechanism.
