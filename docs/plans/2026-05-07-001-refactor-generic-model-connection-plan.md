---
title: "refactor: Make ModelConnection generic to preserve model typing through .using()"
type: refactor
status: active
date: 2026-05-07
---

# refactor: Make ModelConnection generic to preserve model typing through .using()

## Summary

`ModelConnection` (the object returned by `Model.using(name)`) is not generic, so every method that should return the bound model class falls back to the unbound `Model` base — `Transcript.using(SERVICE).get("...")` resolves to `Model | None` instead of `Transcript | None`. This plan parameterizes `ModelConnection` over `M: Model` using PEP 695 syntax, propagates `M` through every method return type, and renames the conflicting `self.using` instance attribute so the class isn't shadowing its own classmethod entrypoint. No behavior changes — pure typing + naming hygiene.

---

## Requirements

- R1. `Transcript.using(SERVICE).get(pk)` resolves to `Transcript | None` under Pyright/Pylance.
- R2. Every other `ModelConnection` method preserves the model type: `create -> M`, `all -> list[M]`, `select -> Query[M]`, `where -> Query[M]`, `bulk_create -> int`, `get_or_create -> tuple[M, bool]`, `update_or_create -> tuple[M, bool]`.
- R3. The instance attribute previously named `self.using` no longer shadows the `Model.using` classmethod name.
- R4. Existing runtime behavior is preserved — every test under `tests/test_connection.py` and `tests/test_named_connections_integration.py` continues to pass without modification.
- R5. Public surface is unchanged: `Model.using("name")` still returns a `ModelConnection`-shaped object with all the same methods and call signatures.

---

## Scope Boundaries

- No widening of `Model.using()` to accept transaction handles, connection objects, role hints, or anything other than the existing `name: str`.
- No audit of unrelated typing erasure elsewhere in the ORM (relation descriptors, raw queries, query builder internals). Anything discovered during this work that is out-of-scope routes to deferred follow-up rather than expanding this plan.
- No changes to `Query`, `Model`, or any FFI signatures — `Query` is already generic and that infrastructure is sufficient.
- No new type-checker wiring into CI. The repo currently has no Pyright/mypy hook (see `.pre-commit-config.yaml` lines 45–51, type checking is commented out). Adding CI enforcement for static typing is a separate, larger discussion.

### Deferred to Follow-Up Work

- Wiring Pyright into pre-commit / CI so `assert_type` regressions fail builds: separate plan, larger scope (config, ignore lists, baseline noise).
- Audit of `relations/descriptors.py` and other Model-returning APIs for similar erasure: separate plan if/when surfaced.

---

## Context & Research

### Relevant Code and Patterns

- `src/ferro/models.py` lines 452–455: `Model.using` classmethod, already annotated `-> ModelConnection[Self]` (the annotation is aspirational today — `ModelConnection` is not actually subscriptable).
- `src/ferro/models.py` lines 557–619: `ModelConnection` class definition. All eight methods to retype live here.
- `src/ferro/query/builder.py` line 29: `class Query(Generic[T])` — already generic. `select()` and `where()` should return `Query[M]` after this refactor; the constructor `Query(self.model_cls, using=...)` already infers `T` from the class argument.
- `src/ferro/relations/descriptors.py` line 112: `await self._target_model.using(origin).get(id_val)` — only external caller of `ModelConnection`'s instance-level methods we found in repo. Will benefit automatically from the typing fix; no change needed.

### Internal Usages of `self.using` (the instance attribute being renamed)

Confirmed via grep — all references are inside `ModelConnection`'s own method bodies:

- `models.py:566` — `await instance.save(using=self.using)`
- `models.py:570` — `using=self.using`
- `models.py:573` — `Query(..., using=self.using)`
- `models.py:588` — `bulk_create(..., using=self.using)`
- `models.py:593` — `Query(..., using=self.using)`
- `models.py:607` — `Query(..., using=self.using)`
- `models.py:615` — `await instance.save(using=self.using)`

No external code reads `instance.using` on a `ModelConnection` object. Renaming is safe.

### Institutional Learnings

- `.cursorrules` §4 — TDD workflow: write a failing test first, then implement. Applies here even though the "test" for the typing fix is a static `assert_type` rather than runtime assertion. The runtime regression test for the rename is conventional pytest.
- `AGENTS.md` I-3 — no `unwrap()` across FFI. Not applicable here (Python-only change), but reinforces "this should not need to touch Rust."
- `AGENTS.md` I-5 — `docs/solutions/` is institutional memory. Worth adding a short note under `docs/solutions/patterns/` if the PEP 695 generic syntax is the first instance in the codebase, so future agents have a reference.

### External References

- PEP 695 (Type Parameter Syntax, Python 3.12+) — Ferro's `requires-python = ">=3.13"` makes this the modern default over `Generic[M]`. No external lookup needed; the syntax is well-known.

---

## Key Technical Decisions

- **Use PEP 695 syntax (`class ModelConnection[M: Model]:`) over `Generic[M]`.** Project pins Python 3.13+ (`pyproject.toml:9`), so the older syntax has no compatibility benefit and adds an import (`Generic`, `TypeVar`). PEP 695 is also what `Query` should eventually move to for consistency, but that migration is out of scope here.
- **Rename `self.using` to `self._connection_name`.** Underscore-prefix signals "internal — don't read this from outside the class," which matches how the attribute is actually used. Avoids the `Model.using` / `ModelConnection.using` name collision that prompted this work in the first place.
- **Test typing via `typing.assert_type` co-located with existing connection tests, not in a new `tests/typing/` directory.** No type checker runs in CI today, so a separate typing test directory would be a discoverability problem (no signal points at it). Inline `assert_type` calls in the existing test files make the intent visible to readers, run as no-ops at runtime, and can be picked up later if/when Pyright is wired into CI.

---

## Open Questions

### Resolved During Planning

- *Should we use `Generic[M]` or PEP 695 `[M: Model]`?* — PEP 695, see Key Technical Decisions.
- *What to rename `self.using` to?* — `self._connection_name`, see Key Technical Decisions.
- *Do we need to update `src/ferro/_core.pyi`?* — No. `ModelConnection` is a pure-Python class; no FFI signatures cross.

### Deferred to Implementation

- *Does `Query`'s existing generic propagation flow through the new `M` cleanly when `ModelConnection.select()` returns `Query[M]`?* — Should "just work" since `Query(model_cls, ...)` infers `T` from `type[T]`, and after the refactor `self.model_cls: type[M]`. Verified at implementation time by writing the `assert_type` calls and running Pyright locally.

---

## Implementation Units

- U1. **Add typing regression coverage for `.using()`**

**Goal:** Lock in the desired post-refactor types via `typing.assert_type` calls so the typing improvement is observable and won't silently regress.

**Requirements:** R1, R2

**Dependencies:** None.

**Files:**
- Modify: `tests/test_connection.py` (or `tests/test_named_connections_integration.py` — pick the file that already exercises the same model)
- Test: same file (typing assertions co-located with runtime tests)

**Approach:**
- Add a small block of `typing.assert_type(...)` calls against an existing test model (e.g., `ConnectionRouteMarker` or `NamedSmokeMarker`).
- Cover the full surface called out in R2: `create`, `all`, `select`, `where`, `bulk_create`, `get_or_create`, `update_or_create`, `get`.
- These are no-ops at runtime; Pyright/Pylance will flag any future regression. Document at the top of the block that they're for static type checking.

**Execution note:** Test-first per `.cursorrules` §4. Write these assertions before touching `models.py` — Pyright should report errors on every line until U2 lands.

**Patterns to follow:**
- Existing test file structure under `tests/`.

**Test scenarios:**
- Happy path: `assert_type(ConnectionRouteMarker.using("service"), ModelConnection[ConnectionRouteMarker])` — proves the classmethod's annotation is now truthful.
- Happy path: `assert_type(await ConnectionRouteMarker.using("service").get(1), ConnectionRouteMarker | None)` — covers R1 directly.
- Happy path: one `assert_type` per remaining method in R2 (`create`, `all`, `select`, `where`, `bulk_create`, `get_or_create`, `update_or_create`).
- Test expectation at runtime: these statements should execute without error. `assert_type` is a runtime no-op.

**Verification:**
- Before U2 lands: `uv run pytest` passes (assert_type doesn't fail at runtime), but Pyright on the test file reports type errors on each new line.
- After U2 lands: Pyright on the test file is clean.

---

- U2. **Make `ModelConnection` generic and rename the connection-name attribute**

**Goal:** Parameterize `ModelConnection` over `M: Model` and rename `self.using` → `self._connection_name`.

**Requirements:** R1, R2, R3, R5

**Dependencies:** U1.

**Files:**
- Modify: `src/ferro/models.py` (lines 557–619 — the `ModelConnection` class body)

**Approach:**
- Change class header to `class ModelConnection[M: Model]:`.
- `__init__` signature: `model_cls: type[M], connection_name: str` (parameter name change is internal — callers go through `Model.using()` which positional-passes the string).
- `self.model_cls: type[M] = model_cls`; `self._connection_name: str = connection_name`.
- Replace all seven `self.using` references in method bodies with `self._connection_name`.
- Method return-type annotations:
  - `create(...) -> M`
  - `all() -> list[M]`
  - `select() -> Query[M]`
  - `where(node) -> Query[M]`
  - `get(pk) -> M | None`
  - `bulk_create(instances: list[M]) -> int`
  - `get_or_create(...) -> tuple[M, bool]`
  - `update_or_create(...) -> tuple[M, bool]`
- `Model.using` classmethod (lines 452–455) stays as-is — its `-> "ModelConnection[Self]"` annotation now resolves correctly.

**Patterns to follow:**
- `src/ferro/query/builder.py:29` (`class Query(Generic[T])`) for how a generic ORM-side class is wired, though we use the newer PEP 695 syntax here.

**Test scenarios:**
- Covers R4. Happy path: full existing `tests/test_connection.py` and `tests/test_named_connections_integration.py` suites pass unchanged. The rename is purely internal so no behavior changes.
- Covers R1, R2. Happy path: every `assert_type` call added in U1 type-checks clean under Pyright after this unit lands.
- Edge case: confirm that `Model.using("name").select().where(...).first()` chains all the way through — each link preserves `M`. Already covered indirectly by the U1 `select` and `where` assertions plus existing query tests.

**Verification:**
- `uv run pytest tests/test_connection.py tests/test_named_connections_integration.py` is green.
- `uv run maturin develop && uv run pytest` is green (full suite — confirms no relation-descriptor or other internal caller broke).
- Manually run Pyright on `tests/test_connection.py` (or whichever file got the U1 assertions): no errors on the typing block.
- Visual check: `Transcript.using(SERVICE).get("…")` in a fresh editor session shows `Transcript | None` in the hover tooltip.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Renaming `self.using` accidentally breaks an external caller we missed in the grep. | Pre-flight grep confirmed all references are internal to the class body. The full pytest suite runs in U2 verification. |
| `Query[M]` doesn't propagate as cleanly as expected because `Query`'s `__init__` infers `T` differently than expected from `type[M]`. | Verified at implementation time via the `assert_type(self.select(), Query[M])` assertion in U1. If it fails, fall back to an explicit cast or constructor annotation; document the fix in `docs/solutions/patterns/`. |
| The typing improvement degrades inside generic mixins or subclasses of `Model` due to `Self` interaction. | `Model.using` returns `ModelConnection[Self]`; this is the standard pattern and shouldn't fight `M: Model`. If a subclass-specific issue surfaces, scope the fix to that subclass — don't expand U2. |

---

## Sources & References

- Brainstorm conversation in this session establishing scope (generic + rename, no broader audit).
- `src/ferro/models.py:452-619` — current `Model.using` and `ModelConnection` definitions.
- `src/ferro/query/builder.py:29` — existing generic ORM class as reference pattern.
- `pyproject.toml:9` — `requires-python = ">=3.13"`, justifying PEP 695 syntax.
- PEP 695 — Type Parameter Syntax.
