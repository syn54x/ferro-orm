---
title: "IR-first Ferro roadmap"
type: strategy
status: active
date: 2026-06-19
origin: chat
---

# IR-first Ferro roadmap

## Goal

Move Ferro to an IR-first architecture where schema, query, migration, and codec behavior are driven from one canonical intermediate representation (IR), eliminating cross-emitter drift classes by construction.

## Decision principles

- No stop-gaps: if a phase reveals a capability gap, extend the primitive instead of adding local patches.
- One source of truth: no parallel emitters that independently derive schema semantics.
- Fail loud on correctness boundaries: no warning-and-continue for invariant violations.
- Backward compatibility is intentional and temporary: each shim has a planned removal phase.

## Scope

- In scope: schema IR, query IR, migration planning from IR, typed codec registry, hydration ABI, sessionized runtime state.
- Out of scope (for this roadmap): feature expansion unrelated to IR migration unless it unblocks a phase gate.

## Success criteria (program-level)

- Runtime DDL, Alembic/autogenerate adapter, and migration planner consume the same IR artifacts.
- Query execution consumes typed QueryIR (not ad-hoc JSON payloads).
- Hydration path is single and ABI-defined for Pydantic slot initialization.
- Global registries are removed from hot-path runtime operations in favor of explicit engine/session state.
- Legacy compatibility shims are removed in the explicit `v0.14.0` cutover.
- User-facing migration guidance is continuously updated and release-ready at each phase boundary.

## Living migration guide requirement

Maintain a running migration guide at:

- `docs/plans/ir-first-migration-guide.md`

Rules:

- Every phase issue must include a migration impact assessment (`none`, `minor`, `breaking`).
- If impact is `minor` or `breaking`, the migration guide must be updated before the issue can be marked Done.
- If impact is `none`, explicitly record why in the issue body.
- Phase exit gates are incomplete until migration-guide updates for that phase are merged (or explicitly marked `no user-impact` across all completed issues).

## Deprecation policy (required)

When new work supersedes existing APIs, behavior, patterns, or code paths:

- Mark the old surface as deprecated at introduction time (not later).
- Use Python's `warnings.deprecated` decorator for user-facing deprecated call sites.
- Include a clear message with:
  - what is deprecated
  - the replacement API/pattern
  - planned removal phase/version
- Add or update tests that assert deprecation warnings are emitted.
- Add a matching entry in `docs/plans/ir-first-migration-guide.md` with migration steps.

Deprecation lifecycle:

1. Introduce replacement + deprecation warning.
2. Keep compatibility through the declared window.
3. Remove deprecated surface in the planned phase/version and update migration guide.

## Documentation update policy (required)

Documentation is part of feature completion, not a follow-up task.

Rules:

- Every roadmap issue must include a documentation impact assessment (`none`, `update-existing`, `new-docs`).
- If assessment is `update-existing` or `new-docs`, documentation updates must land before the issue can be marked Done.
- If assessment is `none`, record explicit justification in the issue body.
- Required docs to evaluate on each issue:
  - user-facing guides under `docs/pages/guide/` and `docs/pages/howto/`
  - API references under `docs/pages/api/`
  - conceptual docs under `docs/pages/concepts/`
  - examples under `docs/examples/` when behavior is demonstrated there
  - migration notes in `docs/plans/ir-first-migration-guide.md` when user behavior changes

Definition of done addition:

- Feature work is not complete until code, tests, and docs are all synchronized for the shipped behavior.

## Program status

- Overall status: `In progress` (Phases 0–7 merged; Phase 8 ferro-migrate cutover landing; Phase 8.5 lowering consolidation pending; Phase 9 shim removal pending)
- Current phase: `Phase 8`
- Last updated: `2026-06-26`
- Roadmap owner: `@syn54x`

> **Architecture audit (2026-06-26):** a code-grounded audit
> ([`ir-first-lowering-consolidation-audit.md`](../solutions/architecture-patterns/ir-first-lowering-consolidation-audit.md))
> found that the program's success criterion *"Runtime DDL, Alembic adapter, and
> migration planner consume the same IR artifacts"* and the principle *"One source
> of truth: no parallel emitters"* are **not yet met for the schema domain**: the
> runtime CREATE path (`src/schema.rs`) keeps its own `CanonicalType` and consumes
> no IR; SchemaIR is compiled by two independent producers (Python
> `compile_schema_ir_payload` + Rust `schema_json_to_schema_ir`); and the type
> system is encoded in ~5 parallel places. Phase 9 removes the legacy *planner*
> but does not close this gap. **Phase 8.5 (below) is added to close it and gates
> Phase 9.**

## Branching and release policy

> **Policy correction (2026-06-26):** the original model below — *stage all work
> on `feat/ir-first`, promote to `main` only at program end* — was abandoned early
> in the program. In practice the IR work ships to `main` incrementally via
> `0.12.x` releases, and each phase integrates on its own short-lived branch.
> `feat/ir-first` is **retired** (diverged from `main`, no open PRs); do not branch
> from it or target it. The current (actual) flow is recorded below; the original
> text is kept struck-through for history.

Current (actual) flow:

- `main` is the trunk. Completed phase work lands on `main` and ships in a
  `0.1x.y` release. Partial IR work on `main` is acceptable because deprecated
  paths stay behind the compatibility window until the `v0.14.0` cutover.
- Each phase uses a short-lived integration branch (e.g.
  `feat/ir-p8-migrate-cutover` for Phase 8). Sub-issue branches stack onto it and
  merge via PR; the phase branch then promotes to `main`.
- Start a new phase/sub-issue branch from `main`, or from the active phase
  integration branch when stacking within a phase.
- Release notes and migration-guide updates are required at each promotion to
  `main`, not only at program end.

Original (superseded) policy:

- ~~`feat/ir-first` is the staging branch for all roadmap work.~~
- ~~Starting work on any phase issue requires creating a new branch from `feat/ir-first`.~~
- ~~Phase PRs must target `feat/ir-first` (not `main`).~~
- ~~Completed phase work merges back into `feat/ir-first`; merge into `main` only when the IR program is complete.~~

## Traceability rule (roadmap <-> GitHub issues)

- Every roadmap deliverable and exit gate must reference one or more GitHub issues.
- Every GitHub issue created for this program must link back to the roadmap document and the relevant phase/section.
- A phase is not considered complete unless all referenced issues are resolved and listed under that phase.
- If scope changes, update roadmap references in the same PR that adds/splits/relabels the issue.
- Roadmap and issue content are a single source of execution truth and must remain synchronized at all times.
- Any roadmap change that affects scope, acceptance criteria, ownership, status, risk, sequencing, or exit gates must be reflected in all already-created linked issues in the same work session (or same PR when applicable).
- Any issue change that affects those same dimensions must be reflected in the roadmap in the same work session (or same PR when applicable).

### Project board and native sub-issues (required)

Filing an issue is not enough. Every epic and sub-issue must be enrolled on the
GitHub Project and wired into the native issue hierarchy **at filing time** —
text references and markdown checklists do not satisfy this.

1. **Add to the board.** Every issue goes on **Project #7** (org `syn54x`,
   <https://github.com/orgs/syn54x/projects/7>):
   `gh project item-add 7 --owner syn54x --url <issue-url>`. Set `Status` (new
   work → `Todo`).
2. **Use GitHub native sub-issues.** Link each sub-issue to its epic with the
   **native sub-issue** relationship — the issue UI ("Create sub-issue" / "Add
   existing issue") or the GraphQL mutation (note the feature header):
   ```
   gh api graphql -H "GraphQL-Features: sub_issues" -f query='
     mutation { addSubIssue(input:{ issueId:"<EPIC_NODE_ID>", subIssueId:"<SUB_NODE_ID>" }) { subIssue { number } } }'
   ```
   A markdown checklist in the epic body is optional context, **not** a
   substitute for the native link.
3. **Group by milestone, not the `Phase` field.** Assign the phase milestone
   `IR-P<phase>` (e.g. `IR-P8.5`): `gh issue edit <n> --milestone IR-P<phase>`
   (create it first if absent: `gh api repos/syn54x/ferro-orm/milestones -f title=...`).
   The board's `Phase` single-select is stale (options stop at `7`) and unused —
   do not rely on it.
4. **Verify before claiming done:** the epic reports N native sub-issues and
   every issue appears on Project #7 with a `Status`.

### Reference format

Use issue references inline under each phase:

- `Epic:` [#number](link)
- `Sub-issues:` [#number](link), [#number](link), [#number](link)

Use this backlink line in issue bodies:

- `Roadmap reference: docs/plans/2026-06-19-001-ir-first-roadmap.md (Phase X)`

### Sync protocol (required)

When editing roadmap content:

1. Update the relevant linked issues immediately.
2. Verify issue titles/body checklists/acceptance criteria still match roadmap text.
3. Add or update issue links in the roadmap if issues were split/renumbered.

When editing issue content:

1. Update the matching roadmap phase/section immediately.
2. Verify roadmap status, gates, and references still match issue state.
3. Record any scope boundary changes in both places.

Definition of synchronized:

- A reader can start from either the roadmap or the issues and get the same current scope, gates, and status with no contradictions.

## Phase tracker

### Phase 0 - Contract and RFC freeze

Status: `Complete`

Issue references:

- `Epic:` [#71](https://github.com/syn54x/ferro-orm/issues/71)
- `Sub-issues:` [#72](https://github.com/syn54x/ferro-orm/issues/72), [#73](https://github.com/syn54x/ferro-orm/issues/73), [#74](https://github.com/syn54x/ferro-orm/issues/74)

**Objective**
- Define versioned IR contracts and non-negotiable invariants before implementation.

**Deliverables**
- [x] RFC: `SchemaIR`, `QueryIR`, `CodecIR` structure and versioning strategy.
- [x] Invariant spec doc covering parity, hydration ABI, null/bind correctness.
- [x] Golden test vector format for schema/query/codec conformance fixtures.

**Exit gate**
- [x] RFC approved and merged.
- [x] Golden vectors committed and validated by CI harness skeleton.

**Evidence (merged to `feat/ir-first`)**
- Phase 0 merge PR: [#75](https://github.com/syn54x/ferro-orm/pull/75) (merge commit `c8c5308`)
- RFC: `docs/rfc/ir-contracts-v1.md`
- Invariant spec: `docs/solutions/patterns/ir-invariants.md`
- Golden vectors: `tests/fixtures/ir_vectors/README.md`, `tests/fixtures/ir_vectors/*.json`
- CI harness skeleton: `tests/test_ir_vectors_contract.py`
- CI wiring: `.github/workflows/ci.yml` (IR vector contract harness step in Python test jobs)
- Issue sync comments:
  - [#71 comment](https://github.com/syn54x/ferro-orm/issues/71#issuecomment-4752226422)
  - [#72 comment](https://github.com/syn54x/ferro-orm/issues/72#issuecomment-4752226080)
  - [#73 comment](https://github.com/syn54x/ferro-orm/issues/73#issuecomment-4752226172)
  - [#74 comment](https://github.com/syn54x/ferro-orm/issues/74#issuecomment-4752226261)

---

### Phase 1 - Build IR core and compiler

Status: `Complete`

Issue references:

- `Epic:` [#76](https://github.com/syn54x/ferro-orm/issues/76)
- `Sub-issues:` [#77](https://github.com/syn54x/ferro-orm/issues/77), [#78](https://github.com/syn54x/ferro-orm/issues/78), [#79](https://github.com/syn54x/ferro-orm/issues/79)

**Objective**
- Introduce a Rust-owned IR crate and compile Python model metadata into deterministic IR artifacts.

**Deliverables**
- [x] `ferro-schema-ir` crate added with versioned serde types.
- [x] Python -> SchemaIR compiler path added.
- [x] IR hashing/fingerprinting persisted for model sets.

**Exit gate**
- [x] Existing representative models compile to stable IR snapshots in CI.
- [x] No user-visible behavior changes yet.

**Evidence (working branch; pending merge to `feat/ir-first`)**
- IR crate: `crates/ferro-schema-ir/` (versioned serde types + RFC vector round-trip tests)
- Compiler + persistence: `src/ferro/ir/compiler.py`, `src/ferro/ir/__init__.py`, `src/ferro/metaclass.py`, `src/ferro/relations/__init__.py`, `src/ferro/state.py`
- Stable representative snapshot fixture: `tests/fixtures/ir_vectors/schema_phase1_fixture_models_v1.json`
- CI gate extension: `tests/test_ir_vectors_contract.py` (snapshot-compare + determinism tests)
- Verification commands:
  - `cargo test -p ferro-schema-ir`
  - `uv run pytest tests/test_ir_vectors_contract.py -q`
  - `uv run pytest tests/test_cross_emitter_parity.py -q`

---

### Phase 2 - Shadow runtime dual-run

Status: `Complete`

Issue references:

- `Epic:` [#80](https://github.com/syn54x/ferro-orm/issues/80)
- `Sub-issues:` [#81](https://github.com/syn54x/ferro-orm/issues/81), [#82](https://github.com/syn54x/ferro-orm/issues/82), [#83](https://github.com/syn54x/ferro-orm/issues/83)

**Objective**
- Run IR-derived runtime planning in shadow mode and compare semantics against current runtime path.

**Deliverables**
- [x] Query/DDL shadow planner behind internal flag.
- [x] Semantic diff harness (result shape, bind semantics, not only SQL strings).
- [x] Backend matrix shadow reports for SQLite/Postgres.

**Exit gate**
- [x] Zero semantic mismatches across integration suite.
- [x] Shadow reports are stable and required in CI for touched paths.

**Evidence (merged to `feat/ir-first`)**
- Phase 2 merge PR: [#105](https://github.com/syn54x/ferro-orm/pull/105) (merge commit `383a3ab`)
- Query/DDL shadow compare wiring: `src/backend.rs`, `src/connection.rs`, `src/operations.rs`, `src/query.rs`, `src/schema.rs`, `src/migrate.rs`
- Shadow report fixtures + harness: `tests/fixtures/shadow_reports/`, `tests/test_shadow_reports.py`
- CI touched-path gate: `.github/workflows/ci.yml` (`changed-shadow-paths`, `test-shadow-reports-pr`)
- Issue sync comments:
  - [#80 comment](https://github.com/syn54x/ferro-orm/issues/80#issuecomment-4753239409)
  - [#81 comment](https://github.com/syn54x/ferro-orm/issues/81#issuecomment-4753239194)
  - [#82 comment](https://github.com/syn54x/ferro-orm/issues/82#issuecomment-4753239269)
  - [#83 comment](https://github.com/syn54x/ferro-orm/issues/83#issuecomment-4753239338)

---

### Phase 3 - QueryIR cutover

Status: `In progress`

Issue references:

- `Epic:` [#84](https://github.com/syn54x/ferro-orm/issues/84)
- `Sub-issues:` [#85](https://github.com/syn54x/ferro-orm/issues/85), [#86](https://github.com/syn54x/ferro-orm/issues/86), [#87](https://github.com/syn54x/ferro-orm/issues/87)

**Objective**
- Move query execution to typed QueryIR and retire internal JSON query contracts.

**Deliverables**
- [x] Runtime query compilation consumes QueryIR.
- [x] Lambda predicate style is first-class; legacy operator style on deprecation track.
- [x] JSON query payload bridge removed from core execution path.

**Exit gate**
- [x] Query builder integration tests pass fully on QueryIR path.
- [x] Compatibility behavior explicitly documented for remaining public API differences.

**Evidence (working branch; pending merge to `feat/ir-first`)**
- QueryIR envelope emission from Python query builder: `src/ferro/query/builder.py`, `src/ferro/query/nodes.py`
- QueryIR envelope consumption on runtime query operations: `src/operations.rs`, `src/ferro/_core.pyi`
- Query/typing/deprecation test coverage: `tests/test_query_builder.py`, `tests/test_query_typing.py`, `tests/test_static_contracts.py`, `tests/test_shadow_reports.py`
- Deprecated-compat test inventory marker: `pytest.mark.deprecated_operator_path` (see `pyproject.toml`)
- Docs + migration updates for deprecation/compatibility: `docs/pages/guide/queries.md`, `docs/pages/concepts/query-typing.md`, `docs/pages/api/queries.md`, `docs/examples/predicates.py`, `docs/plans/ir-first-migration-guide.md`
- Verification command:
  - `uv run pytest tests/test_static_contracts.py tests/test_query_builder.py tests/test_query_typing.py tests/test_shadow_reports.py -q`

---

### Phase 4 - SchemaIR migration and DDL cutover

Status: `Complete` (Alembic path; runtime `auto_migrate` cutover deferred to Phase 8)

Issue references:

- `Epic:` [#88](https://github.com/syn54x/ferro-orm/issues/88)
- `Sub-issues:` [#89](https://github.com/syn54x/ferro-orm/issues/89), [#90](https://github.com/syn54x/ferro-orm/issues/90), [#91](https://github.com/syn54x/ferro-orm/issues/91)

**Objective**
- Make SchemaIR the only schema truth for migration planning and DDL emission.

**Deliverables**
- [x] `ferro-migrate` planner scaffold: `SchemaIR(old) -> SchemaIR(new) -> MigrationPlan`.
- [~] Backend emitters from `MigrationPlan` — entrypoint landed; executable DDL for all ops deferred to Phase 8 ([#90](https://github.com/syn54x/ferro-orm/issues/90) scaffold only).
- [x] Alembic adapter consumes IR outputs (no independent schema derivation).

**Exit gate**
- [x] Alembic no longer independently derives schema semantics.
- [ ] Runtime `auto_migrate` executes the IR migration plan — **deferred to Phase 8**.

**Evidence (merged to `feat/ir-first`)**
- New planner/emitter crate: `crates/ferro-migrate/` (`plan_from_ir`, `emit_sql`, unit tests)
- SchemaIR fidelity updates: `src/ferro/ir/compiler.py`, `crates/ferro-schema-ir/src/lib.rs`
- Runtime planner IR adapter scaffold (plan computed, not executed): `src/migrate.rs`
- Alembic metadata now derives from SchemaIR modelset: `src/ferro/migrations/alembic.py`
- Deprecation coverage for superseded JSON helper path: `tests/test_alembic_bridge.py`
- Verification commands:
  - `cargo test -p ferro-schema-ir -p ferro-migrate`
  - `uv run pytest tests/test_ir_vectors_contract.py tests/test_alembic_bridge.py tests/test_alembic_autogenerate.py tests/test_migrate_plan.py tests/test_auto_migrate.py -q`

---

### Phase 5 - Codec and hydration ABI unification

Status: `Complete`

Issue references:

- `Epic:` [#92](https://github.com/syn54x/ferro-orm/issues/92)
- `Sub-issues:` [#93](https://github.com/syn54x/ferro-orm/issues/93), [#94](https://github.com/syn54x/ferro-orm/issues/94), [#95](https://github.com/syn54x/ferro-orm/issues/95)

**Objective**
- Centralize bind/fetch type behavior and formalize the hydration ABI.

**Deliverables**
- [x] Unified codec registry used by insert/update/filter/m2m/fetch.
- [x] Hydration ABI layer with explicit Pydantic slot initialization contract.
- [x] Codec conformance tests for null/uuid/decimal/temporal/enum semantics.

**Exit gate**
- [x] One hydration path only.
- [x] One codec path only (documented exceptions removed or justified by design).

**Evidence (working branch; pending merge to `feat/ir-first`)**
- Unified codec registry + routing: `src/codec.rs`, `src/operations.rs`, `src/query.rs`, `src/lib.rs`
- Hydration ABI unification: `src/hydration.rs`, `src/operations.rs`, `tests/test_hydration.py`
- Codec vectors + conformance: `tests/fixtures/ir_vectors/codec_registry_core_v1.json`, `tests/test_ir_vectors_contract.py`, `tests/test_typed_null_binds.py`, `tests/test_structural_types.py`, `tests/test_temporal_types.py`, `tests/test_enum_cold_hydration.py`
- Verification commands:
  - `cargo check`
  - `cargo test -p ferro-schema-ir`
  - `uv run pytest tests/test_ir_vectors_contract.py tests/test_hydration.py tests/test_typed_null_binds.py tests/test_structural_types.py tests/test_temporal_types.py tests/test_enum_cold_hydration.py -q`

---

### Phase 6 - Sessionized runtime state

Status: `Complete`

Issue references:

- `Epic:` [#96](https://github.com/syn54x/ferro-orm/issues/96)
- `Sub-issues:` [#97](https://github.com/syn54x/ferro-orm/issues/97), [#98](https://github.com/syn54x/ferro-orm/issues/98), [#99](https://github.com/syn54x/ferro-orm/issues/99)

**Objective**
- Replace global mutable registries in hot paths with explicit session/engine scoped state.

**Deliverables**
- [x] Session/UnitOfWork abstraction for identity map + transaction state.
- [x] Engine-scoped registries replace global lookups in core operations.
- [x] Temporary compatibility shim for legacy call sites.

**Exit gate**
- [x] Core CRUD/query paths no longer require global registries.
- [x] Session lifecycle and concurrency semantics documented.

**Evidence (working branch; pending merge to `feat/ir-first`)**
- Session API + ambient context routing: `src/ferro/session.py`, `src/ferro/state.py`, `src/ferro/models.py`, `src/ferro/query/builder.py`, `src/ferro/raw.py`, `src/ferro/__init__.py`, `src/ferro/_core.pyi`
- Session-scoped Rust runtime state for tx/identity-map hot paths: `src/state.rs`, `src/operations.rs`, `src/connection.rs`, `src/lib.rs`
- Session lifecycle/concurrency + compatibility coverage: `tests/test_session.py`, `tests/test_connection.py`, `tests/test_transactions.py`, `tests/test_named_connections_integration.py`
- Docs/migration/invariant updates for sessionized runtime and compatibility shim: `docs/pages/guide/connections.md`, `docs/pages/guide/transactions.md`, `docs/pages/howto/multiple-databases.md`, `docs/pages/concepts/identity-map.md`, `docs/pages/api/connection.md`, `docs/pages/api/transactions.md`, `docs/plans/ir-first-migration-guide.md`, `docs/solutions/patterns/ir-invariants.md`
- Verification commands:
  - `cargo check`
  - `cargo test` (fails in local environment due missing Python symbol linkage for PyO3 test target; non-blocking for Phase 6 code paths)
  - `cargo test -p ferro-schema-ir -p ferro-migrate`
  - `uv run pytest tests/test_session.py tests/test_connection.py tests/test_transactions.py tests/test_named_connections_integration.py tests/test_query_builder.py tests/test_raw_sql.py tests/test_crud.py tests/test_deletion.py tests/test_bulk_update.py tests/test_hydration.py tests/test_ir_vectors_contract.py tests/test_shadow_reports.py -q`

---

### Phase 7 - Major-version public release with compatibility window

Status: `Complete`

Issue references:

- `Epic:` [#100](https://github.com/syn54x/ferro-orm/issues/100)
- `Sub-issues:` [#101](https://github.com/syn54x/ferro-orm/issues/101), [#102](https://github.com/syn54x/ferro-orm/issues/102), [#103](https://github.com/syn54x/ferro-orm/issues/103)

**Objective**
- Release the IR-first upgrade publicly while keeping deprecated compatibility paths available through a defined migration window.

**Deliverables**
- [x] Public upgrade release shipped with migration guide and deprecation messaging.
- [x] Deprecated compatibility paths remain available during the migration window.
- [x] Deprecated-compat test inventory is tagged and tracked for removal (`pytest.mark.deprecated_operator_path`).
- [x] Migration guide and upgrade checklist.
- [x] Final release checklist and changelog entries.

**Exit gate**
- [x] Release branch green across full backend/test matrix.
- [x] Migration guide validated against at least one real example project.
- [x] Deprecation warnings explicitly point to `v0.14.0` as the removal release.

**Evidence (working branch; pending merge to `feat/ir-first`)**
- Deprecation messaging consistency primitive + call-site adoption: `src/ferro/_deprecations.py`, `src/ferro/query/builder.py`, `src/ferro/state.py`, `src/ferro/migrations/alembic.py`
- Deprecated inventory and warning-target coverage: `tests/test_deprecated_operator_inventory.py`, `tests/test_query_builder.py`, `tests/test_query_typing.py`, `tests/test_session.py`, `tests/test_alembic_bridge.py`
- Phase 7 migration/release docs: `docs/plans/ir-first-migration-guide.md`, `docs/pages/howto/migrating-to-v0-12-0.md`, `docs/plans/ir-first-release-checklist.md`, `docs/pages/guide/queries.md`, `docs/pages/concepts/query-typing.md`, `docs/pages/api/queries.md`, `docs/pages/guide/connections.md`, `docs/pages/api/migrations.md`, `zensical.toml`, `CHANGELOG.md`
- Migration-guide validation against real example project surface: `uv run pytest -v tests/test_docs_examples.py` (includes `docs/examples/*.py` scripts and migration guide code snippet validation)
- Verification commands:
  - `cargo test --no-default-features --features testing`
  - `cargo test -p ferro-schema-ir -p ferro-migrate`
  - `uv run pytest -v tests/test_ir_vectors_contract.py`
  - `uv run pytest -v --cov=src --cov-report=xml --cov-report=term`
  - `uv run pytest -v -m "backend_matrix or postgres_only" --db-backends=sqlite,postgres`
  - `uv run pytest -v -m deprecated_operator_path`
  - `uv run pytest -v tests/test_query_builder.py tests/test_query_typing.py tests/test_session.py tests/test_alembic_bridge.py tests/test_docs_examples.py`
  - `uv run zensical build --clean`
  - `uv run maturin build --release`

---

### Phase 8 - Runtime migration IR cutover (`v0.13.0`)

Status: `Not started`

Issue references:

- `Epic:` [#117](https://github.com/syn54x/ferro-orm/issues/117)
- `Sub-issues:` [#118](https://github.com/syn54x/ferro-orm/issues/118), [#119](https://github.com/syn54x/ferro-orm/issues/119), [#120](https://github.com/syn54x/ferro-orm/issues/120)

**Objective**
- Finish `ferro-migrate` and cut runtime `auto_migrate` over to the SchemaIR migration pipeline so runtime DDL and Alembic share one planner (AGENTS.md I-1).

**Deliverables**
- [ ] `ferro-migrate` `emit_sql` emits executable DDL for all `MigrationOp` variants on SQLite and Postgres (no comment placeholders).
- [x] `plan_table_migration` executes the IR plan as the primary runtime path; legacy enriched-JSON diff walk **deprecated** but retained for shadow comparison (removal deferred to Phase 9).
- [x] Shadow/parity gate: IR migration path matches legacy planner, `create_tables`, and Alembic for the `auto_migrate` capability matrix.
- [x] `shadow_compare_migration_plan` compares IR vs legacy output (not legacy roundtrip); `FERRO_SHADOW_RUNTIME` / `FERRO_SHADOW_RUNTIME_STRICT` enforce drift in CI.
- [~] Duplicate `schema_json_to_schema_ir` / `live_columns_to_schema_ir` lowering consolidated or single-sourced where feasible. **Correction (2026-06-26 audit):** consolidation was scoped to the migrate path only. The runtime CREATE emitter (`src/schema.rs`) still uses its own `CanonicalType` (not `ferro-ddl-lowering`), and SchemaIR is still produced by two independent compilers (Python + Rust). Full single-sourcing moved to **Phase 8.5**.

**Exit gate**
- [ ] `cargo test -p ferro-migrate` green with full op coverage.
- [ ] `tests/test_auto_migrate.py` and `tests/test_migrate_plan.py` green on SQLite + Postgres backend matrix.
- [ ] IR planner is live; no discarded `_typed_plan` scaffolding in `migrate.rs`.
- [x] Legacy JSON diff planner deprecated and shadow-compared; **not** removed (Phase 9).

**Verification commands**
- `cargo test -p ferro-schema-ir -p ferro-migrate`
- `uv run pytest tests/test_ir_vectors_contract.py tests/test_migrate_plan.py tests/test_auto_migrate.py tests/test_alembic_autogenerate.py -q`
- `uv run pytest -m "backend_matrix or postgres_only" --db-backends=sqlite,postgres tests/test_auto_migrate.py tests/test_migrate_plan.py -q`

---

### Phase 8.5 - Lowering consolidation & single-source-of-truth closeout (gates Phase 9)

Status: `Not started`

Issue references:

- `Epic:` [#139](https://github.com/syn54x/ferro-orm/issues/139)
- `Sub-issues:` [#140](https://github.com/syn54x/ferro-orm/issues/140), [#141](https://github.com/syn54x/ferro-orm/issues/141), [#142](https://github.com/syn54x/ferro-orm/issues/142), [#143](https://github.com/syn54x/ferro-orm/issues/143), [#144](https://github.com/syn54x/ferro-orm/issues/144)

> Inserted as `8.5` (not renumbered) so existing Phase 9 issue references
> (#107–#110) stay valid per the traceability/sync rules. Source:
> [`ir-first-lowering-consolidation-audit.md`](../solutions/architecture-patterns/ir-first-lowering-consolidation-audit.md).

**Objective**
- Make the schema/DDL domain actually single-sourced — the property the program
  set as a success criterion but has not yet met. Until this lands, cross-emitter
  parity (AGENTS.md I-1) is test-enforced rather than structural, and the legacy
  planner cannot be safely removed.

**Deliverables**
- [ ] `src/schema.rs` (runtime CREATE) consumes `ferro-ddl-lowering`; its private
      `CanonicalType` / `canonical_to_db_type_token` are deleted. One canonical
      type system across CREATE and migrate.
- [ ] Single SchemaIR producer: Rust consumes the Python-compiled SchemaIR over
      FFI (as the Query path does) instead of rebuilding it via
      `schema_json_to_schema_ir` / `live_columns_to_schema_ir`.
- [ ] Duplicated helpers in `src/migrate.rs` (`pg_alter_type_target`,
      `sqlite_declared_type`, `sqlite_type_class`, `single_unique_index_name`,
      `fk_action_sql`, `literal_default_value`) removed in favor of the
      `ferro-ddl-lowering` originals.
- [ ] IR `db_type` is authoritative, or it is dropped for non-explicit columns
      (emitters must not silently re-derive a value that disagrees with the IR).
- [ ] Auto-migrate **reconciles standalone Ferro-named indexes & uniques** (single
      + composite) on existing tables: ADD missing always; DROP orphaned (Ferro-named,
      gone from the model) under `migrate_destructive` — symmetric with column
      add/drop. Requires new live index introspection (`live_table_indexes`) on
      SQLite **and** Postgres; migrate-added indexes must byte-match what the create
      path emits (AGENTS.md I-1). Inline single-column `UNIQUE` on existing columns
      stays out of scope. _(Scope expanded 2026-06-26 from "populate the IR or
      document the cut" — a user expects auto-migrate to add their indexes; see
      [#144](https://github.com/syn54x/ferro-orm/issues/144). This is the largest 8.5
      sub-issue.)_

**Exit gate**
- [ ] Exactly one `CanonicalType` and one SchemaIR producer remain in the tree
      (grep-verified; no parallel encoders).
- [ ] Cross-emitter parity is backed by shared code, not only by
      `test_cross_emitter_parity.py` / `test_db_type_cross_emitter_parity.py`
      (those become regression sentinels over shared lowering, not the primary
      guarantee).
- [ ] Backend matrix green on SQLite + Postgres.

**Verification commands**
- `cargo test -p ferro-schema-ir -p ferro-migrate`
- `cargo test --no-default-features --features testing`
- `uv run pytest tests/test_cross_emitter_parity.py tests/test_db_type_cross_emitter_parity.py tests/test_ir_vectors_contract.py -q`
- `uv run pytest -m "backend_matrix or postgres_only" --db-backends=sqlite,postgres tests/test_auto_migrate.py tests/test_migrate_plan.py -q`

---

### Phase 8.6 - Post-8.5 cross-crate / single-source cleanups

Status: `In progress`

Issue references:

- `Epic:` [#145](https://github.com/syn54x/ferro-orm/issues/145)
- `Sub-issues:` [#146](https://github.com/syn54x/ferro-orm/issues/146) _(dialect enums)_, [#153](https://github.com/syn54x/ferro-orm/issues/153) _(create-path unification; **done**, merged via #157)_, [#154](https://github.com/syn54x/ferro-orm/issues/154) _(datetime→timestamptz coarseness)_, [#155](https://github.com/syn54x/ferro-orm/issues/155) _(py3.14 deferred annotations)_, [#158](https://github.com/syn54x/ferro-orm/issues/158) _(single db_check check-renderer)_

> Inserted as `8.6` (post-8.5 cleanup backlog). Does **not** gate Phase 9 — it
> executes after the 8.5 consolidation lands. Source: cleanups surfaced during
> Phase 8.5.

**Objective**
- Collect and execute the cross-crate duplication / single-source cleanups that
  are the same "parallel abstractions kept in sync by hand" anti-pattern as the
  8.5 lowering work, at the type/boilerplate level.

**Deliverables**
- [x] Unify the runtime CREATE TABLE path onto the Python SchemaIR (single
      declared-schema producer + single create-table emitter)
      ([#153](https://github.com/syn54x/ferro-orm/issues/153); merged via #157).
- [ ] Unify the three dialect enums (`SqlDialect`/`BackendKind`,
      `ferro-ddl-lowering::Dialect`, `ferro-migrate::BackendDialect`) into one
      shared `Dialect` in a leaf crate; delete the per-seam translation helpers
      ([#146](https://github.com/syn54x/ferro-orm/issues/146)).
- [ ] Single check-renderer for `db_check` CHECK SQL; drop the positional
      `render_check_expression` re-quoting added in #153
      ([#158](https://github.com/syn54x/ferro-orm/issues/158)).
- [ ] _Additional items from a read-only Rust duplication sweep (planned after
      #140 lands): duplicated enums/types across crates, same-logic functions in
      multiple places, parallel match-arm "translation" boilerplate, repeated
      magic mappings._

**Exit gate**
- [ ] Backend matrix green; all cleanups behavior-preserving (no DDL/runtime change).

---

### Phase 9 - Compatibility cutover and shim removal (`v0.14.0`)

Status: `Not started`

> **Dependency (2026-06-26 audit):** removal of the deprecated enriched-JSON
> migration planner in `src/migrate.rs` must not land until **Phase 8.5** has
> single-sourced the IR path. Deleting the legacy planner while SchemaIR still has
> two producers and the CREATE path still diverges would leave parity unguarded.

Issue references:

- `Epic:` [#107](https://github.com/syn54x/ferro-orm/issues/107)
- `Sub-issues:` [#108](https://github.com/syn54x/ferro-orm/issues/108), [#109](https://github.com/syn54x/ferro-orm/issues/109), [#110](https://github.com/syn54x/ferro-orm/issues/110)

**Objective**
- Complete migration by removing deprecated compatibility shims in `v0.14.0`.

**Deliverables**
- [ ] Legacy compatibility code paths removed (operator-style predicates, ambient session routing, private Alembic JSON helpers, **deprecated enriched-JSON migration planner** in `src/migrate.rs`).
  - Includes removal of the deprecated `canonical_from_parts` `("string", Some(...))` arms and the `#[cfg(test)]` `schema_json_to_schema_ir` parity fixture — both fall with `plan_table_migration_legacy` and remain required by it until this phase. Re-homed here from #153 (create-path unification, Phase 8.6) which confirmed they are not needed by the create path.
- [ ] Deprecated-compat test inventory removed (all `deprecated_operator_path` tests deleted or rewritten).
- [ ] Final migration-guide cutover notes for `v0.14.0`.
- [ ] Release checklist and changelog entries for shim removal.

**Exit gate**
- [ ] Full backend/test matrix green with deprecated paths removed.
- [ ] Migration guide validated against at least one real example project on the `v0.14.0` code path.

## Workstreams and ownership

- WS1 - IR contracts and compiler (`Rust core + Python bridge`)
- WS2 - Query/runtime cutover (`Query builder + operations`)
- WS3 - Migration/DDL cutover (`Schema + Alembic adapter`)
- WS4 - Codecs/hydration ABI (`bind/fetch + Pydantic integration`)
- WS5 - Session architecture (`identity map + transaction routing`)
- WS6 - Developer experience (`docs, upgrade tooling, release`)

Assign one directly responsible owner (DRO) per phase and one reviewer owner per workstream.

## Target user experience (north star)

The migration is successful only if Ferro becomes more predictable for users while keeping ergonomics high.

### Desired API posture

- Explicit path is first-class:
  - `async with engines.session("analytics") as s:`
  - `await s.query(Metric).where(...).all()`
- Convenience path is supported inside an active session context:
  - `async with engines.session("analytics"):`
  - `metrics = await Metric.all()`
- Both paths are equivalent in behavior and safety.

### Session discovery contract

- `Model` operations may auto-discover the active session from async context.
- Discovery source is a session-scoped context value only (no hidden fallback to global default connection).
- If no active session exists, operations fail with a clear actionable error.
- Explicit session parameters override ambient discovery when both are present.

### Multi-DB UX intent

- Multi-DB routing is explicit and composable (`session("name")` or explicit session handle).
- No routing from untrusted input.
- No implicit cross-connection transaction semantics.
- Session/identity behavior is scoped to the selected connection.

### Caveats to track across phases

- Nested sessions must shadow and restore correctly.
- Async task spawning rules for context propagation must be documented and tested.
- Ambient convenience must not reintroduce hidden-global behavior.
- Error messages for missing/mismatched sessions must be precise and stable.

### UX examples (directional)

Explicit session-first style:

```python
async with engines.session("analytics") as s:
    metrics = await s.query(Metric).where(lambda t: t.value > 0).all()
```

Convenience ActiveRecord-like style inside session context:

```python
async with engines.session("analytics"):
    metrics = await Metric.where(lambda t: t.value > 0).all()
```

Deterministic failure outside session context:

```python
metrics = await Metric.all()  # RuntimeError: no active session
```

Explicit override beats ambient session when needed:

```python
async with engines.session("app"):
    async with engines.session("analytics") as analytics_session:
        metrics = await Metric.all(session=analytics_session)
```

### UX acceptance criteria (program-level)

- Users can choose explicit `session.query(...)` or convenience `Model.*` inside session contexts.
- The same operation returns the same result and semantics in either style.
- Attempting convenience model calls outside a session raises a deterministic error.
- Multi-DB behavior is deterministic under concurrent coroutine workloads.

## GitHub Project mapping

**Live project:** org `syn54x`, **Project #7** — <https://github.com/orgs/syn54x/projects/7>.
Enrollment, native sub-issues, and milestone assignment are **mandatory** for
every issue; see *Traceability rule → Project board and native sub-issues
(required)* above for the exact steps.

> **Field note (2026-06-26):** in practice the board is driven by the **`Status`**
> field (`Todo` / `In Progress` / `Done`) plus the **`IR-P<phase>` milestone**,
> with phase/workstream carried in the issue title prefix (`[IR-P<phase>][WSx]`).
> The custom `Phase` single-select is stale (options stop at `7`) and unused —
> **group by milestone**. The field list below is the original design intent, not
> the live schema.

**Project fields (original design intent)**
- `Phase`: 0, 1, 2, ... — *stale; use the `IR-P<phase>` milestone instead*
- `Workstream`: WS1..WS6
- `Type`: RFC, Infra, Runtime, Migration, Test, Docs, Release
- `Status`: Backlog, Ready, In Progress, Blocked, In Review, Done
- `Exit Gate`: No/Yes
- `Risk`: Low, Medium, High

**Issue template shape**
- Title: `[IR-P<phase>][WSx] <short outcome>`
- Body:
  - Problem statement
  - Intended invariant
  - Scope boundaries
  - Migration impact (`none|minor|breaking`) and required guide updates
  - Deliverable checklist
  - Validation plan
  - Rollback/failure mode

## Milestone cadence

- Milestone per phase (`IR-P0`, `IR-P1`, ...); inserted sub-phases use a `.5`
  suffix milestone (e.g. `IR-P8.5`).
- Weekly roadmap review:
  - phase status changes
  - new blockers and risk level updates
  - gate evidence links added

## Risk register

- Risk: hidden coupling to legacy JSON/query internals.
  - Mitigation: keep shadow diff harness mandatory through Phase 3.
- Risk: migration planner parity regressions during cutover.
  - Mitigation: golden vector suite plus backend matrix at every phase gate.
- Risk: compatibility shim drag extends timeline.
  - Mitigation: define shim sunset at creation time with a hard owning phase.
- Risk: "single source of truth" is asserted by phase gates but not realized in
  code — parallel type-lowering encoders and a dual SchemaIR producer persist,
  so cross-emitter parity is test-enforced rather than structural (2026-06-26
  audit).
  - Mitigation: Phase 8.5 collapses the encoders and producers; it gates Phase 9
    so the legacy planner is not removed while parity is unguarded.

## Progress log

Append updates as concise entries.

- `2026-06-19` - Roadmap initialized.
- `2026-06-19` - Branching policy set: phase work branches from `feat/ir-first` and merges back into `feat/ir-first` until final promotion to `main`.
- `2026-06-19` - Phase 0 completed and merged via [#75](https://github.com/syn54x/ferro-orm/pull/75).
- `2026-06-19` - Phase 1 implementation landed on working branch: added `ferro-schema-ir`, Python->SchemaIR compiler, model-set fingerprinting, and stable representative snapshot checks.
- `2026-06-19` - Phase 2 scaffolding landed on working branch: internal shadow runtime flag/hook wiring, semantic comparison harness, stable SQLite/Postgres shadow report fixtures, and touched-path CI gate for shadow reports.
- `2026-06-19` - Phase 2 merged via [#105](https://github.com/syn54x/ferro-orm/pull/105); issues [#80](https://github.com/syn54x/ferro-orm/issues/80), [#81](https://github.com/syn54x/ferro-orm/issues/81), [#82](https://github.com/syn54x/ferro-orm/issues/82), [#83](https://github.com/syn54x/ferro-orm/issues/83) synchronized and closed.
- `2026-06-19` - Phase 3 working-branch implementation landed: QueryIR envelope hot-path cutover for query operations, operator-style deprecation warnings, and synchronized query docs/migration guidance updates.
- `2026-06-19` - Sequencing update: Phase 7 is now public release with deprecated compatibility support; hard removal moved to Phase 8 (`v0.14.0`).
- `2026-06-19` - Phase 9 issue set created and linked: epic [#107](https://github.com/syn54x/ferro-orm/issues/107) with sub-issues [#108](https://github.com/syn54x/ferro-orm/issues/108), [#109](https://github.com/syn54x/ferro-orm/issues/109), [#110](https://github.com/syn54x/ferro-orm/issues/110) (originally filed as Phase 8; renumbered 2026-06-23).
- `2026-06-23` - Phase 8 issue set created and linked: epic [#117](https://github.com/syn54x/ferro-orm/issues/117) with sub-issues [#118](https://github.com/syn54x/ferro-orm/issues/118), [#119](https://github.com/syn54x/ferro-orm/issues/119), [#120](https://github.com/syn54x/ferro-orm/issues/120).
- `2026-06-23` - Sequencing update: inserted Phase 8 (`ferro-migrate` runtime cutover, `v0.13.0`); prior shim-removal phase renumbered to Phase 9 (`v0.14.0`). Phase 4 exit gates corrected to reflect Alembic-only cutover on `feat/ir-first`.
- `2026-06-24` - Phase 8 scope update: legacy enriched-JSON migration planner is deprecated and shadow-compared in Phase 8; hard removal moves to Phase 9 (`v0.14.0`) after parity confidence.
- `2026-06-19` - Phase 4 working-branch implementation landed: added `ferro-migrate` (`SchemaIR(old,new)` diff + SQL emission entrypoint), expanded SchemaIR fidelity (enum/check/join-table coverage), switched Alembic metadata derivation to SchemaIR, and added deprecation warnings for superseded JSON-only Alembic helpers (target removal `v0.14.0`).
- `2026-06-22` - Phase 5 working-branch implementation landed: added unified codec registry module (`src/codec.rs`) across insert/update/filter/m2m/fetch paths, extracted single hydration ABI helper (`src/hydration.rs`) with required Pydantic slot initialization, and expanded codec conformance vectors/tests for null/uuid/decimal/temporal/enum semantics.
- `2026-06-22` - Phase 6 working-branch implementation landed: introduced sessionized runtime API (`engines.session` / `Session`) and ambient session routing, moved transaction/identity-map hot-path state to session scope in Rust with compatibility fallback + deprecation warnings, added session lifecycle tests, and synchronized migration/guide/API/invariant docs.
- `2026-06-22` - Phase 7 working-branch implementation landed: completed version-centric public migration guidance (`Migrating to v0.12.0`), added release checklist + changelog entries, centralized `v0.14.0` deprecation-target messaging across compatibility paths, added deprecated-path inventory tests, and validated full Rust/Python/docs/release verification matrix.
- `2026-06-26` - Code-grounded architecture audit recorded at `docs/solutions/architecture-patterns/ir-first-lowering-consolidation-audit.md`. Finding: the schema/DDL domain is not yet single-sourced — runtime CREATE (`src/schema.rs`) keeps a private `CanonicalType` and consumes no IR; SchemaIR has two independent producers (Python `compile_schema_ir_payload` + Rust `schema_json_to_schema_ir`); the type system is encoded in ~5 parallel places. Corrected the overclaimed Phase 8 consolidation deliverable (`[x]` → `[~]`).
- `2026-06-26` - Inserted **Phase 8.5** (lowering consolidation & single-source-of-truth closeout); gates Phase 9 shim removal. Numbered `8.5` (no renumbering) to preserve existing Phase 9 issue references (#107–#110).
- `2026-06-26` - Phase 8.5 issues filed and linked: epic [#139](https://github.com/syn54x/ferro-orm/issues/139) with sub-issues [#140](https://github.com/syn54x/ferro-orm/issues/140), [#141](https://github.com/syn54x/ferro-orm/issues/141), [#142](https://github.com/syn54x/ferro-orm/issues/142), [#143](https://github.com/syn54x/ferro-orm/issues/143), [#144](https://github.com/syn54x/ferro-orm/issues/144). Audit captured in PR [#138](https://github.com/syn54x/ferro-orm/pull/138).
- `2026-06-26` - Inserted **Phase 8.6** (post-8.5 cross-crate / single-source cleanup backlog; does not gate Phase 9). Filed epic [#145](https://github.com/syn54x/ferro-orm/issues/145) with seed sub-issue [#146](https://github.com/syn54x/ferro-orm/issues/146) (unify the three dialect enums, surfaced during #140). Remaining 8.6 items to be filed by a read-only Rust duplication sweep planned after #140 lands.
- `2026-06-26` - #140 and #143 merged to the Phase 8.5 integration branch. **#144 scope expanded** from "populate the IR or document the scope cut" to full auto-migrate index/unique reconciliation (ADD always, DROP under `migrate_destructive`, both backends, new live index introspection) — a user expects auto-migrate to add their indexes; now the largest 8.5 sub-issue. Roadmap deliverable + issue #144 updated to match.
- `2026-06-29` - **Phase 8.6 #153 create-path unification** landed on `feat/ir-p8.6-153-create-path-ir` (PRs into integration `feat/ir-p8.6-cleanups`). The runtime CREATE path now consumes the Python SchemaIR via the shared `ferro-migrate` `render_create_table` emitter (inline FKs, byte-identical DDL), added a `binary` `logical_type` for `bytes`, fails loud on unknown column types, and removed the cutover-orphaned JSON create emitter (`build_create_table_sqls` + cluster). **Scope correction:** removal of the deprecated `canonical_from_parts` `("string", Some(...))` arms + the `#[cfg(test)]` `schema_json_to_schema_ir` is **re-homed to Phase 9 / #108** — they remain required by `plan_table_migration_legacy` (retained for shadow comparison until Phase 9), not by the create path. Migration impact `none`.
- `2026-06-29` - **Phase 8.6 #153 merged to integration** `feat/ir-p8.6-cleanups` (PR #157, CI green incl. the Postgres matrix, which caught two real create-path bugs — PK autoincrement default + stale join-table registry — fixed before merge). #153 closed. Phase 8.6 status → `In progress`. Filed [#158](https://github.com/syn54x/ferro-orm/issues/158) (single check-renderer for `db_check`; drop the positional `render_check_expression` re-quoting #153 introduced) as a sub-issue under #145 — kept in 8.6 rather than deferred to Phase 9. Remaining 8.6 work: #146 (dialect enums), #154, #155, #158.

## Immediate next actions

- [x] Create GitHub issues for Phase 0 deliverables.
- [x] Create `IR-P0` milestone and seed with Phase 0 issues.
- [ ] Assign DRO for Phase 0 RFC.
