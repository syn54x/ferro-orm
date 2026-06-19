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
- Legacy compatibility shims are removed in the explicit `v0.13.0` cutover.
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

- Overall status: `In progress`
- Current phase: `Phase 3`
- Last updated: `2026-06-19`
- Roadmap owner: `@syn54x`

## Branching and release policy

IR program integration branch:

- `feat/ir-first` is the staging branch for all roadmap work.
- Starting work on any phase issue requires creating a new branch from `feat/ir-first`.
- Phase PRs must target `feat/ir-first` (not `main`).
- Completed phase work merges back into `feat/ir-first`.

Promotion to release:

- `main` must not receive partial IR migration work.
- Merge `feat/ir-first` into `main` only when the IR program is complete and release-ready.
- Release notes and migration guide updates are required at promotion time.

## Traceability rule (roadmap <-> GitHub issues)

- Every roadmap deliverable and exit gate must reference one or more GitHub issues.
- Every GitHub issue created for this program must link back to the roadmap document and the relevant phase/section.
- A phase is not considered complete unless all referenced issues are resolved and listed under that phase.
- If scope changes, update roadmap references in the same PR that adds/splits/relabels the issue.
- Roadmap and issue content are a single source of execution truth and must remain synchronized at all times.
- Any roadmap change that affects scope, acceptance criteria, ownership, status, risk, sequencing, or exit gates must be reflected in all already-created linked issues in the same work session (or same PR when applicable).
- Any issue change that affects those same dimensions must be reflected in the roadmap in the same work session (or same PR when applicable).

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

Status: `In progress`

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

Status: `Not started`

Issue references:

- `Epic:` [#88](https://github.com/syn54x/ferro-orm/issues/88)
- `Sub-issues:` [#89](https://github.com/syn54x/ferro-orm/issues/89), [#90](https://github.com/syn54x/ferro-orm/issues/90), [#91](https://github.com/syn54x/ferro-orm/issues/91)

**Objective**
- Make SchemaIR the only schema truth for migration planning and DDL emission.

**Deliverables**
- [ ] `ferro-migrate` planner: `SchemaIR(old) -> SchemaIR(new) -> MigrationPlan`.
- [ ] Backend emitters from `MigrationPlan`.
- [ ] Alembic adapter consumes IR outputs (no independent schema derivation).

**Exit gate**
- [ ] No independent parallel DDL emitter remains.
- [ ] Cross-emitter parity class is structurally eliminated.

---

### Phase 5 - Codec and hydration ABI unification

Status: `Not started`

Issue references:

- `Epic:` [#92](https://github.com/syn54x/ferro-orm/issues/92)
- `Sub-issues:` [#93](https://github.com/syn54x/ferro-orm/issues/93), [#94](https://github.com/syn54x/ferro-orm/issues/94), [#95](https://github.com/syn54x/ferro-orm/issues/95)

**Objective**
- Centralize bind/fetch type behavior and formalize the hydration ABI.

**Deliverables**
- [ ] Unified codec registry used by insert/update/filter/m2m/fetch.
- [ ] Hydration ABI layer with explicit Pydantic slot initialization contract.
- [ ] Codec conformance tests for null/uuid/decimal/temporal/enum semantics.

**Exit gate**
- [ ] One hydration path only.
- [ ] One codec path only (documented exceptions removed or justified by design).

---

### Phase 6 - Sessionized runtime state

Status: `Not started`

Issue references:

- `Epic:` [#96](https://github.com/syn54x/ferro-orm/issues/96)
- `Sub-issues:` [#97](https://github.com/syn54x/ferro-orm/issues/97), [#98](https://github.com/syn54x/ferro-orm/issues/98), [#99](https://github.com/syn54x/ferro-orm/issues/99)

**Objective**
- Replace global mutable registries in hot paths with explicit session/engine scoped state.

**Deliverables**
- [ ] Session/UnitOfWork abstraction for identity map + transaction state.
- [ ] Engine-scoped registries replace global lookups in core operations.
- [ ] Temporary compatibility shim for legacy call sites.

**Exit gate**
- [ ] Core CRUD/query paths no longer require global registries.
- [ ] Session lifecycle and concurrency semantics documented.

---

### Phase 7 - Major-version public release with compatibility window

Status: `Not started`

Issue references:

- `Epic:` [#100](https://github.com/syn54x/ferro-orm/issues/100)
- `Sub-issues:` [#101](https://github.com/syn54x/ferro-orm/issues/101), [#102](https://github.com/syn54x/ferro-orm/issues/102), [#103](https://github.com/syn54x/ferro-orm/issues/103)

**Objective**
- Release the IR-first upgrade publicly while keeping deprecated compatibility paths available through a defined migration window.

**Deliverables**
- [ ] Public upgrade release shipped with migration guide and deprecation messaging.
- [ ] Deprecated compatibility paths remain available during the migration window.
- [ ] Deprecated-compat test inventory is tagged and tracked for removal (`pytest.mark.deprecated_operator_path`).
- [ ] Migration guide and upgrade checklist.
- [ ] Final release checklist and changelog entries.

**Exit gate**
- [ ] Release branch green across full backend/test matrix.
- [ ] Migration guide validated against at least one real example project.
- [ ] Deprecation warnings explicitly point to `v0.13.0` as the removal release.

---

### Phase 8 - Compatibility cutover and shim removal (`v0.13.0`)

Status: `Not started`

Issue references:

- `Epic:` _TBD_
- `Sub-issues:` _TBD_

**Objective**
- Complete migration by removing deprecated compatibility shims in `v0.13.0`.

**Deliverables**
- [ ] Legacy compatibility code paths removed.
- [ ] Deprecated-compat test inventory removed (all `deprecated_operator_path` tests deleted or rewritten).
- [ ] Final migration-guide cutover notes for `v0.13.0`.
- [ ] Release checklist and changelog entries for shim removal.

**Exit gate**
- [ ] Full backend/test matrix green with deprecated paths removed.
- [ ] Migration guide validated against at least one real example project on the `v0.13.0` code path.

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

## GitHub Project mapping (ready to instantiate)

Use this roadmap as the source for issues and project fields.

**Recommended project fields**
- `Phase`: 0, 1, 2, 3, 4, 5, 6, 7, 8
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

- Milestone per phase (`IR-P0`, `IR-P1`, ...).
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

## Progress log

Append updates as concise entries.

- `2026-06-19` - Roadmap initialized.
- `2026-06-19` - Branching policy set: phase work branches from `feat/ir-first` and merges back into `feat/ir-first` until final promotion to `main`.
- `2026-06-19` - Phase 0 completed and merged via [#75](https://github.com/syn54x/ferro-orm/pull/75).
- `2026-06-19` - Phase 1 implementation landed on working branch: added `ferro-schema-ir`, Python->SchemaIR compiler, model-set fingerprinting, and stable representative snapshot checks.
- `2026-06-19` - Phase 2 scaffolding landed on working branch: internal shadow runtime flag/hook wiring, semantic comparison harness, stable SQLite/Postgres shadow report fixtures, and touched-path CI gate for shadow reports.
- `2026-06-19` - Phase 2 merged via [#105](https://github.com/syn54x/ferro-orm/pull/105); issues [#80](https://github.com/syn54x/ferro-orm/issues/80), [#81](https://github.com/syn54x/ferro-orm/issues/81), [#82](https://github.com/syn54x/ferro-orm/issues/82), [#83](https://github.com/syn54x/ferro-orm/issues/83) synchronized and closed.
- `2026-06-19` - Phase 3 working-branch implementation landed: QueryIR envelope hot-path cutover for query operations, operator-style deprecation warnings, and synchronized query docs/migration guidance updates.
- `2026-06-19` - Sequencing update: Phase 7 is now public release with deprecated compatibility support; hard removal moved to Phase 8 (`v0.13.0`).

## Immediate next actions

- [x] Create GitHub issues for Phase 0 deliverables.
- [x] Create `IR-P0` milestone and seed with Phase 0 issues.
- [ ] Assign DRO for Phase 0 RFC.
