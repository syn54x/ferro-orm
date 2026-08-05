# Label addition is update-gated and append-only

A native Postgres enum type that already exists is an existing schema object,
so evolving its label set belongs to the **reconciliation pass**
(`migrate_updates`), not the create pass — the same line ADR-0010 drew for
indexes. The reconciliation pass performs **label addition** (`CONTEXT.md`):
it appends model-declared labels missing from a live ferro-owned enum type,
and does nothing else. Labels the database has but the model lacks are warned
about loudly and never removed — rows may still hold them, and old code may
still be running against the new schema during a rolling deploy. A type is
ferro-owned by **derivation**: its name matches the name ferro derives from
the model (enum types carry no `idx_`/`uq_`-style prefix, so prefix doctrine
cannot apply). The additive-only scope is what makes derivation-based
ownership safe: the worst misattribution appends a label; it never drops or
rewrites anything.

Both migration doors consume one decision: the label diff (model labels +
live `pg_enum` labels → additions + warnings) lives in the Rust core and is
consumed mechanically by the auto-migrate planner and by an Alembic
autogenerate comparator in the bridge — the same one-decision-table seam as
`resolve_column_storage` (AGENTS.md I-1). Alembic autogenerate has no
`migrate_updates` gate: running autogenerate is itself the request for a
diff, so the comparator always reports; parity is in the decision, not the
gate. `ALTER TYPE ... ADD VALUE` executes outside transactions on both paths
(an autocommit pre-pass before the per-table transactions in auto-migrate;
`autocommit_block()` in generated revisions) — Prisma shipped the
in-transaction version and it is a graveyard (prisma#7251, #5290, #8424).

Decision by owner (2026-08-05), grilling #328.

Rejected alternatives:

- **Ensure semantics under plain `auto_migrate`** ("the type guard already
  runs in the create pass; label addition is just a stronger ensure"):
  re-opens the two-owners divergence ADR-0010 closed — an existing object
  whose shape plain `auto_migrate` sometimes changes. The create pass stays
  introspection-free and silent about drift of every kind, enums included.
- **Automatic removal/rename** (full sync, as `alembic-postgresql-enum`
  does): removal requires a type-replacement dance and human judgment about
  rows holding the removed label; every implementation that automated it
  grew a bug tracker around it. Reviewed-migration territory, permanently.
- **Recommending `alembic-postgresql-enum`** instead of our own comparator:
  it autogenerates removals, contradicting append-only — the two doors would
  disagree about what the same model means.
- **Ownership markers** (`COMMENT ON TYPE` stamped at create time): sound
  provenance, but every existing deployment's types are unmarked, and the
  backfill machinery buys nothing additive-only doesn't already guarantee.

## Consequences

- Under plain `auto_migrate=True`, an evolved `StrEnum` still fails at first
  use with the database's `invalid input value for enum` error — silently at
  boot, by design. The documented answer to drift, enum drift included, is
  `migrate_updates=True`. Auto-migrate docs must carry this trap loudly:
  fresh schemas always get the complete label set, so no app-side test
  against a throwaway schema can catch it (#328).
- A new table created under plain `auto_migrate` that references an existing
  stale type gets the stale label set — the create-pass type guard only
  fires for missing types. Same flag fixes it.
- Appended labels land at the end of the Postgres enum ordering regardless of
  their position in the Python declaration; `ORDER BY` on an enum column
  follows database order, not declaration order.
- Live introspection must read `pg_enum` labels (today it records only
  "is an enum"), and the cross-emitter parity tests extend to pin the label
  diff across both consumers.
