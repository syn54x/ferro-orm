---
title: IR-first merge-readiness review checklist
date: 2026-06-23
category: architecture-patterns
module: ir-first
problem_type: architecture_pattern
component: development_workflow
severity: medium
applies_when:
  - "Large architecture branch (e.g. feat/ir-first) is nearing merge to main"
  - "A phase gate claims unified behavior across multiple code paths"
  - "Python modules import-and-reexport mutable singletons from a central state module"
  - "CI aggregate jobs depend on upstream path-detection jobs"
resolution_type: code_fix
tags:
  - ir-first
  - schema-ir
  - query-ir
  - merge-readiness
  - ffi
  - pyo3
  - typed-null-binds
related_components:
  - documentation
  - testing_framework
---

# IR-first merge-readiness review checklist

## Context

Before merging `feat/ir-first` (PR #116) to `main`, a merge-readiness review
flagged six concrete defects that would ship if the branch merged as-is. Commit
`99ae829` ("fix: address IR-first merge-readiness review findings") fixed the
code and CI issues; unstaged doc edits then aligned `architecture.md`, the
IR-first roadmap, and the migration guide with the actual program state (Phases
0–7 complete; Phase 8 = `ferro-migrate` runtime cutover; Phase 9 = shim removal).

The findings were not "IR-first is incomplete" — they were specific invariant
violations and documentation drift that a phase-completion gate should have
caught. (session history)

**Residual risk accepted at merge (not blocking):** runtime `auto_migrate` still
executes the legacy enriched-JSON diff in `src/migrate.rs`; `ferro-migrate`
executable DDL is Phase 8 ([#117](https://github.com/syn54x/ferro-orm/issues/117)–[#120](https://github.com/syn54x/ferro-orm/issues/120)).

## Guidance

### 1. SchemaIR modelset cache must write through `ferro.state`

**Finding:** `compile_registry_schema_ir()` imported `_SCHEMA_IR_MODELSET` from
`ferro.state` then used `global _SCHEMA_IR_MODELSET`. In Python, that binds
module-local globals — not attributes on `ferro.state`. Alembic `get_metadata()`
and parity tests reading `ferro.state._SCHEMA_IR_MODELSET` saw `None` after
compilation.

**Fix:** Assign explicitly on the state module:

```python
ferro_state._SCHEMA_IR_MODELSET = envelope
ferro_state._SCHEMA_IR_MODELSET_FINGERPRINT = _fingerprint(envelope)
```

**Test hygiene:** Registry fixtures must clear `_SCHEMA_IR_MODELSET`,
`_SCHEMA_IR_MODELSET_FINGERPRINT`, `_SCHEMA_IR_BY_MODEL`, and
`_SCHEMA_IR_FINGERPRINT_BY_MODEL` — not just model/join registries. Add a
contract test asserting the cache is populated after `compile_registry_schema_ir()`.

### 2. Codec registry — one decimal-NULL strategy per backend

**Finding:** Postgres decimal `None` used inconsistent bind types across codec
paths (`float8`-typed NULL on some paths, `numeric` on others). Phase 5 claimed
unified codec behavior; merge review caught the divergence.

**Fix:** Early return in `schema_bind_expr` for Postgres decimal NULL:

```rust
if col_is_decimal && backend == SqlDialect::Postgres {
    return Ok(Expr::value(SeaValue::String(None)).cast_as("numeric"));
}
```

Assert both bind variant (`SeaValue::String(None)`) and SQL cast (`numeric`) in
unit tests — not just "INSERT accepts None." See
[`typed-null-binds.md`](../patterns/typed-null-binds.md).

### 3. No `unwrap()` across the FFI boundary (AGENTS.md I-3)

**Finding:** `QueryDef::to_condition_for_backend()` and INSERT/M2M builders
called `.unwrap()` on Sea-Query value assembly. Malformed QueryIR or bad bind
tuples would panic into Python as opaque aborts.

**Fix pattern:**

- Change internal builders to `Result<T, String>` with explicit messages.
- Add a thin FFI adapter mapping to `PyValueError`:

```rust
fn query_condition_for_backend(query_def: &QueryDef, backend: SqlDialect) -> PyResult<Condition> {
    query_def
        .to_condition_for_backend(backend)
        .map_err(pyo3::exceptions::PyValueError::new_err)
}
```

### 4. Shadow CI gate must fail closed when path detection fails

**Finding:** The aggregate `all-checks` job could pass when
`changed-shadow-paths` itself failed — a broken detector silently skipped the
shadow gate.

**Fix:** At the top of the aggregate job:

```bash
if [[ "${{ needs.changed-shadow-paths.result }}" == "failure" ]]; then
  echo "Shadow path detection failed; refusing to treat shadow gate as passed."
  exit 1
fi
```

Shadow enforcement is only trustworthy if the "what changed?" job succeeds.

### 5. Do not hand-write release CHANGELOG entries

**Finding:** A manually authored `v0.12.0` CHANGELOG block was added during
Phase 7; the project's release flow generates changelog entries.

**Fix:** Remove the hand-written block. Release artifacts come from the release
pipeline, not ad-hoc commits on the feature branch.

### 6. Roadmap and migration guide must reflect partial phase delivery

**Finding:** Roadmap implied Phase 4 runtime `auto_migrate` cutover was done. In
reality Phase 4 delivered Alembic SchemaIR cutover + `ferro-migrate` scaffold
only; executable DDL and runtime execution are Phase 8.

**Fix (doc sync):**

- Phase 4 status: `Complete (Alembic path; runtime auto_migrate cutover deferred to Phase 8)` with `[~]` on `ferro-migrate` emit_sql.
- Insert **Phase 8** — Runtime migration IR cutover (`v0.13.0`, issues #117–#120).
- Renumber shim removal to **Phase 9** (`v0.14.0`, issues #107–#110).
- Migration guide: #90 = "planner scaffold; executable DDL deferred to Phase 8."
- `architecture.md`: split compile-time (SchemaIR) vs runtime (QueryIR + session + codec) pipelines.

## Why This Matters

- **Silent cache miss** breaks Alembic parity and makes SchemaIR appear compiled
  when consumers read empty state — phantom diffs or missing metadata at release.
- **Bind path drift** causes backend-specific query failures despite a "unified
  codec" phase gate (AGENTS.md I-1 analogue for binds).
- **FFI panics** violate project invariant I-3 and destroy the pytest feedback
  loop on bad IR input.
- **False-green CI** lets shadow regressions merge when the detector is broken.
- **Roadmap/doc drift** makes merge-readiness reviews unreliable: reviewers
  assume runtime migration is IR-driven when it still uses the legacy JSON diff
  in `src/migrate.rs`.

## When to Apply

- **Pre-merge gate reviews** on large architecture branches (especially
  `feat/ir-first` → `main` promotion).
- **After any "unify X across paths" phase** (codec, hydration, migration
  emitters): grep for divergent special-cases and missing parity tests.
- **When Python modules import-and-reexport mutable singletons** from a central
  `state` module: verify assignments target the canonical module, not a shadow
  `global`.
- **When adding CI aggregate jobs** that depend on upstream detection jobs: fail
  closed if detection fails.
- **When closing a phase whose deliverables are partially landed**: update
  roadmap exit gates, migration guide impact tables, and architecture docs in the
  same PR — not as follow-up.

## Examples

**SchemaIR cache contract test:**

```python
def test_compile_registry_schema_ir_persists_modelset_cache(clean_model_registry):
    from ferro import state as ferro_state
    from ferro.ir import compile_registry_schema_ir, schema_ir_fingerprint

    ferro_state._SCHEMA_IR_MODELSET = None
    compiled = compile_registry_schema_ir()

    assert ferro_state._SCHEMA_IR_MODELSET == compiled
    assert ferro_state._SCHEMA_IR_MODELSET_FINGERPRINT == schema_ir_fingerprint(compiled)
```

**QueryIR error propagation (before → after):**

```rust
// Before: silent panic or empty condition on bad IR
let left_cond = self.node_to_condition_for_backend(node.left.as_ref().unwrap(), backend);

// After: actionable Python exception
let left = node.left.as_ref()
    .ok_or_else(|| "compound QueryNode is missing left child".to_string())?;
let left_cond = self.node_to_condition_for_backend(left, backend)?;
```

**Roadmap partial-delivery notation:**

```markdown
**Deliverables**
- [x] Alembic adapter consumes IR outputs
- [~] Backend emitters from MigrationPlan — scaffold landed; executable DDL deferred to Phase 8

**Exit gate**
- [x] Alembic no longer independently derives schema semantics
- [ ] Runtime auto_migrate executes IR migration plan — deferred to Phase 8
```

**Architecture doc: two-pipeline mental model (post-sync):**

```text
Python owns the model authoring surface.
SchemaIR and QueryIR own the cross-language contracts.
Rust owns execution, codecs, and hydration.
Sessions scope routing, transactions, and the identity map.
```

Schema path fans out at class creation: `build_model_schema()` → SchemaIR
(Alembic, ferro-migrate) + enriched JSON (Rust registry). Runtime path crosses
FFI per `await` as QueryIR inside an active `engines.session()`.

## Related

- [`ir-invariants.md`](../patterns/ir-invariants.md) — invariant contract the review validates
- [`cross-emitter-ddl-parity.md`](../patterns/cross-emitter-ddl-parity.md) — concrete parity symptoms/recipes
- [`typed-null-binds.md`](../patterns/typed-null-binds.md) — CodecIR/bind enforcement
- [`docs/plans/2026-06-19-001-ir-first-roadmap.md`](../../plans/2026-06-19-001-ir-first-roadmap.md) — phase status and Phase 8/9 sequencing
- [`docs/plans/ir-first-migration-guide.md`](../../plans/ir-first-migration-guide.md) — deprecated-surface inventory
- [`docs/pages/concepts/architecture.md`](../../pages/concepts/architecture.md) — public architecture alignment target
- Commit `99ae829` — code/CI fixes from this review
- Issues [#117](https://github.com/syn54x/ferro-orm/issues/117)–[#120](https://github.com/syn54x/ferro-orm/issues/120) — Phase 8 runtime migration cutover (open)
