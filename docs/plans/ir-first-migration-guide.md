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

_TBD_

### Phase 4

_TBD_

### Phase 5

_TBD_

### Phase 6

_TBD_

### Phase 7

_TBD_
