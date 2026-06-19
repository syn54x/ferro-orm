---
title: IR-first invariants (parity, hydration ABI, null/bind correctness)
type: pattern
tags: [convention, invariant, ir, schema, query, codec, bridge, rust, python]
related_files:
  - AGENTS.md
  - docs/rfc/ir-contracts-v1.md
  - docs/solutions/patterns/cross-emitter-ddl-parity.md
  - docs/solutions/patterns/typed-null-binds.md
  - src/ferro/migrations/alembic.py
  - src/schema.rs
  - src/query.rs
  - src/operations.rs
  - src/backend.rs
  - tests/test_cross_emitter_parity.py
  - tests/test_db_type_cross_emitter_parity.py
  - tests/test_typed_null_binds.py
  - tests/test_hydration.py
related_issues: [71, 72, 73, 74]
related_prs: []
captured: 2026-06-19
---

## Problem

Ferro currently crosses Python and Rust boundaries using multiple JSON-shaped contracts and independently enforced conventions. Without one explicit invariant spec, drift can appear as phantom DDL diffs, typed-null regressions, or hydration attribute errors that only show up at runtime.

## Takeaway

Treat these as non-negotiable IR invariants:

1. **Cross-emitter parity**: every schema artifact name/type/default/nullability must match across emitters.
2. **Hydration ABI**: zero-copy hydration must initialize required Pydantic slots exactly.
3. **Typed null/bind correctness**: schema-driven paths must emit type-correct binds, including typed NULLs.

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

- `src/ferro/migrations/alembic.py` naming convention.
- `src/schema.rs` naming/type emission helpers.
- `tests/test_cross_emitter_parity.py`
- `tests/test_db_type_cross_emitter_parity.py`
- `tests/test_schema_constraints.py`
- `tests/test_alembic_autogenerate.py`

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

- `src/operations.rs` (`set_pydantic_hydration_slots`)
- `src/backend.rs` row materialization flow
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

- `src/query.rs` (`value_rhs_simple_expr_for_backend`, typed-null selection)
- `src/operations.rs` (`engine_bind_values_from_sea`)
- `src/backend.rs` (`EngineBindValue`, `NullKind`)
- `tests/test_typed_null_binds.py`

## Applying invariants to IR contracts

When updating `SchemaIR`, `QueryIR`, or `CodecIR`:

1. State which invariant(s) the change touches.
2. Add or update golden vectors that encode the invariant boundary.
3. Add a regression test that fails before the change and passes after.
4. Reject designs that only mitigate symptoms; fill the primitive gap.

## How to recognize a violation

- Alembic autogenerate proposes drop/recreate with no real model change.
- Query/filter updates start failing only on one backend for NULL/UUID values.
- Hydrated instances error on `__pydantic_*` slot access.
- New IR field appears in one emitter path but not another.

If any appears, treat it as a correctness bug, not a warning-level mismatch.
