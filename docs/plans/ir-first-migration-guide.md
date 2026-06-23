# IR-first migration guide (living document)

This guide is updated continuously during the IR-first roadmap execution.

## Purpose

- Capture user-facing migration impact as work lands.
- Provide upgrade guidance before each phase closes.
- Ensure breaking changes are documented with mitigation paths.

## How to use this guide

- Add entries as part of issue completion for roadmap work.
- Group entries by phase.
- For each entry, include:
  - linked issue
  - change summary
  - impact level (`none`, `minor`, `breaking`)
  - required user action
  - compatibility window/deprecation timeline (if applicable)

## Phase entries

### Phase 0

No user-facing runtime behavior changes expected.

| Issue | Change | Impact | User action | Notes |
| --- | --- | --- | --- | --- |
| [#72](https://github.com/syn54x/ferro-orm/issues/72) | IR contract RFC definition | none | none | design-only; artifact: `docs/rfc/ir-contracts-v1.md` |
| [#73](https://github.com/syn54x/ferro-orm/issues/73) | Invariant specification | none | none | design-only; artifact: `docs/solutions/patterns/ir-invariants.md` |
| [#74](https://github.com/syn54x/ferro-orm/issues/74) | Golden vectors + CI harness skeleton | none | none | infra-only; artifacts: `tests/fixtures/ir_vectors/`, `tests/test_ir_vectors_contract.py` |

### Phase 1

No user-facing runtime behavior changes expected.

| Issue | Change | Impact | User action | Notes |
| --- | --- | --- | --- | --- |
| [#77](https://github.com/syn54x/ferro-orm/issues/77) | Add `ferro-schema-ir` crate with versioned serde IR contracts | none | none | Internal contract crate only; artifacts: `crates/ferro-schema-ir/`, RFC vector round-trip tests |
| [#78](https://github.com/syn54x/ferro-orm/issues/78) | Add deterministic Python -> SchemaIR compiler path | none | none | Internal compiler path only; artifacts: `src/ferro/ir/compiler.py`, model registration + relationship-resolution hooks |
| [#79](https://github.com/syn54x/ferro-orm/issues/79) | Persist model-set fingerprints and stable representative snapshots | none | none | Infra/test only; artifacts: `tests/fixtures/ir_vectors/schema_phase1_fixture_models_v1.json`, `tests/test_ir_vectors_contract.py` |

### Phase 2

No user-facing runtime behavior changes expected. Shadow planning is internal-only and defaults off.

| Issue | Change | Impact | User action | Notes |
| --- | --- | --- | --- | --- |
| [#81](https://github.com/syn54x/ferro-orm/issues/81) | Internal shadow planner flag and runtime dual-run compare hooks for query/DDL planning | none | none | Internal env-controlled verification path (`FERRO_SHADOW_RUNTIME` / `FERRO_SHADOW_RUNTIME_STRICT`) for CI and maintainers; no public API behavior cutover |
| [#82](https://github.com/syn54x/ferro-orm/issues/82) | Semantic diff harness for query planning semantics and bind semantics | none | none | Test-only helper `_shadow_compare_query_plan_for_test` + backend-matrix strict checks |
| [#83](https://github.com/syn54x/ferro-orm/issues/83) | Stable SQLite/Postgres shadow reports + touched-path CI enforcement | none | none | Golden shadow reports in `tests/fixtures/shadow_reports/` and path-gated CI workflow job |

### Phase 3

| Issue | Change | Impact | User action | Notes |
| --- | --- | --- | --- | --- |
| [#85](https://github.com/syn54x/ferro-orm/issues/85) | Runtime query compilation now consumes QueryIR envelopes on core execution paths | minor | No API change for lambda/`col()` query callers; if you rely on internal `_core` query payload shape, migrate to QueryIR envelope (`ir_kind`, `ir_version`, `payload`) | Internal JSON `QueryDef` payload contract is no longer the core hot-path boundary |
| [#86](https://github.com/syn54x/ferro-orm/issues/86) | Operator-style predicates (`Model.field OP value`) are deprecated with runtime warnings | minor | Migrate call sites to `where(lambda t: ...)` (recommended) or `col(Model.field)` | Deprecation message includes replacement + removal target (`v0.14.0`) |
| [#87](https://github.com/syn54x/ferro-orm/issues/87) | Python query builder now emits QueryIR envelope payloads to Rust runtime | minor | No action for public `Model.where`/`Query.where` usage; update internal tests/tools that serialized legacy `where_clause` JSON | Compatibility behavior remains documented in query typing docs during deprecation window |

Phase 3 test-migration note:

- Tests that exist only to verify temporary operator-style compatibility are tagged `deprecated_operator_path` and scheduled for removal/rewrite at `v0.14.0`.

### Phase 4

| Issue | Change | Impact | User action | Notes |
| --- | --- | --- | --- | --- |
| [#89](https://github.com/syn54x/ferro-orm/issues/89) | SchemaIR compiler fidelity extended for `db_check` expressions, enum metadata, and join-table model inclusion | minor | No API changes for model declaration; if you consume internal SchemaIR fixtures, refresh snapshots to include join-table and enum/check artifacts | Artifacts: `src/ferro/ir/compiler.py`, `tests/test_ir_vectors_contract.py` |
| [#90](https://github.com/syn54x/ferro-orm/issues/90) | Introduce `crates/ferro-migrate` planner scaffold and SQL emission entrypoint (executable DDL deferred to Phase 8) | minor | No user API change | Phase 4 landed scaffold only; completion tracked in [#118](https://github.com/syn54x/ferro-orm/issues/118)–[#120](https://github.com/syn54x/ferro-orm/issues/120) |
| [#91](https://github.com/syn54x/ferro-orm/issues/91) | Alembic `get_metadata()` now derives from SchemaIR modelset; legacy JSON-lowering helpers deprecated | minor | Keep using `get_metadata()`; if you directly import private `ferro.migrations.alembic._build_sa_table` / `_map_to_sa_type`, migrate away now | Deprecated helpers emit `DeprecationWarning` with planned removal `v0.14.0` |

Phase 4 deprecation note:

- Deprecated: `ferro.migrations.alembic._build_sa_table()` and `ferro.migrations.alembic._map_to_sa_type()`
- Replacement: `get_metadata()` (SchemaIR-backed) and internal IR lowering via `_sa_type_from_ir_column()`
- Removal target: `v0.14.0`

### Phase 5

| Issue | Change | Impact | User action | Notes |
| --- | --- | --- | --- | --- |
| [#93](https://github.com/syn54x/ferro-orm/issues/93) | Unified Rust codec registry now drives schema-aware bind/fetch lowering for insert/update/filter/m2m/fetch paths | minor | No API change; if you rely on internal Rust helper names (`schema_value_expr`, `value_rhs_simple_expr_for_backend`, `backend_column_value_expr`), migrate to codec registry entrypoints | Artifacts: `src/codec.rs`, `src/operations.rs`, `src/query.rs` |
| [#94](https://github.com/syn54x/ferro-orm/issues/94) | Hydration ABI is centralized into one helper that enforces `direct_dict` + required Pydantic slot initialization | minor | No API change; hydration behavior is now deterministic across `get/all/first` fetch paths | Artifacts: `src/hydration.rs`, `tests/test_hydration.py` |
| [#95](https://github.com/syn54x/ferro-orm/issues/95) | Codec conformance vectors and backend-matrix tests expanded for null/uuid/decimal/temporal/enum semantics | minor | No API change; expect stricter/clearer type behavior in edge cases previously relying on fallback coercions | Artifacts: `tests/fixtures/ir_vectors/codec_registry_core_v1.json`, `tests/test_ir_vectors_contract.py`, `tests/test_typed_null_binds.py` |

### Phase 6

| Issue | Change | Impact | User action | Notes |
| --- | --- | --- | --- | --- |
| [#97](https://github.com/syn54x/ferro-orm/issues/97) | Add explicit `Session`/`engines.session(name)` runtime boundary for ambient model/query/raw routing | minor | Prefer `async with ferro.engines.session(\"name\")` around request/task work; use `session=` explicit overrides when needed | Adds deterministic nested-session shadow/restore semantics |
| [#98](https://github.com/syn54x/ferro-orm/issues/98) | Core CRUD/query/transaction hot paths now support session-scoped transaction + identity-map state in Rust | minor | No call-site change required if you use sessions; behavior is now isolated per session under concurrent workloads | Legacy global fallback remains only for compatibility paths |
| [#99](https://github.com/syn54x/ferro-orm/issues/99) | Temporary compatibility shim keeps implicit default-connection routing but emits deprecation warnings | minor | Migrate unqualified operations (`Model.*`, `ferro.execute/fetch_*`) to run inside sessions; keep `using=` for explicit one-off routing | Deprecation warning points to removal target `v0.14.0` |

Phase 6 deprecation note:

- Deprecated: implicit default-connection routing outside an active session context.
- Replacement: `async with ferro.engines.session(\"name\")` (ambient routing) or explicit `session=` arguments.
- Removal target: `v0.14.0`.

### Phase 7

Public release phase for the IR-first architecture with a defined compatibility
window. Deprecated paths remain supported in `v0.12.x` and are removed in
`v0.14.0`.

| Issue | Change | Impact | User action | Notes |
| --- | --- | --- | --- | --- |
| [#100](https://github.com/syn54x/ferro-orm/issues/100) | Coordinate public IR-first release readiness and compatibility window evidence | minor | Follow the `v0.12.0` migration checklist before upgrading production workloads | Phase-level coordination/evidence issue |
| [#101](https://github.com/syn54x/ferro-orm/issues/101) | Ship public IR-first release while keeping compatibility paths during migration window | minor | Keep legacy call sites working short-term, but migrate off deprecated surfaces immediately | Deprecated paths stay live only through the compatibility window |
| [#102](https://github.com/syn54x/ferro-orm/issues/102) | Publish version-centric migration guide and upgrade checklist | minor | Follow [Migrating to v0.12.0](../pages/howto/migrating-to-v0-12-0.md) step-by-step | Includes IR-first rationale and concrete migration actions |
| [#103](https://github.com/syn54x/ferro-orm/issues/103) | Finalize release checklist/changelog and validate deprecation target consistency | minor | Validate that internal/private deprecated usage has been removed from your codebase | All Phase 7 deprecation warnings explicitly target `v0.14.0` removal |

Deprecated compatibility inventory for the Phase 7 window:

- Operator-style predicates (`Model.field OP value`) are deprecated.
  - Replacement: `where(lambda t: ...)` (official) or `col(Model.field)`.
  - Removal target: `v0.14.0`.
- Implicit default-connection routing outside an active session is deprecated.
  - Replacement: `async with ferro.engines.session("name")` or explicit `session=`.
  - Removal target: `v0.14.0`.
- Private Alembic JSON helper APIs are deprecated:
  - `ferro.migrations.alembic._build_sa_table`
  - `ferro.migrations.alembic._map_to_sa_type`
  - Replacement: `ferro.migrations.get_metadata()`.
  - Removal target: `v0.14.0`.

Phase 7 migration artifacts:

- User migration checklist: [Migrating to v0.12.0](../pages/howto/migrating-to-v0-12-0.md)
- Maintainer release checklist: [IR-first release checklist](ir-first-release-checklist.md)

### Phase 8

Runtime migration IR cutover (target: `v0.13.0`).

| Issue | Change | Impact | User action | Notes |
| --- | --- | --- | --- | --- |
| [#117](https://github.com/syn54x/ferro-orm/issues/117) | Coordinate `ferro-migrate` runtime cutover and parity exit gates | none | No action — internal planner cutover | Epic |
| [#118](https://github.com/syn54x/ferro-orm/issues/118) | Complete executable SQL emission from `MigrationPlan` for all ops (SQLite + Postgres) | none | No action | Continues #90 scaffold |
| [#119](https://github.com/syn54x/ferro-orm/issues/119) | Wire `auto_migrate` / `plan_table_migration` to execute ferro-migrate IR plans | none | No action — behavior must remain observably identical | Retires discarded `_typed_plan` path |
| [#120](https://github.com/syn54x/ferro-orm/issues/120) | Parity gate + remove legacy JSON diff path in `src/migrate.rs` | none | No action | AGENTS.md I-1 enforcement |

- **Migration impact:** `none` for public APIs — `connect(auto_migrate=...)` and `ferro.migrate` signatures unchanged.
- **Internal change:** `auto_migrate` executes `ferro-migrate` `SchemaIR(old,new)` plans instead of the legacy enriched-JSON diff walk in `src/migrate.rs`.
- **Parity requirement:** auto-migrated schema artifacts must remain byte-identical to `create_tables` and Alembic (AGENTS.md I-1).

### Phase 9

Compatibility cutover and shim removal (target: `v0.14.0`).

| Issue | Change | Impact | User action | Notes |
| --- | --- | --- | --- | --- |
| [#107](https://github.com/syn54x/ferro-orm/issues/107) | Coordinate hard removal of deprecated compatibility surfaces | breaking | Migrate off all deprecated paths before upgrading to `v0.14.0` | Epic |
| [#108](https://github.com/syn54x/ferro-orm/issues/108) | Remove deprecated runtime compatibility code paths | breaking | Use lambda/`col()` predicates and session-scoped routing | Operator style, ambient routing, Alembic JSON helpers |
| [#109](https://github.com/syn54x/ferro-orm/issues/109) | Remove deprecated compatibility test inventory | minor | No user action | `deprecated_operator_path` marker removal |
| [#110](https://github.com/syn54x/ferro-orm/issues/110) | Publish `v0.14.0` cutover migration and release notes | breaking | Follow final cutover guide at release | Changelog + checklist |

Planned cutover checklist:

- Remove deprecated operator-style predicate support.
- Remove ambient default-connection routing outside an active session.
- Remove private Alembic JSON helper APIs (`_build_sa_table`, `_map_to_sa_type`).
- Remove or rewrite all tests tagged `deprecated_operator_path`.
