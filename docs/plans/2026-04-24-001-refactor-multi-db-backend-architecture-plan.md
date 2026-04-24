---

## title: Refactor Ferro multi-db backend architecture

type: refactor
status: active
date: 2026-04-24

# Refactor Ferro multi-db backend architecture

## Overview

Refactor Ferro's SQLite/Postgres support so backend behavior is modeled explicitly instead of emerging from a global `sqlx::Any` pool plus scattered dialect branches. The goal is to make backend handling more correct, more maintainable, and easier to extend while preserving Ferro's existing Python-first API and current single-connection product boundary.

---

## Problem Frame

Ferro already supports both SQLite and Postgres in meaningful ways, but the implementation is accumulating backend-specific exceptions in multiple layers. Runtime SQL generation, type decoding, UUID handling, migrations metadata, join-table typing, and docs/test coverage are no longer aligned closely enough to support clean evolution.

Today, the same logical database behavior is described in at least three places:

- Rust runtime schema and execution code in `src/schema.rs`, `src/query.rs`, and `src/operations.rs`
- Python schema registration in `src/ferro/metaclass.py` and `src/ferro/relations/__init__.py`
- Alembic metadata generation in `src/ferro/migrations/alembic.py`

That split has created real debt:

- backend identity is tracked globally rather than owned by a backend abstraction
- Postgres support depends on `sqlx::Any` workarounds such as UUID text casting
- join-table schemas still assume integer FK columns
- docs overstate support and options that the runtime does not actually provide
- CI does not validate a live SQLite/Postgres behavior matrix

This plan is intentionally scoped to internal backend architecture and support accuracy. It does **not** add user-facing named multi-database APIs like `using("replica")`.

---

## Requirements Trace

- R1. Replace the current `sqlx::Any`-centered backend model with an explicit backend abstraction that treats SQLite and Postgres as first-class runtime targets.
- R2. Establish one canonical schema/metadata normalization path that Rust runtime code and Alembic both consume consistently.
- R3. Preserve Ferro's current public Python ergonomics (`Model`, `connect(url, auto_migrate=False)`, query builder, relationship APIs) while improving backend correctness under the hood.
- R4. Standardize backend-specific type handling for at least UUID, Decimal, JSON, Enum, temporal values, and relationship shadow FK types.
- R5. Remove or isolate backend-specific hacks so they live in well-defined backend adapters instead of spreading across query, schema, and operation code.
- R6. Add automated verification that exercises both SQLite and Postgres behavior, with backend-specific tests where portability is intentionally not exact.
- R7. Bring docs and support statements back in line with what Ferro actually implements.
- R8. Keep scope limited to the current single-connection-per-process model; do not introduce named multi-connection APIs in this refactor.

---

## Scope Boundaries

- No user-facing multi-database connection routing (`using()`, read replicas, named engines).
- No new third backend such as MySQL in this phase.
- No broad ORM feature expansion unrelated to backend architecture.
- No full migration away from SeaQuery; this plan assumes SeaQuery remains the SQL lowering layer.

### Deferred to Follow-Up Work

- Named multi-connection APIs and connection routing semantics after the backend abstraction has stabilized.
- Optional Postgres-only enhancements that are not required for SQLite/Postgres parity, such as richer JSONB query features.
- Broader performance benchmarking and public benchmark publication after correctness and architecture land.

---

## Context & Research

### Relevant Code and Patterns

- `src/state.rs` currently owns global engine, dialect, transaction, and identity-map state.
- `src/connection.rs` performs URL-prefix backend detection and creates a single `Pool<Any>`.
- `src/schema.rs` is the runtime DDL authority for `auto_migrate` and `create_tables()`.
- `src/query.rs` and `src/operations.rs` contain most backend-specific type and SQL workarounds.
- `src/ferro/metaclass.py` and `src/ferro/relations/__init__.py` build and register the schema that Rust consumes.
- `src/ferro/migrations/alembic.py` independently reconstructs SQLAlchemy metadata with richer nullability/type semantics than the Rust DDL path currently enforces.
- `tests/test_structural_types.py`, `tests/test_shadow_fk_types.py`, `tests/test_alembic_bridge.py`, and `tests/test_alembic_type_mapping.py` already cover much of the logical type surface and should anchor refactor safety.

### Institutional Learnings

- No `docs/solutions/` directory or prior institutional learnings were found in this repo.

### External References

- `sqlx` guidance favors typed backends for first-class database behavior; `Any` is best treated as a runtime-generic escape hatch rather than the center of a backend architecture.
- SeaQuery is a good SQL lowering layer, but not a sufficient semantic IR for ORM metadata and portability rules.
- `sqlx` and SeaQuery both expose meaningful SQLite/Postgres differences for Decimal, UUID, Enum, and transaction behavior; the plan should encode those decisions explicitly rather than infer them ad hoc.

---

## Key Technical Decisions

- **Backend architecture:** Introduce an explicit backend layer in Rust, most likely an enum-backed facade over typed SQLite/Postgres pools and executors, instead of continuing to centralize `Pool<Any>`.
- **Schema ownership:** Introduce a canonical normalized schema/metadata representation that both runtime DDL and Alembic derive from, even if the final lowering targets differ.
- **SQL construction:** Keep SeaQuery as the SQL builder/lowering target rather than hand-writing SQL everywhere.
- **Scope control:** Keep the public Python API stable during this refactor; the internal architecture changes first, and user-facing multi-db features remain deferred.
- **Backend support contract:** Treat SQLite and Postgres as the only supported runtime backends in this phase; remove or sharply qualify MySQL mentions in docs.
- **Testing posture:** Add a backend-matrix contract suite and make PostgreSQL runtime coverage real in CI rather than relying mainly on SQLite plus SQLAlchemy compile-time checks.

---

## Open Questions

### Resolved During Planning

- **Should this refactor add named multi-connection APIs?** No. The work is limited to internal backend architecture and correctness.
- **Should SeaQuery be replaced?** No. SeaQuery stays; the refactor introduces a cleaner semantic layer above it.
- **Should the public Python API change first?** No. Preserve the existing API shape and rework the backend boundary under it.
- **Should docs continue to mention MySQL as supported?** No. The support contract should be SQLite/Postgres only until runtime implementation actually changes.

### Deferred to Implementation

- **Exact backend facade shape:** enum dispatch vs trait objects can be finalized after the first backend module extraction, since that decision depends on how much generic friction appears in real code.
- **Portable enum storage policy:** whether runtime DDL keeps enums as portable text everywhere or adds optional Postgres-native enum support can be finalized after the shared schema IR exists.
- **Decimal storage representation on SQLite:** the implementation should confirm whether existing behavior should be preserved or normalized more explicitly as text-backed portability semantics.
- **How much of `sqlx::Any` remains:** implementation can keep a small bootstrap/admin usage if needed, but the plan assumes it is no longer the main query/runtime path.

---

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

```text
Python model / query layer
  -> normalized Ferro schema + query IR
  -> Rust backend facade
       -> SQLite adapter
            -> SeaQuery SqliteQueryBuilder
            -> SqlitePool / Sqlite transaction handling
            -> SQLite type encode/decode rules
       -> Postgres adapter
            -> SeaQuery PostgresQueryBuilder
            -> PgPool / Pg transaction handling
            -> Postgres type encode/decode rules
  -> hydrated Rust values
  -> Python object reconstruction
```

Key consequence: shared ORM semantics live above the backend adapters, while backend-specific SQL, bind/decode, and capability decisions live below them.

---

## Phased Delivery

### Phase 1

- Stabilize the backend abstraction and schema ownership model without changing public API behavior.

### Phase 2

- Move type handling, query lowering, transaction semantics, and DDL rules onto the new backend layer.

### Phase 3

- Land backend-matrix tests, CI coverage, and doc/support-contract cleanup.

---

## Implementation Units

- U1. **Introduce an explicit Rust backend facade**

**Goal:** Replace the current global dialect-plus-`Any` engine model with a backend facade that makes SQLite and Postgres explicit runtime choices.

**Requirements:** R1, R3, R5, R8

**Dependencies:** None

**Files:**

- Create: `src/backend.rs`
- Modify: `src/state.rs`
- Modify: `src/connection.rs`
- Modify: `src/lib.rs`
- Test: `tests/test_connection.py`

**Approach:**

- Extract backend identity out of ad hoc URL-prefix branching and into a persistent backend-owned runtime structure.
- Move engine state from "global `Pool<Any>` plus global `SqlDialect`" toward a backend facade that owns pool, executor behavior, and transaction entrypoints.
- Preserve the public `connect(url, auto_migrate=False)` shape while making backend classification and runtime dispatch explicit and testable.
- Keep `reset_engine()` and similar test-isolation hooks available, but align them with the new backend state model.

**Execution note:** Start with failing connection/runtime contract tests that assert backend identity and reset behavior before moving shared state.

**Patterns to follow:**

- `src/connection.rs` for the existing connection lifecycle contract
- `src/state.rs` for existing test-reset patterns that must remain available

**Test scenarios:**

- Happy path: connecting with a SQLite URL initializes the SQLite backend path and leaves basic model operations working.
- Happy path: connecting with a Postgres URL initializes the Postgres backend path without requiring SQLite-specific fallbacks.
- Edge case: repeated `connect()` / `reset_engine()` cycles do not leave stale backend state behind.
- Error path: unsupported or malformed URLs fail with a backend-classification error that is clearer than "default to SQLite".
- Integration: existing Python `connect()` still resolves relationships, initializes the backend, and keeps `auto_migrate` orchestration intact.

**Verification:**

- Connection tests clearly distinguish backend classification from general connection failures.
- No runtime code still depends on a process-global dialect flag as the primary backend switch.

---

- U2. **Create one canonical schema normalization layer**

**Goal:** Ensure the schema metadata used by Rust runtime DDL, query/type semantics, and Alembic all flow from the same normalized Ferro model description.

**Requirements:** R2, R3, R4, R5

**Dependencies:** U1

**Files:**

- Create: `src/ferro/schema_metadata.py`
- Modify: `src/ferro/metaclass.py`
- Modify: `src/ferro/relations/__init__.py`
- Modify: `src/ferro/migrations/alembic.py`
- Modify: `src/schema.rs`
- Test: `tests/test_alembic_bridge.py`
- Test: `tests/test_alembic_type_mapping.py`
- Test: `tests/test_alembic_nullability.py`

**Approach:**

- Centralize enrichment of Pydantic schema with Ferro-specific metadata so metaclass registration, relationship re-registration, and Alembic metadata generation stop rebuilding the same semantics independently.
- Make nullability, PK/FK metadata, composite uniques, and logical types explicit in the normalized schema layer.
- Update runtime DDL creation in `src/schema.rs` to consume the same explicit nullability/type metadata that Alembic already respects.
- Remove silent divergence where Alembic understands constraints or type intent that runtime `create_tables()` does not.

**Patterns to follow:**

- `src/ferro/metaclass.py` for class-definition-time schema registration
- `src/ferro/migrations/alembic.py` for the current richest mapping of logical schema intent

**Test scenarios:**

- Happy path: a model with PK, FK, unique, index, and nullable metadata produces equivalent semantics in runtime DDL and Alembic metadata.
- Happy path: UUID, JSON, Decimal, Enum, and temporal fields preserve their logical type identity in the normalized schema.
- Edge case: optional fields and optional FKs remain aligned between Python validation, runtime DDL, and Alembic.
- Error path: invalid composite-unique declarations or broken relationship metadata fail at schema normalization time rather than surfacing later during DDL execution.
- Integration: relationship resolution re-registers schemas without dropping metadata that Alembic and runtime DDL both need.

**Verification:**

- The repo has a single authoritative schema enrichment path, and Alembic/runtime metadata parity tests pass from that shared source.

---

- U3. **Normalize backend-specific type handling and join-table typing**

**Goal:** Move UUID/Decimal/JSON/Enum/temporal handling onto explicit backend rules and eliminate integer-only assumptions for generated relationship tables.

**Requirements:** R2, R4, R5

**Dependencies:** U2

**Files:**

- Modify: `src/query.rs`
- Modify: `src/operations.rs`
- Modify: `src/schema.rs`
- Modify: `src/ferro/_shadow_fk_types.py`
- Modify: `src/ferro/relations/__init__.py`
- Test: `tests/test_structural_types.py`
- Test: `tests/test_shadow_fk_types.py`
- Test: `tests/test_temporal_types.py`
- Test: `tests/test_query_builder.py`

**Approach:**

- Replace scattered type-specific runtime hacks with backend adapter rules for bind values, decoded row values, and DDL lowering.
- Keep the Python-facing type contract stable while deciding explicit physical storage rules per backend.
- Make generated many-to-many join-table FK columns derive from related model PK types instead of hard-coding integer.
- Revisit current Postgres UUID select/filter workarounds and re-home them into backend-specific bind/decode paths rather than shared operation code.

**Patterns to follow:**

- `src/ferro/_shadow_fk_types.py` for PK-type-aware shadow FK typing
- `tests/test_shadow_fk_types.py` for UUID FK expectations already treated as contract behavior

**Test scenarios:**

- Happy path: UUID PKs and FK shadow columns round-trip correctly on both SQLite and Postgres.
- Happy path: Decimal, JSON, Enum, bytes, and temporal fields save and hydrate with the same Python-level behavior across backends unless explicitly documented otherwise.
- Edge case: many-to-many relationships between UUID-keyed models generate join tables with correctly typed FK columns.
- Edge case: query filters involving UUID and Decimal values compile and execute correctly without backend-specific false positives from string inference.
- Error path: unsupported backend-specific type operations fail explicitly rather than degrading to wrong SQL or `NULL` binds.
- Integration: runtime DDL, query filtering, hydration, and relationship traversal all agree on the same logical type mapping.

**Verification:**

- Type round-trip tests cover shared and backend-specific behavior from one contract suite.
- Generated join tables no longer assume integer PKs.

---

- U4. **Refactor transactions and write semantics onto backend-owned execution**

**Goal:** Replace raw transaction string management and backend-fragile write/ID behavior with backend-owned transaction and insert semantics.

**Requirements:** R1, R4, R5

**Dependencies:** U1, U3

**Files:**

- Modify: `src/operations.rs`
- Modify: `src/connection.rs`
- Modify: `src/state.rs`
- Modify: `src/ferro/models.py`
- Test: `tests/test_transactions.py`
- Test: `tests/test_crud.py`
- Test: `tests/test_bulk_update.py`

**Approach:**

- Move transaction entrypoints onto backend-owned transaction objects or a backend facade that can model nested behavior safely.
- Remove generic raw `BEGIN` / `COMMIT` / `ROLLBACK` handling where the library can provide stronger semantics.
- Replace SQLite-specific last-insert-id assumptions with backend-specific insert/returning rules that make PK behavior explicit.
- Keep current Python transaction ergonomics stable while hardening the backend implementation.

**Patterns to follow:**

- `src/ferro/models.py` transaction context manager and save flow as the stable Python-facing contract
- `tests/test_transactions.py` for the current behavioral contract

**Test scenarios:**

- Happy path: the transaction context manager commits successful work on both backends.
- Happy path: failed work rolls back correctly and leaves no partial writes.
- Edge case: bulk operations and updates inside transactions preserve expected row visibility and cleanup behavior.
- Edge case: insert behavior returns or applies PK values correctly for autoincrement and non-autoincrement keys on both backends.
- Error path: transaction IDs or handles cannot outlive/reset into invalid backend state.
- Integration: `Model.save()`, `Model.create()`, and query updates/deletes remain consistent with transaction-scoped operations.

**Verification:**

- Transaction tests rely on backend-owned semantics rather than raw SQL string commands.
- Insert ID behavior is explicit and backend-correct instead of relying on SQLite fallbacks.

---

- U5. **Build a real SQLite/Postgres contract test matrix**

**Goal:** Make backend correctness observable through a shared contract suite plus backend-specific coverage in CI.

**Requirements:** R4, R6

**Dependencies:** U1, U2, U3, U4

**Files:**

- Create: `tests/db_backends.py`
- Modify: `tests/conftest.py`
- Modify: `tests/test_connection.py`
- Modify: `tests/test_structural_types.py`
- Modify: `tests/test_shadow_fk_types.py`
- Modify: `tests/test_transactions.py`
- Modify: `.github/workflows/ci.yml`
- Test: `tests/test_documentation_features.py`

**Approach:**

- Introduce a backend-matrix fixture strategy so the same runtime behavior can be exercised against SQLite and Postgres.
- Split tests into portable contract tests vs backend-specific expectations where semantics intentionally diverge.
- Add a live Postgres path in CI rather than relying on skipped local-only tests.
- Keep lightweight SQLite-only coverage where useful, but stop treating it as a sufficient proxy for Postgres behavior.

**Patterns to follow:**

- Existing runtime integration tests under `tests/`
- The dual-backend experimentation already visible in the existing `.worktrees/dual-db-test-matrix` branch as directional prior art only

**Test scenarios:**

- Happy path: the same contract suite passes on SQLite and Postgres for CRUD, relationships, structural types, and transactions.
- Edge case: backend-specific divergences are captured explicitly in marked tests rather than silently tolerated in shared tests.
- Error path: CI fails if Postgres runtime behavior regresses even when SQLite remains green.
- Integration: docs-example tests use the same backend fixture utilities where practical so examples do not drift away from supported runtime behavior.

**Verification:**

- CI provisions and runs a live Postgres test target.
- Shared contract tests make backend support claims concrete rather than aspirational.

---

- U6. **Align docs and support statements with the implemented contract**

**Goal:** Make Ferro's public docs accurately describe the supported backends, connection API, testing story, and deferred features after the refactor.

**Requirements:** R6, R7, R8

**Dependencies:** U5

**Files:**

- Modify: `README.md`
- Modify: `docs/guide/database.md`
- Modify: `docs/getting-started/installation.md`
- Modify: `docs/api/utilities.md`
- Modify: `docs/howto/testing.md`
- Modify: `docs/howto/multiple-databases.md`
- Modify: `docs/faq.md`

**Approach:**

- Remove or qualify inaccurate claims about MySQL, unsupported `connect()` options, and unavailable lifecycle functions.
- Clarify that this refactor improves SQLite/Postgres backend architecture but does not yet add named multi-database APIs.
- Update testing/docs examples to match the real connection lifecycle and backend support matrix.

**Patterns to follow:**

- `README.md` quick-start tone and current docs structure
- Existing warning pattern in `docs/howto/multiple-databases.md` for deferred features

**Test scenarios:**

- Happy path: installation and database setup docs describe only the backends and options that actually work.
- Edge case: docs for deferred features clearly distinguish "not implemented" from "supported with caveats".
- Integration: runtime examples and testing examples match current public API signatures and lifecycle hooks.

**Verification:**

- No doc page claims support for a backend or option that is absent from runtime code and CI coverage.

---

## System-Wide Impact

- **Interaction graph:** This work touches the Python model/schema layer, Rust execution layer, migration metadata bridge, and CI/docs support contract simultaneously.
- **Error propagation:** Backend classification, schema normalization, and type-lowering failures should move earlier in the stack so errors happen at connection/schema-build time rather than during deep query execution.
- **State lifecycle risks:** Engine reset behavior, transaction state ownership, and identity-map invalidation all become more sensitive during the backend abstraction change.
- **API surface parity:** Public Python model/query APIs must remain behaviorally stable even as backend internals change.
- **Integration coverage:** Unit tests alone will not prove this refactor; cross-layer backend-matrix tests and Alembic parity checks are required.
- **Unchanged invariants:** Ferro remains a single-connection runtime per process in this phase, keeps SeaQuery as the SQL builder, and keeps Pydantic-driven model definitions as the authoring surface.

---

## Risk Analysis & Mitigation


| Risk                                                                                | Likelihood | Impact | Mitigation                                                                                                 |
| ----------------------------------------------------------------------------------- | ---------- | ------ | ---------------------------------------------------------------------------------------------------------- |
| Backend refactor breaks existing SQLite behavior while improving Postgres structure | Medium     | High   | Land backend contract tests early and run them continuously during the refactor                            |
| Schema normalization work introduces drift between runtime DDL and Alembic metadata | Medium     | High   | Treat parity tests as blocking and make one normalization path authoritative                               |
| Transaction changes create subtle lifecycle regressions                             | Medium     | High   | Preserve Python transaction API, add backend-specific rollback/commit tests, and keep reset hooks explicit |
| Type portability decisions become too ambitious for one refactor                    | Medium     | Medium | Scope this phase to SQLite/Postgres correctness and explicit capability boundaries, not feature expansion  |
| Docs and support claims remain ahead of implementation                              | High       | Medium | Make docs cleanup a required final unit tied to CI-backed support claims                                   |


---

## Documentation / Operational Notes

- CI will need a Postgres service or equivalent runtime provisioning once the backend matrix lands.
- Release notes should call out that backend architecture changed internally while the public Python API remains stable.
- After this refactor lands, the repo should capture the resulting backend architecture decisions in a durable learning document.

---

## Sources & References

- Related code: `src/state.rs`, `src/connection.rs`, `src/schema.rs`, `src/query.rs`, `src/operations.rs`
- Related code: `src/ferro/metaclass.py`, `src/ferro/relations/__init__.py`, `src/ferro/migrations/alembic.py`, `src/ferro/models.py`
- Related tests: `tests/test_connection.py`, `tests/test_structural_types.py`, `tests/test_shadow_fk_types.py`, `tests/test_alembic_bridge.py`, `tests/test_alembic_type_mapping.py`, `tests/test_transactions.py`
- External docs: [sqlx Any](https://docs.rs/sqlx/latest/sqlx/any/index.html)
- External docs: [sqlx Transaction](https://docs.rs/sqlx/latest/sqlx/struct.Transaction.html)
- External docs: [SeaQuery backend docs](https://docs.rs/sea-query/latest/sea_query/backend/index.html)
- External docs: [PyO3 async guide](https://pyo3.rs/v0.27.2/async-await)
