---
title: Typed-null binds for primitives and UUID
type: bugfix-refactor
status: active
date: 2026-04-29
---

# Typed-null binds for primitives and UUID

## Overview

Replace Ferro's lossy "all-NULL-is-text" bind handling with **typed nulls at the
bind layer** for primitive types and UUID. This fixes
[#38](https://github.com/syn54x/ferro-orm/issues/38) (PostgreSQL rejects
`text`-typed NULL for `integer` / `bigint` / `bool` / `numeric` / `bytea`
columns) and converges Ferro's NULL handling onto a single rule for all
**schema-driven** NULL emission paths: type information about a value belongs
in the bind, not as `CAST(...)` wrappers in the SQL text.

Raw SQL bind paths are an explicit, documented exception (see Architectural
Direction §5).

---

## Problem Frame

Ferro's Rust core currently loses type information for NULL values at two
layers:

1. **SQL emission** (`src/operations.rs::schema_value_expr`) routes JSON `null`
   through a single catch-all to `sea_query::Value::String(None)`, which
   SeaQuery emits as a text-typed parameter on Postgres.
2. **Bind layer** (`src/backend.rs::bind_engine_value`) maps every
   `EngineBindValue::Null` to `Option::<String>::None`, producing a `text`
   parameter OID on the wire regardless of the column's declared type.

PostgreSQL has no implicit `text -> integer` cast, so any nullable integer
column rejects the INSERT:

> column "bench_level" is of type integer but expression is of type text

Existing UUID and temporal handling work around this with explicit
`cast_as("uuid")` / `cast_as("timestamptz")` branches at SQL emission time,
which papers over the bug at SQL-text level rather than fixing it at the bind
layer where type information natively belongs.

### Root cause

```rust
// src/operations.rs (schema_value_expr)
serde_json::Value::Null => Expr::value(sea_query::Value::String(None)),
```

```rust
// src/backend.rs (bind_engine_value)
EngineBindValue::Null => query.bind(Option::<String>::None),
```

SeaQuery already preserves type information in its typed `Value::Int(None)` /
`BigInt(None)` / `Bool(None)` / `Double(None)` / `Bytes(None)` variants. The
information is being thrown away by Ferro **between** SeaQuery and SQLx — not
by either of those libraries.

### Other emitters with the same shape

The same bug pattern exists in additional code paths beyond `schema_value_expr`
+ `bind_engine_value`. All of them are in scope for this refactor:

- `src/query.rs::json_to_sea_value` — query-filter NULL handling for `WHERE`
  predicates. Today routes JSON `null` to `String(None)`, so a
  `Thing.filter(count=None)` against a nullable integer column reproduces #38
  on the read side.
- `src/operations.rs::python_to_sea_value` — used by M2M operations
  (`add_m2m_links`, `remove_m2m_links`, `clear_m2m_links`) for target IDs.
  Today routes Python `None` to `String(None)`, so passing `None` as an M2M
  target id against a non-uuid integer FK reproduces #38.
- `src/query.rs::value_rhs_simple_expr_for_backend` — UPDATE / UPSERT
  expression emission. Currently emits explicit
  `cast_as("uuid"|"timestamptz"|"date"|"bytea"|"numeric")` for non-null
  values, which contradicts R3 once the bind layer carries the type.
- `src/operations.rs::backend_column_value_expr` — M2M target-id wrapping.
  Currently wraps M2M IDs in `cast_as("uuid")` for UUID FK columns; same
  contradiction.

---

## Requirements Trace

- **R1.** Primitive nullable columns must accept Python `None` on PostgreSQL
  for `integer`, `bigint`, `double precision`, `numeric`, `boolean`, and
  `bytea` columns. `Decimal | None` and `str | None` are included.
- **R2.** UUID nullable columns must continue to work without relying on
  `cast_as("uuid")` SQL fragments. Non-null UUID binds must also flow
  through the typed-bind layer.
- **R3.** Architectural rule: for **schema-driven** NULL emission paths
  (INSERT column values, UPDATE column values, query-filter predicates,
  M2M target IDs), type information for a bound value travels in the
  **bind**, not as `CAST(...)` wrappers in the SQL. After this change,
  `schema_value_expr`, `value_rhs_simple_expr_for_backend`,
  `python_to_sea_value`, and `backend_column_value_expr` should not need a
  `cast_as` branch for any in-scope type. Raw-SQL bind paths are exempt
  (see R6 / Architectural Direction §5).
- **R4.** SQLite behavior is unchanged. SQLite is permissive and the existing
  string-NULL bind is sufficient there.
- **R5.** Coverage is enforced by `backend_matrix` integration tests that
  exercise every in-scope type with both `None` and a real value across
  SQLite and PostgreSQL, including INSERT, UPDATE, query-filter, and M2M
  paths.
- **R6.** The raw-SQL bind path (`python_to_engine_bind_value` consumed by
  `raw_execute` / `raw_fetch_all` / `raw_fetch_one`) keeps an untyped null
  bind variant. This path has no schema or column-type context and cannot
  pick a typed null. The boundary is documented in
  `docs/solutions/patterns/typed-null-binds.md`.

---

## Scope Boundaries

- **Types in scope:** `int | None`, `float | None`, `bool | None`, `bytes | None`,
  `Decimal | None`, `str | None`, `UUID | None`, and non-null `UUID` binds.
- **Emission paths in scope:**
  - `src/operations.rs::schema_value_expr` (INSERT column values)
  - `src/operations.rs::python_to_sea_value` (M2M target IDs)
  - `src/operations.rs::backend_column_value_expr` (M2M target-id wrapping)
  - `src/query.rs::json_to_sea_value` (query-filter NULL handling)
  - `src/query.rs::value_rhs_simple_expr_for_backend` (UPDATE / UPSERT
    expression emission)
- **Bind layer in scope:** `EngineBindValue` enum + `engine_bind_values_from_sea`
  + `bind_engine_value` chain in `src/backend.rs` and `src/operations.rs`.
- **Cargo dependencies:** `sqlx/uuid` and `sea-query/with-uuid` enabled;
  `chrono` / `time` deliberately not added (deferred to #40).

### Explicit non-goals

- **Temporal types** (`datetime`, `date`, `time`, `timestamptz`). Routing
  through typed binds requires a `chrono` or `time` dependency that Ferro
  has deliberately avoided. Tracked as
  [#40](https://github.com/syn54x/ferro-orm/issues/40).
- **Raw-SQL bind path** (`python_to_engine_bind_value` and its callers). The
  raw-SQL boundary has no schema or column-type context, so the typed-null
  rule cannot apply there. Stays as untyped null bind. Documented as an
  explicit exception. See Architectural Direction §5 and R6.
- **JSON / JSONB null binding.** Postgres-side `cast_as("json")` already
  handles this, has no equivalent dependency cost, and the `serde_json::Json`
  type does not have an obvious `Option<T>` shape. Out of scope unless it
  falls out of the refactor naturally.
- Type-aware NULL for SQLite-specific exotic types.
- `using()` / multi-database connection routing.
- Public Python API changes.

### Deferred to Follow-Up Work

- Temporal typed-null binds: [#40](https://github.com/syn54x/ferro-orm/issues/40)
  (separate issue; depends on chrono-vs-time decision).
- Native `numeric` parameter binding (`Decimal | None` currently binds as
  `float8`-typed null and relies on Postgres' implicit assignment cast — see
  Risks). Requires `sqlx/rust_decimal` + `sea-query/with-rust_decimal` and a
  decision about Decimal precision. Open as a separate issue if the wire-OID
  change creates user-visible churn.

---

## Context & Research

### Relevant Code and Patterns

- **Bind layer:** `src/backend.rs::EngineBindValue` and `bind_engine_value`.
  Today's untyped `Null` variant is the focal point of the change.
- **From-SeaQuery adapter:** `src/operations.rs::engine_bind_values_from_sea`.
  Existing function that maps `SeaValue::*` to `EngineBindValue::*` with a
  catch-all `_ => EngineBindValue::Null` arm.
- **Schema-driven SQL emitters** (all in scope):
  `src/operations.rs::schema_value_expr`,
  `src/query.rs::value_rhs_simple_expr_for_backend`,
  `src/query.rs::json_to_sea_value`,
  `src/operations.rs::python_to_sea_value`,
  `src/operations.rs::backend_column_value_expr`.
- **Raw-SQL adapter (boundary):** `src/operations.rs::python_to_engine_bind_value`
  and its `cfg(test)` companion `raw_sql_tests::extracts_none_as_null`.
- **Test fixtures:** `tests/conftest.py` (`backend_matrix` marker registration,
  `--db-backends` CLI option), `tests/db_backends.py` (Postgres URL discovery,
  backend selection).
- **Related test files to mirror:** `tests/test_temporal_types.py` (similar
  shape — type-specific column behavior across backends), `tests/test_crud.py`
  (INSERT round-trip patterns), `tests/test_query_builder.py` (filter
  patterns), `tests/test_bulk_update.py` (UPDATE patterns),
  `tests/test_relationship_engine.py` (M2M patterns), `tests/test_raw_sql.py`
  (raw-SQL boundary tests).
- **Cross-emitter parity tests:** `tests/test_cross_emitter_parity.py`,
  `tests/test_alembic_autogenerate.py`. Don't touch DDL paths but should
  remain green as a regression gate (AGENTS.md I-1).

### Institutional Learnings

- **`docs/solutions/patterns/cross-emitter-ddl-parity.md`** — the closest
  precedent for a "rule that spans multiple Ferro emitters." Mirror its
  framing, related-files frontmatter, and Problem/Takeaway/Recipe shape when
  authoring `typed-null-binds.md`.
- **`docs/solutions/patterns/foreign-key-index.md`,
  `shadow-fk-columns.md`, `index-unique-redundancy.md`** — consistent doc
  shape under `docs/solutions/patterns/`. Cross-reference where helpful.
- **AGENTS.md I-1 (DDL parity)** — this refactor doesn't touch DDL emission,
  but the parity test suite is a regression gate and the new pattern doc
  should cite I-1 as a parallel-but-distinct invariant.
- **AGENTS.md I-3 (no `unwrap()` across FFI)** — UUID parse-failure handling
  must `map_err` into `PyValueError`, not `unwrap()` or panic.
- **AGENTS.md I-4 (tests live with the layer they exercise)** — Rust unit
  tests stay in `cfg(test)` modules adjacent to changed functions; Python
  integration tests live under `tests/`.
- **`.cursorrules` §3.B (FFI Efficiency: keep the bridge thin)** — the core
  motivation for typed-null binds. Pattern doc cites this.
- **`.cursorrules` §4 (TDD)** — every implementation unit below carries an
  Execution note for the test-first sequencing.

### External References

- **SQLx 0.8 docs**: `Encode<Postgres>` / `Encode<Sqlite>` /
  `Type<Postgres>` / `Type<Sqlite>` impls for primitive `Option<T>` and
  `Option<uuid::Uuid>` (gated by the `uuid` feature).
- **SeaQuery 0.32 docs**: `sea_query::Value` typed-`None` variants;
  `with-uuid` feature flag for `Value::Uuid`.
- **PostgreSQL parameter OID assignment-cast rules**: relevant for the
  `Decimal | None` → `float8` OID note (see Risks).

---

## Key Technical Decisions

- **Tag-form `NullKind` enum over expanded `*Null` variants**: keeps
  `EngineBindValue` size bounded, makes the `NullKind::Untyped` raw-SQL
  fallback fall out naturally, and pattern-matches symmetrically with the
  non-null variants.
- **Enable `sqlx/uuid` and `sea-query/with-uuid` Cargo features**: routes
  UUID through the same SeaQuery typed-value path as primitives, keeps
  `engine_bind_values_from_sea` consistent, and avoids a parallel side
  channel for UUID. Cost: small dep tree growth (`sqlx-postgres/uuid`,
  `sqlx-sqlite/uuid` codecs). Acceptable trade.
- **Raw-SQL boundary stays untyped**: `python_to_engine_bind_value` lacks the
  schema context to pick a typed null. Acknowledge this with
  `NullKind::Untyped` rather than papering over it. Document explicitly. A
  future `Param.null(IntType)` raw-SQL surface can be added later if
  user demand emerges.
- **UUID parse failure raises `PyValueError`**: not `panic!`, not silent
  fallback to `String + cast_as`. Error message names model + column +
  offending value to match or improve on Postgres' diagnostic.
- **Reject `None` M2M target ids with `TypeError` at the Python API layer**:
  preferred over threading column-type context through `python_to_sea_value`.
  M2M target ids should never be `None`; surfacing the error early is
  clearer than typed-null binding it.
- **Decimal stays on the `float8` OID path** for this PR. A native `numeric`
  bind requires `sqlx/rust_decimal` + a precision policy and is deferred.
- **No new AGENTS.md invariant.** "Type info travels in the bind" is a
  coding convention captured in `docs/solutions/patterns/`, not a product-
  visible invariant. Revisit if a future emitter (e.g., a `Ferro.to_sql()`
  CLI) makes it product-visible.

---

## Architectural Direction

The refactor is bounded to one enum and four function chains plus a documented
raw-SQL exception.

1. **Replace `EngineBindValue::Null`** with `Null(NullKind)` (tag form).
   `NullKind` carries variants for the in-scope types: `I64`, `Bool`, `F64`,
   `String`, `Bytes`, `Uuid`, plus `Untyped` for the raw-SQL exception.

2. **Extend `engine_bind_values_from_sea`** in `src/operations.rs` so each
   `SeaValue::T(None)` maps to its matching `Null(NullKind::T)` instead of
   collapsing to an untyped null. SeaQuery preserves typed `None` for the
   in-scope variants.

3. **Extend `bind_engine_value`** in `src/backend.rs` to bind each
   `Null(NullKind::T)` as `Option::<T>::None` (`Option::<i64>::None`,
   `Option::<bool>::None`, `Option::<f64>::None`, `Option::<Vec<u8>>::None`,
   `Option::<String>::None`, `Option::<uuid::Uuid>::None`). SQLx encodes
   these with the correct PostgreSQL parameter OIDs.

4. **Simplify the schema-driven SQL emitters** so the JSON `null` arm picks
   a SeaQuery typed `None` based on the column's JSON type / format, and
   non-null UUID strings parse to `uuid::Uuid` in Rust before binding rather
   than going through `String + cast_as`:
   - `schema_value_expr` (INSERT)
   - `value_rhs_simple_expr_for_backend` (UPDATE / UPSERT)
   - `json_to_sea_value` (query filters)
   - `python_to_sea_value` and `backend_column_value_expr` (M2M target IDs)

   Type mapping:
   - JSON `"integer"` → `Value::BigInt(None)`
   - JSON `"number"` → `Value::Double(None)` (Decimal lands here today; see
     Risks for the `float8` OID note)
   - JSON `"boolean"` → `Value::Bool(None)`
   - JSON `"string"` with `format: "byte"` → `Value::Bytes(None)`
   - JSON `"string"` (default) → `Value::String(None)`
   - UUID column (per `uuid_columns` set) → `Value::Uuid(None)`

   **UUID parse-failure error handling:** `uuid::Uuid::parse_str(s)` failure
   raises a `PyValueError` naming model + column + offending value.

   After the change, the only remaining `cast_as` branches in these emitters
   are the temporal ones (#40) and JSON / JSONB ones (out of scope).

5. **Raw-SQL bind path is an explicit exception.**
   `src/operations.rs::python_to_engine_bind_value` keeps emitting an
   untyped null via `EngineBindValue::Null(NullKind::Untyped)`. Boundary is
   documented in the new `EngineBindValue` doc-comment, in
   `docs/solutions/patterns/typed-null-binds.md`, and in the function-level
   doc-comment for `python_to_engine_bind_value`.

---

## Dependencies

The following Cargo feature flags are added to `Cargo.toml`:

- `sqlx` — add `"uuid"` to the feature list.
- `sea-query` — add `"with-uuid"` to the feature list.

`chrono` and `time` are explicitly **not** added — temporal typed-null binds
defer to [#40](https://github.com/syn54x/ferro-orm/issues/40).

---

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for
> review, not implementation specification. The implementing agent should
> treat it as context, not code to reproduce.*

```text
        Python None on a nullable column            Python None on raw SQL
                    │                                       │
                    ▼                                       ▼
    Schema-driven emitter chooses                 python_to_engine_bind_value
    SeaValue::T(None) by JSON type                returns Null(NullKind::Untyped)
                    │                                       │
                    ▼                                       │
    engine_bind_values_from_sea maps              ─────────┘
    SeaValue::T(None) → Null(NullKind::T)         (rejoin at bind step)
                    │
                    ▼
        bind_engine_value pattern-matches
        Null(NullKind::T) → query.bind(Option::<T>::None)
                    │
                    ▼
            SQLx wire OID for column's type
              (e.g., int4 NULL, bool NULL, uuid NULL)
```

Decision matrix for the `serde_json::Value::Null` arm in schema-driven
emitters:

| Column shape | JSON type / format | SeaValue typed-None |
|---|---|---|
| int, bigint, smallint | `"integer"` | `BigInt(None)` |
| float, double | `"number"` | `Double(None)` |
| Decimal (today) | `"number"` (with anyOf pattern) | `Double(None)` (note: `float8` OID) |
| bool | `"boolean"` | `Bool(None)` |
| bytes / bytea | `"string"`, `format: "byte"` | `Bytes(None)` |
| str (default) | `"string"` | `String(None)` |
| UUID | `"string"`, `format: "uuid"` (or column in `uuid_columns`) | `Uuid(None)` |
| temporal (out of scope) | `"string"`, `format: "date-time"` etc. | unchanged: `cast_as` branch retained |
| JSON / JSONB (out of scope) | `"object"` / `"array"` | unchanged: `cast_as("json")` retained |

---

## Implementation Units

- [x] U1. **Cargo features for typed UUID binds**

**Goal:** Enable `sqlx/uuid` and `sea-query/with-uuid` so typed UUID binds
compile and SeaQuery's `Value::Uuid` variant is available.

**Requirements:** R2, R6 (preconditions for everything else)

**Dependencies:** None

**Files:**
- Modify: `Cargo.toml`

**Approach:**
- Add `"uuid"` to the `sqlx` feature list.
- Add `"with-uuid"` to the `sea-query` feature list.
- `cargo check` should still pass without further changes (existing UUID
  paths use `String` today; the features just become available).

**Patterns to follow:**
- Existing feature additions to `sqlx` (e.g., `tls-rustls-ring-webpki`).

**Test scenarios:**
- Test expectation: none -- pure dependency change. Verified by U2-U8
  compiling.

**Verification:**
- `cargo check` succeeds.
- `cargo build --release` succeeds.

---

- [x] U2. **`EngineBindValue::Null(NullKind)` shape**

**Goal:** Replace the untyped `EngineBindValue::Null` variant with
`Null(NullKind)` carrying a tag for the bind type. Define the `NullKind`
enum with `I64`, `Bool`, `F64`, `String`, `Bytes`, `Uuid`, and `Untyped`
variants. Document the raw-SQL boundary in the variant doc-comment.

**Requirements:** R3, R6

**Dependencies:** U1 (so `NullKind::Uuid` can compile against
`uuid::Uuid`-aware Encode impls in U3)

**Files:**
- Modify: `src/backend.rs` (enum + derives + doc comments)
- Test: `src/backend.rs` (`#[cfg(test)] mod tests`)

**Approach:**
- Derive `Debug`, `Clone`, `PartialEq` on `NullKind`.
- The `Null(NullKind::Untyped)` variant is the explicit fallback for the
  raw-SQL path and any unmapped SeaQuery `Value` variant in U3.
- Doc-comment on `EngineBindValue::Null` names the schema-driven vs raw-SQL
  boundary and links to the new pattern doc (U10).
- This unit does **not** yet rewire any callers — `bind_engine_value` and
  call sites compile against the old `Null` variant by adding a deprecated
  `pub fn null_legacy()` constructor that returns `Null(NullKind::Untyped)`,
  or by doing the rewire in the same commit. Implementer's choice.

**Execution note:** Test-first per `.cursorrules` §4 — write Rust unit
tests asserting variant construction, equality, and `Debug` output before
changing the enum definition.

**Patterns to follow:**
- Existing `EngineBindValue` derives and doc-comments.

**Test scenarios:**
- Happy path: each `NullKind` variant constructs and compares equal to
  itself (`NullKind::I64 == NullKind::I64`).
- Edge case: `NullKind::Untyped` is distinct from typed variants
  (`NullKind::Untyped != NullKind::I64`).
- Happy path: `Debug` output for `Null(NullKind::I64)` is stable and
  contains both the variant name and the kind.

**Verification:**
- `cargo test --lib` passes the new unit tests.
- `cargo check` clean across the crate.

---

- [x] U3. **Bind layer: `engine_bind_values_from_sea` + `bind_engine_value`**

**Goal:** Map each SeaQuery typed `None` variant to its matching `NullKind`
and bind each `NullKind` as `Option::<T>::None` to SQLx. Add an explicit
`NullKind::Untyped` fallback for unmapped SeaQuery variants and a Rust unit
test that locks the fallback in.

**Requirements:** R1, R2, R3, R6

**Dependencies:** U1, U2

**Files:**
- Modify: `src/operations.rs::engine_bind_values_from_sea`
- Modify: `src/backend.rs::bind_engine_value`
- Test: `src/operations.rs` (`#[cfg(test)] mod engine_bind_tests` or similar)
- Test: `src/backend.rs` (existing or new `#[cfg(test)]` module)

**Approach:**
- `engine_bind_values_from_sea` adds match arms for `SeaValue::Int(None)`,
  `BigInt(None)`, `Bool(None)`, `Double(None)`, `Bytes(None)`,
  `String(None)`, `Uuid(None)` mapping to the matching `NullKind`. Catch-all
  `_ => EngineBindValue::Null(NullKind::Untyped)` is preserved as the
  documented fallback.
- `bind_engine_value` pattern-matches each `NullKind` variant and binds
  the corresponding `Option::<T>::None`.
- Verify SeaQuery preserves typed `None` through `.build(...)` via a small
  Rust test before relying on the assumption.

**Execution note:** Test-first — assert SeaQuery typed `None` round-trips
through the build step before wiring `engine_bind_values_from_sea`.

**Patterns to follow:**
- Existing match arms in `engine_bind_values_from_sea` (one arm per
  `SeaValue` variant).
- Existing pattern-match arms in `bind_engine_value`.

**Test scenarios:**
- Happy path: every in-scope `SeaValue::T(None)` round-trips to
  `Null(NullKind::T)` through `engine_bind_values_from_sea`.
- Edge case: a deliberately-unmapped SeaQuery `Value` variant (e.g., one
  not in scope) maps to `Null(NullKind::Untyped)` (locks the fallback).
- Happy path: `bind_engine_value` produces `Option::<T>::None` for each
  variant. Compile-time enforcement comes from SQLx's `Encode<DB>` trait
  bounds; runtime check confirms we don't regress to
  `Option::<String>::None` for typed-null variants.
- Integration: SeaQuery `.build_postgres()` of a query containing
  `Value::Int(None)` produces a parameter list whose first entry round-trips
  through `engine_bind_values_from_sea` to `Null(NullKind::I64)`.

**Verification:**
- `cargo test --lib` passes new unit tests.
- No call site outside the schema-driven emitters constructs an untyped
  null directly.

---

- [x] U4. **Raw-SQL boundary preservation**

**Goal:** Update `python_to_engine_bind_value` to emit
`Null(NullKind::Untyped)` for Python `None`, and update its companion test
`extracts_none_as_null` to assert the new variant. Add a function-level
doc-comment describing the boundary.

**Requirements:** R6

**Dependencies:** U2

**Files:**
- Modify: `src/operations.rs::python_to_engine_bind_value`
- Modify: `src/operations.rs::raw_sql_tests::extracts_none_as_null`

**Approach:**
- Single-line variant constructor change: `EngineBindValue::Null` →
  `EngineBindValue::Null(NullKind::Untyped)`.
- Doc-comment on `python_to_engine_bind_value` says: "Raw-SQL bind path
  has no schema or column-type context; emits an untyped null. Schema-
  driven emitters use typed nulls. See
  `docs/solutions/patterns/typed-null-binds.md`."

**Execution note:** Update the existing `extracts_none_as_null` test
assertion before changing the function so it fails first.

**Patterns to follow:**
- Existing `cfg(test)` test shape in `src/operations.rs::raw_sql_tests`.

**Test scenarios:**
- Happy path: Python `None` → `EngineBindValue::Null(NullKind::Untyped)`.
- Edge case: Python `None` for a non-textual implied type (no schema
  context exists; the boundary is the function's purpose).

**Verification:**
- `cargo test --lib raw_sql_tests::extracts_none_as_null` passes.

---

- [x] U5. **INSERT emitter (`schema_value_expr`) + UUID parse-failure handling**

**Goal:** Replace the JSON-`null` catch-all in `schema_value_expr` with a
typed-null pick based on column JSON type / format. Retire the UUID
`cast_as("uuid")` non-null branch in favor of `uuid::Uuid::parse_str(...)`
+ `Value::Uuid(Some(...))`. Add UUID parse-failure error handling raising
`PyValueError` with model + column + offending value.

**Requirements:** R1, R2, R3

**Dependencies:** U1, U2, U3

**Files:**
- Modify: `src/operations.rs::schema_value_expr`
- Test: extend existing `#[cfg(test)]` blocks in `src/operations.rs`
- Test: `tests/test_typed_null_binds.py` (new — see U9)

**Approach:**
- JSON `null` arm switches on column metadata (JSON type, format,
  `uuid_columns` membership) and emits the matching SeaQuery typed `None`.
- Non-null UUID column path: parse string to `uuid::Uuid`; on failure
  raise `PyValueError` via `?` + `map_err`. Per AGENTS.md I-3 — no
  `unwrap()`.
- Error message format: `"Invalid UUID for {model}.{column}: {offending_value}"`.
  Keep stable; tested for stability in U9.

**Execution note:** Test-first — write a failing Python integration test
mirroring #38 (`bench_level: int | None = None` on Postgres INSERT) in
U9 first, then implement until it passes.

**Patterns to follow:**
- Existing JSON-type discrimination logic in `schema_value_expr`.
- `PyResult` / `map_err` pattern from sibling functions in
  `src/operations.rs`.

**Test scenarios:**
- Happy path: each in-scope nullable type (int, bigint, float, bool, bytes,
  Decimal, str, UUID) accepts `None` on Postgres INSERT.
- Happy path: each in-scope nullable type accepts a real value on Postgres
  INSERT (round-trip via `get()`).
- Edge case: invalid UUID string → `PyValueError` with stable diagnostic
  message naming model + column + offending value.
- Integration: `Covers #38 regression.` `bench_level: int | None = None`
  succeeds on Postgres.

**Verification:**
- `tests/test_typed_null_binds.py` test for INSERT path passes on
  `--db-backends=sqlite,postgres`.
- `schema_value_expr` contains no `cast_as("uuid")` branch after the change.

---

- [x] U6. **UPDATE / UPSERT emitter (`value_rhs_simple_expr_for_backend`)**

**Goal:** Same shape as U5 for UPDATE and UPSERT paths. Remove the
`cast_as("uuid"|"bytea"|"numeric")` branches; rely on typed binds.

**Requirements:** R1, R2, R3

**Dependencies:** U1, U2, U3

**Files:**
- Modify: `src/query.rs::value_rhs_simple_expr_for_backend`
- Test: existing `tests/test_bulk_update.py`, `tests/test_query_builder.py`
- Test: `tests/test_typed_null_binds.py` (new — see U9)

**Approach:**
- Mirror U5's logic: typed null based on JSON type for `null` values;
  parse-then-bind for non-null UUID.
- Existing UPDATE-path snapshots in `tests/test_query_builder.py` may need
  relaxation for UUID columns where the `CAST($1 AS uuid)` wrapper goes
  away.

**Execution note:** Test-first — failing UPDATE integration test first.

**Patterns to follow:**
- The U5 implementation; same shape.

**Test scenarios:**
- Happy path: `Thing.update(...)` setting nullable column to `None` and to
  non-null on Postgres.
- Happy path: bulk UPDATE same shape.
- Integration: `Covers #38 regression.` UPDATE branch.
- Edge case: existing SQL snapshot tests for UUID UPDATE relaxed where
  the cast wrapper is removed.

**Verification:**
- `tests/test_typed_null_binds.py` UPDATE-path tests pass.
- `tests/test_bulk_update.py`, `tests/test_query_builder.py` pass with
  any required snapshot updates.
- `value_rhs_simple_expr_for_backend` contains no `cast_as` for in-scope
  types.

---

- [x] U7. **Query-filter emitter (`json_to_sea_value`)**

**Goal:** Update query-filter NULL emission to use typed null based on
column JSON type. Confirm `WHERE col = ?` with `?=None` binds typed null,
not text.

**Requirements:** R1, R3, R5

**Dependencies:** U1, U2, U3

**Files:**
- Modify: `src/query.rs::json_to_sea_value`
- Test: `tests/test_query_builder.py`
- Test: `tests/test_typed_null_binds.py` (new — see U9)

**Approach:**
- Same JSON-type → typed-null mapping as U5.
- This unit closes the read-side half of #38 (filter NULLs).

**Execution note:** Test-first — failing
`Thing.filter(bench_level=None)` integration test on Postgres first.

**Patterns to follow:**
- The U5 / U6 implementations.

**Test scenarios:**
- Happy path: `Thing.filter(count=None)` for `count: int | None` returns
  the right rows on Postgres without raising.
- Happy path: comparison with non-null nullable values still works.
- Integration: `Covers #38 regression.` filter branch.
- Edge case: `IS NULL` semantics — verify `filter(count=None)` produces
  `WHERE count IS NULL`, not `WHERE count = NULL` (this is existing
  behavior; just confirm we didn't regress it).

**Verification:**
- `tests/test_typed_null_binds.py` filter-path tests pass.
- `tests/test_query_builder.py` passes with snapshot updates if needed.

---

- [x] U8. **M2M target-id emitters (`python_to_sea_value`, `backend_column_value_expr`)**

**Goal:** Update M2M target-id paths to use typed binds. Reject `None` M2M
target IDs at the Python API layer with `TypeError`. Remove
`cast_as("uuid")` for UUID FK columns from `backend_column_value_expr`.

**Requirements:** R2, R3

**Dependencies:** U1, U2, U3

**Files:**
- Modify: `src/operations.rs::python_to_sea_value`
- Modify: `src/operations.rs::backend_column_value_expr`
- Modify: `src/operations.rs` (M2M API entry: `add_m2m_links` /
  `remove_m2m_links` — wherever `None` ids should be rejected)
- Test: `tests/test_relationship_engine.py`, `tests/test_one_to_one.py`
- Test: `tests/test_typed_null_binds.py` (new — see U9)

**Approach:**
- `python_to_sea_value` returns typed `SeaValue::Int(...)` /
  `SeaValue::Uuid(...)` based on the M2M FK column type.
- `backend_column_value_expr` removes the UUID-specific `cast_as("uuid")`
  branch; relies on typed binding.
- M2M API entry validates target ids: `None` raises a `TypeError` with a
  clear message ("M2M target id cannot be None for {relation}").

**Execution note:** Test-first — failing
`m2m_field.add(target_id=None)` test asserting `TypeError`.

**Patterns to follow:**
- Existing M2M code in `src/operations.rs`.
- `TypeError` raises via `PyErr::new::<PyTypeError, _>(...)`.

**Test scenarios:**
- Happy path: M2M `add` / `remove` / `clear` with valid integer FK ids on
  Postgres.
- Happy path: M2M `add` / `remove` / `clear` with valid UUID FK ids on
  Postgres.
- Edge case: `m2m.add(None)` raises `TypeError` with a clear message.
- Integration: existing M2M tests in `tests/test_relationship_engine.py`
  remain green.

**Verification:**
- `tests/test_typed_null_binds.py` M2M-path tests pass.
- `tests/test_relationship_engine.py`, `tests/test_one_to_one.py` pass.
- `backend_column_value_expr` contains no `cast_as` for in-scope types.

---

- [x] U9. **Python integration matrix (`tests/test_typed_null_binds.py`)**

**Goal:** New test file covering Type matrix × Path matrix per R5. Covers
INSERT, UPDATE, query-filter, M2M for every in-scope type, on both SQLite
and PostgreSQL via the `backend_matrix` marker. Includes #38 regression
and UUID parse-failure tests.

**Requirements:** R1, R2, R5

**Dependencies:** U5, U6, U7, U8

**Files:**
- Create: `tests/test_typed_null_binds.py`

**Approach:**
- One model `TypedNullThing` with non-PK fields covering every in-scope
  type: `int_v: int | None`, `float_v: float | None`, `bool_v: bool | None`,
  `bytes_v: bytes | None`, `decimal_v: Decimal | None`, `str_v: str | None`,
  `uuid_v: UUID | None`.
- Parametrized tests across path × value (None / non-None) / backend.
- Specific tests:
  - `Covers AE: #38 regression on INSERT` —
    `bench_level: int | None = None`.
  - `Covers AE: #38 regression on UPDATE`.
  - `Covers AE: #38 regression on filter`.
  - `Covers AE: UUID parse failure raises PyValueError with stable
    diagnostic`.
  - `Covers AE: M2M None target id raises TypeError`.
- Mark file-level with `pytest.mark.backend_matrix` per
  `tests/conftest.py` convention.

**Execution note:** This test file is built incrementally as U5-U8 land —
each unit's per-path test scenarios live here. By U8 completion the file is
the single integration surface for the refactor.

**Patterns to follow:**
- `tests/test_temporal_types.py` (similar shape: type-specific column
  behavior across backends).
- `tests/conftest.py` (`backend_matrix` marker, `--db-backends` selector).

**Test scenarios:**
- Happy path: 7 types × 4 paths × 2 None/non-None × 2 backends = full
  matrix.
- Edge case: invalid UUID string surfaces stable diagnostic.
- Edge case: M2M `None` target id surfaces `TypeError`.
- Integration: #38 regression covers INSERT, UPDATE, and filter forms.

**Verification:**
- `uv run pytest tests/test_typed_null_binds.py --db-backends=sqlite,postgres`
  passes with no skipped backend cases.
- The file is included in the existing CI matrix (`.github/workflows/ci.yml`)
  without changes to the workflow — the `backend_matrix` marker auto-
  parametrizes.

---

- [x] U10. **Documentation deliverables**

**Goal:** Ship the user-facing and institutional-memory documentation in
the same PR.

**Requirements:** AGENTS.md I-5 (institutional memory), product-lens
review feedback (user-facing release notes).

**Dependencies:** U1-U9 (doc reflects the implemented behavior)

**Files:**
- Create: `docs/solutions/patterns/typed-null-binds.md`
- Modify: `CHANGELOG.md` and `docs/changelog.md`
- Modify: `README.md` (or `docs/guide/backend.md` — whichever names
  supported nullable types)

**Approach:**
- `typed-null-binds.md` mirrors the shape of
  `docs/solutions/patterns/cross-emitter-ddl-parity.md`: frontmatter
  with `type: pattern`, `tags`, `related_files`, `related_issues`,
  `related_prs`, `captured` fields. Sections: Problem, Takeaway, Recipe,
  Boundaries (raw-SQL exception), Related patterns
  (`cross-emitter-ddl-parity.md`).
- `CHANGELOG.md` entry under the next-version heading: lists the now-
  supported nullable types on Postgres (int, bigint, float, bool, bytes,
  Decimal, UUID), names the raw-SQL boundary, links to #38 and the
  pattern doc.
- README / backend guide: add a paragraph naming nullable type support
  with a one-sentence note about the raw-SQL boundary.

**Patterns to follow:**
- `docs/solutions/patterns/cross-emitter-ddl-parity.md` (frontmatter
  shape, section ordering, prose voice).

**Test scenarios:**
- Test expectation: none -- pure documentation.

**Verification:**
- Lint / linkcheck runs cleanly if such tooling exists.
- The pattern doc is searchable via `rg 'typed-null-binds'` from the
  repo root.

---

## System-Wide Impact

- **Interaction graph:** the bind layer change touches every code path that
  passes a `None` through to SQLx, regardless of which Ferro API the user
  called. Schema-driven paths (CRUD, filter, bulk, M2M) all funnel through
  the same enum.
- **Error propagation:** UUID parse-failure is now surfaced at the Python
  call site as `PyValueError` rather than at SQL execution time as a
  Postgres-side error. Improves diagnostic quality for malformed input;
  changes the failure-time location.
- **State lifecycle risks:** none — this refactor doesn't change row-level
  state, only the bind shape.
- **API surface parity:** Python public API is unchanged. Rust `EngineBindValue`
  shape changes (private to the cdylib).
- **Integration coverage:** U9's `backend_matrix` test file is the single
  cross-layer integration surface. Pre-existing tests
  (`tests/test_temporal_types.py`, `tests/test_crud.py`,
  `tests/test_query_builder.py`, `tests/test_relationship_engine.py`,
  `tests/test_bulk_update.py`) act as regression coverage for paths the
  refactor touches transitively.
- **Unchanged invariants:** AGENTS.md I-1 (DDL parity) — this refactor does
  not touch DDL emission. AGENTS.md I-2 (zero-copy hydration) — unchanged.
  AGENTS.md I-3 (no `unwrap()` across FFI) — explicitly honored in U5's
  UUID parse-failure handling. The pattern doc in U10 sits alongside I-1's
  pattern doc as a parallel-but-distinct invariant.

---

## Risks & Dependencies

| Risk | Mitigation |
|---|---|
| `Decimal | None` binds as `float8`-typed NULL, not `numeric`; wire OID change can churn pgbouncer prepared-statement caches. | Document in `docs/solutions/patterns/typed-null-binds.md`. Native `numeric` bind deferred (see Deferred follow-ups). |
| Test surface larger than ~6 tests; existing `extracts_none_as_null` and possibly other `cfg(test)` blocks reference `EngineBindValue::Null`. | U2 / U4 explicitly update `extracts_none_as_null`. Run `rg 'EngineBindValue::Null' src/ tests/` before each unit to catch hard references. |
| Subtle SQL diffs in tests that snapshot generated SQL (UUID `CAST` wrapper goes away). | U6 / U7 / U8 explicitly call out snapshot relaxation. |
| `Decimal` round-tripping precision pre-existing concerns surface during testing. | Out of scope; preserve current decimal behavior. Note any drift in U9 test commentary. |
| Removing untyped `EngineBindValue::Null` is a Rust ABI change; today Ferro ships only as a Python cdylib so no external consumer. | Acknowledged in Risks; revisit if Ferro opens an extension/plugin path. |
| SeaQuery 0.32's `Postgres`/`Sqlite` builders may not preserve typed `Value::T(None)` through `.build(...)`. | U3 starts with a Rust unit test that asserts the round-trip; if the assumption fails, the unit blocks early with a clear failure rather than producing a half-correct refactor. |
| PR review surface is multi-day, not single-sitting. | Sequence units U1-U10 as separate commits in the PR; reviewers can check off one unit at a time. |

---

## Documentation / Operational Notes

- **CHANGELOG entry** is part of U10. Names supported nullable types,
  raw-SQL boundary, and #38 fix.
- **README / backend guide** mention is part of U10. One-paragraph addition
  in the relevant `docs/guide/backend.md` section.
- **Rollout:** standard 0.5.x patch release (per Open Question #3). No
  feature flag, no migration step. Users get the fix on upgrade.
- **Monitoring:** none specific. Watch for issues tagged with
  parameter-OID or pgbouncer-related symptoms after release.

---

## Open Questions

### Resolved During Planning

- **Enum shape**: tag form (`Null(NullKind)`) chosen over expanded
  `*Null` variants. See Key Technical Decisions.
- **UUID parse-failure UX**: `PyValueError` with stable message format.
  See U5.
- **M2M `None` target id handling**: reject with `TypeError` at API entry,
  not typed-null. See U8 and Key Technical Decisions.
- **`docs/solutions/patterns/` entry**: ships in this PR (U10).
- **AGENTS.md status of the rule**: stays at `docs/solutions/patterns/`
  level, not promoted to a top-level invariant.

### Deferred to Implementation

- **SeaQuery typed-`None` preservation through `.build(...)`**: assumed but
  not yet verified. U3's first task is a Rust unit test that round-trips
  `SeaValue::T(None)` through the build step. If the assumption fails, the
  refactor needs a different layering — surface as a blocker before
  proceeding to U5.
- **Exact `extracts_none_as_null` and other `EngineBindValue::Null`
  references in test code**: surveyed via `rg` at U2 / U4. Each reference
  may need a 1-line update; not knowable until the survey runs.
- **Snapshot test relaxation surface**: tests in `tests/test_query_builder.py`
  and possibly elsewhere snapshot generated SQL. Exact set discovered as
  U5-U8 land.

---

## Sources & References

- **Origin**: this document evolved through brainstorming and doc-review
  passes on 2026-04-29. Earlier requirements-shape content is preserved
  in the Problem Frame, Requirements Trace, Scope Boundaries, and
  Architectural Direction sections above.
- Bug: [#38](https://github.com/syn54x/ferro-orm/issues/38)
- Temporal follow-up (chrono vs time decision):
  [#40](https://github.com/syn54x/ferro-orm/issues/40)
- Related code:
  - `src/operations.rs::schema_value_expr`,
    `src/operations.rs::python_to_sea_value`,
    `src/operations.rs::backend_column_value_expr`,
    `src/operations.rs::python_to_engine_bind_value`
  - `src/operations.rs::engine_bind_values_from_sea`
  - `src/query.rs::json_to_sea_value`,
    `src/query.rs::value_rhs_simple_expr_for_backend`
  - `src/backend.rs::bind_engine_value`, `EngineBindValue`
  - `src/state.rs::SqlDialect`, `RustValue`
- Related patterns: `docs/solutions/patterns/cross-emitter-ddl-parity.md`
- Project invariants: `AGENTS.md` I-1 (cross-emitter DDL parity),
  I-3 (no `unwrap()` across FFI), I-4 (tests live with the layer they
  exercise), I-5 (`docs/solutions/` institutional memory).
- Project workflow: `.cursorrules` §3.B (FFI Efficiency: keep the bridge thin),
  §4 (TDD).
