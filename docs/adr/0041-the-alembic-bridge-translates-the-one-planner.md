# The Alembic bridge translates the one planner

A model gains a column and a check over it:

```python
class Card(Model):
    flavor: str | None = None
    __ferro_checks__ = (Check("flavor_set", lambda card: card.flavor != None),)
```

`alembic revision --autogenerate` writes two lines for it, and until this decision two different deciders produced them: SQLAlchemy reflection compared the live table against the `MetaData` built from the models and proposed the `add_column`; ferro's own comparator queried `pg_constraint`, asked the Rust core, and proposed the check. The reconciliation pass answered the first question a third way, through `plan_from_ir`. Keeping those answers in step by hand is what AGENTS.md I-1's parity list paid for, and every new artifact (rename hints, SQLite inline checks, persisted defaults) added a row to it.

We decided that the bridge has **one decider**: it reads the live database through the converter the reconciliation pass uses, calls the one planner (ADR-0023's, grown to carry checks, enum labels and row security on both sides), and translates the planner's ops into the revision.

```python
def upgrade():
    op.add_column('card', sa.Column('flavor', sa.String(), nullable=True))                     # an op with an Alembic twin
    op.execute("ALTER TABLE card ADD CONSTRAINT ck_card_flavor_set CHECK (flavor IS NOT NULL)")  # an op without one
```

**Native where Alembic has a twin, the pass's statement where it does not.** Create and drop table, add and drop column, type and nullability changes, indexes, foreign keys, and table and column renames are written as Alembic's own ops, built from the same `sa.Column` the bridge builds today. Everything else (label addition, check rebuilds, row security, renames of owned names, enum label renames) is `op.execute` of the byte-identical statement the pass would run. The destructive and data-dependent markers the in-house generator writes as `-- ferro:` headers are Python comments above the op.

**The bridge sees exactly what the reconciliation pass sees.** It plans with destructive changes on, since a revision is reviewed before it runs. An empty autogenerate and "no drift" are the same statement. Anything on a ferro table that ferro never declared (a hand-set server default, a comment, a foreign constraint) is no longer proposed. A live table no model declares stays Alembic's, as do a project's own SQLAlchemy tables, which keep Alembic's full comparison. (Amended 2026-10-07: a dropped model's table is the planner's drop; see "Amended at the epic's close" below.)

**Alembic is told to leave ferro tables alone, in `env.py`, and autogenerate refuses when it was not.**

```python
context.configure(connection=connection, target_metadata=get_metadata(), **ferro_options())
```

`ferro_options()` carries the `render_item` hook and an object filter that hides ferro tables and both tracking tables from Alembic's own comparator, wrapping any filter the project passes in. The tables stay in `target_metadata`, so a SQLAlchemy table's foreign key to a ferro table still resolves.

**`downgrade()` is the planner run backwards**, declared to live, through the same translator: the mechanism ADR-0033 gives the in-house door. The bridge has no snapshot, so a re-added column returns with what the converter can read (type, nullability, index, foreign key, checks). A step the planner calls irreversible renders as a `raise` carrying its reason.

**Rename hints are honoured.** A hint is live while the database holds the old name and not the new one, so the same model means a rename on both doors.

**Data steps are the bridge's boundary.** A change that demands values of existing rows is written as the plain op, marked, and names the door that generates the backfill:

```python
# ferro: data-dependent (fails while card has rows; in-house migrations generate the backfill)
op.add_column('card', sa.Column('flavor', sa.String(), nullable=False))
```

A primary-key change is refused, as it is in-house until the restructure scaffold ships. On SQLite, a change that needs a table rebuild is refused at autogenerate and points at in-house migrations (ADR-0034); changes SQLite alters natively still autogenerate.

**A tracked database refuses autogenerate over ferro models.** The refusal names `ferro migrate new` and tells a project whose Alembic chain still manages its own SQLAlchemy tables to drop `get_metadata()` from `env.py` and keep the filter. Autogenerate writes nothing, so it takes no run lock. `alembic upgrade` runs a plain revision file with no ferro code in it and is not policed; an old revision applied to a tracked database is drift.

**`get_metadata()` reads the project configuration when there is one.** It imports the configured database's `models` through `FerroSettings` with the CLI's import rules and returns only that database's tables (`get_metadata(database="…")` when several are configured). A registered model whose module no configured database lists is refused by name. With no configuration it behaves as before.

## Considered options

- **Keep the per-family comparators.** Rejected: "peer" becomes a tax that grows with every artifact, and the two deciders can still disagree, which is what a phantom diff is.
- **One decider, the whole revision as `op.execute`.** Rejected: one pin instead of two, bought by making revisions dialect-locked SQL with no typed ops to edit. A chain could no longer run on SQLite in tests and Postgres in production.
- **Take ferro tables from Alembic automatically**, deleting its ops after the fact. Rejected: Alembic still logs changes that then vanish, and the precedent (`render_item`) is a wired line that is refused when missing.
- **Alembic's batch mode on SQLite.** Rejected: it has no pragma handling, so the drop cascades into `ON DELETE CASCADE` children.
- **A backfill scaffold in the revision.** Rejected: a second, weaker generator with no historical models behind it.
- **Alembic's per-op `reverse()` for the downgrade.** Rejected: one hand-written reverse per family, and ordering that leans on `UpgradeOps.reverse()`.

## Consequences

- AGENTS.md I-1: the "Alembic comparator consumes … over FFI" clauses of items 11–16 collapse into "the bridge translates the one planner's ops", carried by two pins: every planner op translates to a revision whose executed DDL matches the pass's, and the existing type-rendering parity test. Item 17 (enum type provenance) stays its own rule: it is decided from the revision alone and has no runtime twin. (Amended 2026-10-07; see "Amended at the epic's close" below.)
- AGENTS.md I-12: the slot rules go. Ferro registers one comparator, and the translator never reorders the planner's ops.
- The switch ships in one release, after the planner and the live converter grow; the bridge keeps its per-family comparators until then.
- An existing `env.py` is refused on its next autogenerate with the `ferro_options()` line to add.

## Amended at the epic's close (2026-10-07, #577)

**A dropped model's table is the planner's drop.** A project deletes `class Tag(Model)`, and the live database still holds `tag` with a native enum `tagkind` that only `tag` uses. Stock Alembic writes `drop_table('tag')` and never drops `tagkind`, so the type outlives the revision. The bridge therefore adds to the planner's table list every live table of the default schema that `target_metadata` does not declare and that the context's name and object filters admit. That is exactly the set Alembic's own comparison would drop, found the same way. The planner drops those tables, the `DROP TYPE` for a type only they used follows, and the downgrade puts back the type and then the table, with its checks and policies.

- Alembic's version table and ferro's tracking tables are never candidates. A table the context's filters exclude stays untouched.
- The downgrade restores what the live converter reads (columns, types, nullability, indexes, foreign keys, checks, policies), the same limit this ADR already accepts for a re-added column. Anything else on the table, such as a hand-set server default or a comment, is not put back. This applies to a removed SQLAlchemy-only table too, where Alembic's reflection would have restored more.
- A generated revision is therefore the reconciliation pass's changes plus these drops. The pass never drops a whole table.

Rejected: leaving `drop_table` to Alembic and appending only the `DROP TYPE`, which brings back two deciders and the cross-op ordering rules I-12 carried; and restricting the drop to tables lineage shows were ferro's, which the bridge cannot do because it has no snapshot and a tracked database refuses autogenerate.

**Item 17 folds into item 11.** On every door an enum type is the planner's guarded `CREATE TYPE` and its `DROP TYPE`, and the bridge renders every enum column `create_type=False`; the type decision is the one planner's, made against the live database, not a separate rule decided from the revision alone. ADR-0020..0022 are superseded in part.

## Amended by ADR-0050, ADR-0051 and ADR-0052 (2026-10-07, deepening tier B)

**`downgrade()` is to be the planner run backwards, literally.** The #533 ruling gave the bridge a hand-written inverse per op kind, because the planner accepted a live database only as the side it plans from. ADR-0050 makes both sides of a plan adapters. The downgrade is to be the one down function every door uses, `plan_down(up, after, before, dialect) -> Plan`, planned from the models (declared) to the database (live) and keeping only the ops whose artifact the upgrade touched.

- A dropped or rebuilt check or policy is to come back with the body the catalog printed, read from the live side.
- An op the live side cannot express (removing an enum label, a policy applying `TO` a role list) is to be the op's irreversible verdict, and the revision is to render it as a `raise` carrying the reason.
- A check or foreign key SQLite cannot restore in place is a table rebuild, which the downgrade is to make irreversible, as the upgrade refuses it.

**The revision is to come from one planner call:**

```rust
pub fn plan_revision(live: &Side, declared: &IrEnvelope<SchemaIrPayload>, dialect: Dialect) -> Result<Revision, RevisionRefusal>;
pub struct Revision { pub upgrade: Vec<RevisionOp>, pub downgrade: Vec<RevisionOp>, pub reports: Vec<Report> }
pub struct RevisionOp { pub op: MigrationOp, pub statements: Vec<String>, pub marker: Option<Marker>, pub irreversible: Option<String> }
```

That call returns the upgrade's and the downgrade's ops, each with its statements, its marker (`destructive` or `data-dependent`) and any irreversible reason. The rules that turn verdicts into a revision become the core's, tested without a database:

- refuse a rebuild;
- write a demanding change plain, with its marker;
- drop an op that renders nothing;
- refuse an op whose rendering blocks.

The translator is to build Alembic ops from that answer and decide nothing. A refusal keeps today's exact text. A refused rename hint is to reach the bridge as a typed report (ADR-0052), never matched by its text.

**A redefined index and a removed foreign key are to be planner ops** (ADR-0051). The bridge is to write them as Alembic's drop and create, and as `op.drop_constraint`.

As built (2026-10-08, deepening B4): `plan_revision` has the signature above, and `_core._plan_revision(live_ir_json, facts_json, declared_json, dialect)` returns it as a dict. `RevisionOp` carries three more fields so that the translator decides nothing. `twin` says whether Alembic's own op writes the op or `op.execute` of its statements does: a column add with a literal default, or with SQLite's inline `REFERENCES` / `CHECK`, is the pass's statement. `index` holds a `RedefineIndex`'s `(columns, unique)`, read from the side the revision leads to. `row_security_statements` holds what an `AddTable` runs beside Alembic's `create_table`. `Marker` carries its comment text. `RevisionRefusal` has six variants: `HintRefused`, `Refused` (a primary-key change or an enum type move), `SqliteRequiredColumn`, `Rebuild`, `Blocked`, and `Render`, which is an emission error and raises `ValueError` as before. `Revision.reports` holds only the upgrade's one-off reports, the ones the revision writes as comments. An upgrade with nothing to write returns an empty revision.
