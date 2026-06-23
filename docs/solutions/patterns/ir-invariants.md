---
title: IR-first invariants (parity, hydration ABI, null/bind correctness)
type: pattern
tags: [convention, invariant, ir, schema, query, codec, bridge, rust, python]
related_files:
  - AGENTS.md
  - docs/rfc/ir-contracts-v1.md
  - docs/solutions/patterns/cross-emitter-ddl-parity.md
  - docs/solutions/patterns/typed-null-binds.md
  - docs/solutions/architecture-patterns/ir-first-merge-readiness-review.md
  - src/ferro/ir/compiler.py
  - src/ferro/migrations/alembic.py
  - crates/ferro-migrate/src/lib.rs
  - src/schema.rs
  - src/query.rs
  - src/operations.rs
  - src/codec.rs
  - src/hydration.rs
  - src/backend.rs
  - tests/test_cross_emitter_parity.py
  - tests/test_db_type_cross_emitter_parity.py
  - tests/test_ir_vectors_contract.py
  - tests/test_typed_null_binds.py
  - tests/test_hydration.py
related_issues: [71, 72, 73, 74, 88, 89, 91, 93, 94, 100, 117, 118, 119, 120]
related_prs: []
captured: 2026-06-19
last_refreshed: 2026-06-23
---

## Problem

Ferro crosses Python and Rust boundaries through versioned IR envelopes — **SchemaIR**
at class-creation time and **QueryIR** per operation — plus enriched JSON schema
for the Rust registry. Phases 1–7 of the IR-first program are complete; runtime
`auto_migrate` still executes a legacy enriched-JSON diff until Phase 8
(`ferro-migrate` cutover, issues #117–#120).

Without one explicit invariant spec, drift can appear as phantom DDL diffs,
typed-null regressions, hydration attribute errors, or silent cache misses that
only show up at runtime.

## Takeaway

Treat these as non-negotiable IR invariants:

1. **Cross-emitter parity**: every schema artifact name/type/default/nullability must match across emitters.
2. **Hydration ABI**: zero-copy hydration must initialize required Pydantic slots exactly.
3. **Typed null/bind correctness**: schema-driven paths must emit type-correct binds, including typed NULLs.
4. **Session-scoped runtime state**: hot-path identity and transaction state is scoped to explicit/ambient sessions, not hidden globals.

Every IR contract change must preserve or explicitly version these invariants.

## Invariant I: Cross-emitter parity

### Contract

Given one model definition, all DDL emission paths must produce equivalent schema artifacts:

- Table names.
- Column names (including FK shadow `*_id` columns).
- Column type mapping/tokenization.
- Constraint/index/check naming.
- Nullability/default semantics.

### Why it exists

If emitters disagree, users get phantom autogenerate diffs and noisy migration history.

### Enforcement anchors

- `src/ferro/ir/compiler.py` — SchemaIR compilation and `ferro.state` modelset cache.
- `src/ferro/migrations/alembic.py` — `get_metadata()` derives from SchemaIR modelset.
- `crates/ferro-migrate/src/lib.rs` — `SchemaIR(old,new)` planner (executable DDL Phase 8).
- `src/schema.rs` — runtime DDL emitter behind `connect(auto_migrate=True)`.
- `tests/test_cross_emitter_parity.py`
- `tests/test_db_type_cross_emitter_parity.py`
- `tests/test_schema_constraints.py`
- `tests/test_alembic_autogenerate.py`
- `tests/test_ir_vectors_contract.py`

## Invariant II: Hydration ABI

### Contract

Rust hydration must keep direct-to-dict construction while being observationally equivalent to Pydantic initialization for required slots.

Minimum required initialized slots:

- `__pydantic_fields_set__`
- `__pydantic_extra__`
- `__pydantic_private__`

### Why it exists

Skipping slot initialization can pass basic reads but later fails with runtime `AttributeError` when user code or Pydantic internals touch missing attributes.

### Enforcement anchors

- `src/hydration.rs` (`hydrate_model_instance`)
- `src/backend.rs` row materialization flow
- `src/codec.rs` typed fetch decode used by hydration paths
- `tests/test_hydration.py`
- `docs/solutions/issues/pydantic-slots-missing-after-ferro-hydration.md`

## Invariant III: Typed null/bind correctness

### Contract

Schema-driven bind paths must preserve type identity for non-null values and NULL values.

- UUID values must remain UUID-typed on strict backends.
- Typed NULL must be selected from schema/type context where available.
- Raw SQL path may use untyped fallback only where schema context is unavailable.

### Why it exists

Untyped binds (especially NULL/UUID) can cause backend-specific mismatches or hidden coercion bugs.

### Enforcement anchors

- `src/codec.rs` (`schema_bind_expr`, `query_bind_expr`, `m2m_bind_expr`)
- `src/operations.rs` (`engine_bind_values_from_sea`)
- `src/backend.rs` (`EngineBindValue`, `NullKind`)
- `tests/test_typed_null_binds.py`

## Applying invariants to IR contracts

When updating `SchemaIR`, `QueryIR`, or `CodecIR`:

1. State which invariant(s) the change touches.
2. Add or update golden vectors that encode the invariant boundary.
3. Add a regression test that fails before the change and passes after.
4. Reject designs that only mitigate symptoms; fill the primitive gap.
5. Propagate failures across the FFI boundary via `PyResult` — never `unwrap()`
   (AGENTS.md I-3). Malformed IR must raise actionable Python exceptions.
6. When caching compiled IR in Python, assign through the canonical module
   (`ferro_state._SCHEMA_IR_MODELSET = ...`) — not `global` on imported names.

## How to recognize a violation

- Alembic autogenerate proposes drop/recreate with no real model change.
- Query/filter updates start failing only on one backend for NULL/UUID/decimal values.
- Hydrated instances error on `__pydantic_*` slot access.
- New IR field appears in one emitter path but not another.
- `ferro.state._SCHEMA_IR_MODELSET` is `None` after `compile_registry_schema_ir()`.
- Bad QueryIR or bind tuples panic in Rust instead of raising `PyValueError`.

If any appears, treat it as a correctness bug, not a warning-level mismatch.

## Invariant IV: Session-scoped runtime state

### Contract

Core CRUD/query/raw operation routing must derive mutable runtime state from explicit or ambient session context.

- Transaction state is session-scoped.
- Identity map state is session-scoped.
- Nested sessions shadow and restore deterministically.
- Legacy implicit default routing is compatibility-only and on a declared removal timeline.

### Why it exists

Global mutable runtime state in hot paths makes concurrent multi-DB behavior fragile and creates hidden coupling between unrelated tasks.

### Enforcement anchors

- `src/ferro/session.py`
- `src/ferro/state.py`
- `src/ferro/models.py`
- `src/ferro/query/builder.py`
- `src/ferro/raw.py`
- `src/state.rs`
- `src/operations.rs`
- `tests/test_session.py`

## Related

- [`ir-first-merge-readiness-review.md`](../architecture-patterns/ir-first-merge-readiness-review.md) — pre-merge checklist that caught invariant violations on `feat/ir-first`
- [`cross-emitter-ddl-parity.md`](cross-emitter-ddl-parity.md) — concrete naming recipes for Invariant I
- [`typed-null-binds.md`](typed-null-binds.md) — bind-path detail for Invariant III
- [`docs/plans/2026-06-19-001-ir-first-roadmap.md`](../../plans/2026-06-19-001-ir-first-roadmap.md) — phase status and exit gates
