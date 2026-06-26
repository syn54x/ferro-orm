---
title: IR-first lowering consolidation audit
date: 2026-06-26
category: architecture-patterns
module: ir-first
problem_type: architecture_pattern
component: schema_migration
severity: high
status: active
applies_when:
  - "Deciding whether the IR-first program is paying off before Phase 9 shim removal"
  - "A phase gate claims 'one source of truth' but multiple code paths derive schema semantics independently"
  - "Adding a schema feature and discovering it must be edited in several type-lowering encoders"
  - "Reasoning about why cross-emitter parity is currently test-enforced rather than structural"
resolution_type: roadmap_correction
tags:
  - ir-first
  - schema-ir
  - ddl-lowering
  - cross-emitter-parity
  - single-source-of-truth
  - migration
related_components:
  - documentation
  - testing_framework
---

# IR-first lowering consolidation audit

## Context

This is a **code-grounded audit** of the IR-first program as it stands on
branch `feat/ir-p8-120-parity-gate` (Phase 8 parity gate landed; runtime
`auto_migrate` now runs the IR planner as primary with the legacy planner
shadow-compared). It was produced by reading the IR stack end-to-end —
`ferro-schema-ir`, `ferro-ddl-lowering`, `ferro-migrate`, the runtime
create/migrate paths in `src/schema.rs` and `src/migrate.rs`, the Python
`src/ferro/ir/compiler.py` and `src/ferro/migrations/alembic.py`, the query
plumbing, and the parity tests — **without relying on prose docs**, then
reconciling against the [IR-first roadmap](../../plans/2026-06-19-001-ir-first-roadmap.md)
and the [merge-readiness review](./ir-first-merge-readiness-review.md).

It complements the merge-readiness review (point-in-time defect list) by
auditing a deeper, structural question the phase gates have not yet closed:
**is the IR actually the single source of truth it is meant to be?** Today, for
the schema/DDL domain, it is not.

## Verdict

The IR-first direction is **correct in principle and correctly motivated** for a
Python ORM with a Rust core that has many consumers of the same schema knowledge
(runtime CREATE, runtime ALTER, Alembic autogenerate, query planning, codec). A
versioned, serializable IR is the right contract to decouple *what the schema/query
is* from *how each backend renders it*. The **Query IR** slice proves the idea
works end-to-end.

But the program is currently at the most expensive point of the transition, and
the **schema/DDL slice has not realized the "single source of truth" property**.
The refactor has, at this moment, *increased* the number of places that encode
the type system rather than reducing them. The payoff is real **iff the
consolidation is finished**; if the current dual-path / multi-encoder state
ossifies, it is net-negative versus the legacy path it replaced.

This finding is not "IR-first is wrong." It is: **the roadmap's own
program-level success criterion ("Runtime DDL, Alembic adapter, and migration
planner consume the same IR artifacts") and decision principle ("One source of
truth: no parallel emitters that independently derive schema semantics") are not
yet met for the schema domain, and no existing phase explicitly closes that
gap.** Phase 9 removes the legacy *planner*; it does not, as written, unify the
type-lowering encoders or collapse the dual SchemaIR producers.

## Findings (with evidence)

### 1. The type system is encoded in ~5 parallel places

The whole value of an IR + a shared lowering crate is *one* place that maps a
Ferro type to a `db_type`/SQL spelling. There are currently at least five:

1. `src/schema.rs:139` — its own `CanonicalType` enum + `canonical_column_type` +
   `canonical_to_db_type_token` (`:163`). This is the **runtime CREATE path**.
2. `crates/ferro-ddl-lowering/src/lib.rs:20` — a *second* `CanonicalType` enum +
   token mapping. This is the **runtime ALTER (migrate) path**.
3. `src/migrate.rs` legacy — verbatim re-implementations of helpers that already
   live in `ferro-ddl-lowering`: `pg_alter_type_target` (`:312`),
   `sqlite_declared_type` (`:336`), `sqlite_type_class` (`:373`),
   `single_unique_index_name` (`:405`), `fk_action_sql` (`:413`),
   `literal_default_value` (`:425`).
4. Python `src/ferro/ir/compiler.py:102` — `_default_db_type` / `_logical_type`.
5. Python `src/ferro/migrations/alembic.py:438` — `_db_type_to_sa_type`; the
   comment at `:435` literally says *"Duplicated on the Rust side."*

`src/schema.rs` imports nothing from `ferro-ddl-lowering` (verified). The crate
that was created to be "shared across Ferro emitters" is used by only one of the
two Rust emitters.

### 2. SchemaIR has two independent producers (split brain)

- Python `compile_schema_ir_payload` (`src/ferro/ir/compiler.py:253`) builds a
  SchemaIR, persists it to `ferro.state`, and fingerprints it. Its only consumer
  is the **Alembic** bridge (`get_metadata()`).
- The **runtime auto-migrate** path does **not** consume that artifact. It
  rebuilds its own SchemaIR in Rust from the enriched JSON schema via
  `schema_json_to_schema_ir` (`src/migrate.rs:116`) and
  `live_columns_to_schema_ir` (`:214`).

So "the canonical SchemaIR" is compiled twice, by two different
implementations, and kept in agreement only by behavioral tests — the opposite
of a single source of truth. Contrast the Query path, where Python is the sole
IR producer and Rust consumes it.

### 3. The runtime CREATE emitter consumes no IR at all

The invariant "an auto-migrated database matches a freshly created one"
(AGENTS.md I-1) is currently a **tested coincidence, not a structural
guarantee**. CREATE (`src/schema.rs`) and ALTER (`ferro-migrate` via
`ferro-ddl-lowering`) use two different `CanonicalType` systems; they agree only
because `tests/test_cross_emitter_parity.py` and
`tests/test_db_type_cross_emitter_parity.py` assert it, and those tests must be
*manually* extended for every new feature (the suite says so in its own
docstring). Shared code would make whole categories of drift impossible instead
of merely detectable.

### 4. The IR's `db_type` field is not authoritative

`ir/compiler.py:102` sets a bare integer's `db_type` to `"bigint"`, but the only
consumer (`alembic.py:_sa_type_from_ir_column`, `:196`) ignores `db_type` for
non-explicit columns and re-derives from `logical_type` → `sa.Integer()`.
Meanwhile the Rust create path maps integer → `int`. The IR therefore carries a
field value that disagrees with what every emitter actually does. It is masked
today (Alembic ignores it), but it means the IR is something emitters *re-derive
around* rather than obey.

### 5. The runtime migrate IR under-populates what the IR can express

`schema_json_to_schema_ir` hardcodes `indexes: []` and `uniques: []`
(`src/migrate.rs:206-207`) and only emits single-column checks, even though
`SchemaIrPayload` models composite indexes/uniques/checks and the Python
compiler fills them in. There is a capability gap between what the contract can
express and what the runtime consumer populates — composite-constraint changes
are not planned by the runtime migrate path.

### 6. Codec IR (the wire contract) is unwired

`CodecIrPayload` / `CodecBindRule` / `CodecFetchRule` / `HydrationAbi` exist as
types plus one conformance fixture
(`tests/fixtures/ir_vectors/codec_registry_core_v1.json`) with **zero runtime
consumers** on either side. (This is distinct from the Phase 5 runtime codec
*registry* in `src/codec.rs`, which is genuinely unified — the *behavior* is
centralized; the *IR envelope* for codec is fixture-only.) Fine as a roadmap
stub, but it widens the versioned contract surface for no current behavior.

### 7. The parity gate is a maintenance tax, justified only if short-lived

Keeping `plan_table_migration` (`src/migrate.rs:451`) and
`plan_table_migration_legacy` (`:510`) byte-identical across `statements`,
`drop_columns`, and `warnings` (`shadow_compare_migration_plan`, `:576`) means
every change lands in two planners and must be proven equal. This is exactly the
"best-effort / transitional scaffolding" that AGENTS.md I-6 ("No stop-gap
solutions") warns against. It is a sound *cutover* instrument; it is a liability
as a steady state.

## What is genuinely working (keep these)

- **Query IR is real and clean.** `src/ferro/query/builder.py` emits a versioned
  envelope (`ir_kind:"query"`); `src/operations.rs:209` deserializes
  `IrEnvelope<QueryIrPayload>`; `src/query.rs` lowers it. An honest Python→Rust
  contract — the proof the architecture works.
- **Versioned envelopes** (`ir_kind`/`ir_version`) + **fingerprints** + **golden
  vectors** (`tests/fixtures/ir_vectors/`) give real wire-stability and
  cross-language testability.
- **`ferro-ddl-lowering` is a good unit** — pure functions, minimal deps
  (serde + sea-query), canonical tokens + naming helpers + drift detection,
  well unit-tested. It is the right home for "the rules" — it just needs to be
  *the* home.
- **Cutover discipline is good** — shadow comparison, env-gated strict mode
  (`FERRO_SHADOW_RUNTIME_STRICT`), IR-native unit tests in `ferro-migrate`, and
  a behavioral parity test that bootstraps a real DB via Rust and asserts an
  empty Alembic autogenerate diff.

## Remediation plan (the "collapse")

In priority order. These are the deliverables proposed as **Phase 8.5** in the
roadmap (gating Phase 9 shim removal).

1. **Make `ferro-ddl-lowering` the one lowering library; have `src/schema.rs`
   consume it.** Delete schema.rs's private `CanonicalType` and route the CREATE
   path through the shared crate. This is the keystone — it turns I-1 from a
   tested coincidence into a structural guarantee and finally justifies the
   crate's existence. (Removes encoder #1's overlap with #2.)
2. **Produce SchemaIR in exactly one place.** Prefer the Query model: Python
   compiles the canonical SchemaIR once (it already does), passes it over FFI,
   and Rust consumes it for *both* migrate and create — instead of Rust
   re-deriving via `schema_json_to_schema_ir`. (Collapses producers in finding #2.)
3. **Delete the duplicated helpers in `src/migrate.rs`** (finding #1, item 3)
   once the legacy planner is gone, and put a hard removal date on the legacy
   planner + parity gate. A parity gate is a cutover tool, not an architecture.
4. **Make `db_type` authoritative or drop it for non-explicit columns**
   (finding #4). Carrying a value no emitter honors invites the drift the IR
   exists to prevent.
5. **Populate composite indexes/uniques in the runtime migrate IR, or document
   the deliberate scope cut** (finding #5).
6. **Defer Codec IR wiring until it has a consumer** (finding #6) — do not carry
   a versioned contract domain with no runtime behavior.

## What is missing (capabilities the IR could unlock but doesn't yet)

- **Shared lowering in the CREATE path** — the single highest-value gap.
- **A single SchemaIR producer** — currently two.
- **A structural (not test-only) emitter-agreement guarantee.**
- **A migration artifact / history.** This is a live-diff auto-migrator
  (introspect → diff → execute immediately, statement by statement; a mid-run
  failure leaves partial DDL, with only a pool refresh + identity-map clear at
  the end). The IR is perfectly positioned to enable a serialized, replayable,
  transactional `MigrationPlan` artifact — the obvious capability it unlocks and
  is not yet cashing in.
- **Native-enum reconciliation** is deferred to Alembic (`postgres_native_enum`
  short-circuit) — a known gap.

## Why this matters

- A second roadmap or a parallel "source of truth" would reproduce the exact
  failure mode this audit criticizes. The fix is **fewer** encoders, not more
  documents — and the same logic applies to docs: this audit amends the existing
  roadmap rather than competing with it.
- "Worth it" is conditional. Stopping at the current state leaves five type
  encoders kept in sync by manually-maintained tests — more fragile than the
  single legacy path it replaced. Finishing the collapse delivers the
  drift-proof architecture the program promised.

## When to apply

- **Before Phase 9 shim removal** — do not delete the legacy planner while the IR
  path still has a split-brain producer and the CREATE path still diverges.
- **When adding any schema feature** — if the change must be made in more than one
  type-lowering encoder, that is finding #1 biting; route through
  `ferro-ddl-lowering` instead.
- **When a phase gate claims "unify X across paths"** — grep for parallel
  derivations and verify the agreement is structural, not just asserted by tests.

## Related

- [`2026-06-19-001-ir-first-roadmap.md`](../../plans/2026-06-19-001-ir-first-roadmap.md) — amended with **Phase 8.5** for this work
- [`ir-first-merge-readiness-review.md`](./ir-first-merge-readiness-review.md) — prior point-in-time review (this audit is the structural follow-up)
- [`ir-invariants.md`](../patterns/ir-invariants.md) — Invariant I (cross-emitter parity) this audit shows is test-enforced, not yet structural
- [`cross-emitter-ddl-parity.md`](../patterns/cross-emitter-ddl-parity.md) — the parity recipes that currently substitute for shared code
- [`docs/pages/concepts/architecture.md`](../../pages/concepts/architecture.md) — two-pipeline model; carries a status note pointing here
