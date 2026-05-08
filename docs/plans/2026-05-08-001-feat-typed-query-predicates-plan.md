---
title: "feat: Typed query predicates (col() + lambda)"
type: feat
status: active
date: 2026-05-08
origin: docs/brainstorms/2026-05-08-typed-query-predicates-requirements.md
---

# feat: Typed query predicates (col() + lambda)

## Summary

Make `FieldProxy` generic over the column's Python type, add a runtime-identity `col()` wrapper that statically narrows a model attribute back to `FieldProxy[T]`, and extend `Query.where` / `Relation.where` to accept lambda predicates of shape `Callable[[QueryProxy[TModel]], QueryNode]`. All three predicate styles (operator, `col()`, lambda) coexist on the same `where()` so existing user code keeps working unchanged. No metaclass, Pydantic, or Rust FFI changes.

---

## Problem Frame

Pyright and `ty` resolve `Model.field` to its Pydantic-annotated type (`bool`, `int`, …) rather than the `FieldProxy` instance the metaclass installs at class-creation time. As a result, `Model.archived == False` is statically `bool`, which fails `Query.where`'s `QueryNode` parameter and forces users to sprinkle `# ty: ignore` in real queries. Origin: `docs/brainstorms/2026-05-08-typed-query-predicates-requirements.md`.

---

## Requirements

- R1. `FieldProxy` is generic over its column's Python type (`FieldProxy[T]`).
- R2. Operator overloads on `FieldProxy[T]` accept `T | FieldProxy[T]` and return `QueryNode`.
- R3. `col(value: T) -> FieldProxy[T]` exists, is runtime-identity for `FieldProxy` inputs, and raises `TypeError` for anything else.
- R4. `Query.where` accepts either a `QueryNode` or a `Callable[[QueryProxy[TModel]], QueryNode]`.
- R5. `Relation.where` accepts the same two shapes (parity).
- R6. The lambda runtime constructs a per-call `QueryProxy` whose `__getattr__` returns `FieldProxy(name)`.
- R7. All existing operator-path tests pass with no source changes.
- R8. New public symbols (`col`, `QueryProxy`, `Predicate`) are exported from `ferro.query`.
- R9. A concept doc explains all three styles, when to use each, and shows them combined.
- R10. New integration tests cover the lambda path, the `col()` path, and a single chain mixing all three styles.

**Origin acceptance examples:** AE1 (col() typecheck-clean), AE2 (operator path unchanged), AE3 (compound lambda), AE4 (col() misuse error), AE5 (mixed predicate chain).

---

## Scope Boundaries

- No `Mapped[T]` / descriptor-based field annotations.
- No mypy plugin or other type-checker plugin.
- No `@dataclass_transform` per-field typing for `QueryProxy` (proxy attribute type stays `FieldProxy[Any]` for now).
- No kwargs-style filter API.
- No `t""` template-string predicate API.
- No code generation or distributed `.pyi` stubs for user models.
- No changes to the metaclass, Pydantic schema generation, or the Rust FFI bridge.

---

## Context & Research

### Relevant Code and Patterns

- `src/ferro/query/nodes.py` — current `FieldProxy` (non-generic) and `QueryNode`. Operator overloads currently take `Any` and return `QueryNode`. `like` accepts `str`; `in_` accepts `list/tuple/set` with a runtime `TypeError` guard.
- `src/ferro/query/builder.py` — `Query.where` and the `Relation.where` override. Both currently take a single `QueryNode`. Existing `Generic[T]` usage in `Query[T]` and `Relation[T]` is the precedent for generics in this layer.
- `src/ferro/query/__init__.py` — public re-export surface for `Query`, `Relation`, `QueryNode`, `FieldProxy`. New symbols add here.
- `src/ferro/metaclass.py` — installs `FieldProxy` instances on the model class at class-creation. Out of scope for changes; the plan relies on this behavior remaining stable.

### Institutional Learnings

- No prior `docs/solutions/` entry on query typing or descriptor patterns. This work is the first time the query layer takes a static-typing stance.

### External References

- Origin requirements doc: `docs/brainstorms/2026-05-08-typed-query-predicates-requirements.md`.
- Pyright `self: FieldProxy[str]` method-binding pattern (used by SQLAlchemy 2.x for str-only column methods).

---

## Key Technical Decisions

- **`FieldProxy.like` is typed via `self: FieldProxy[str]`** so non-string columns cannot statically reach `.like(...)`. Cheap to add, prevents a real footgun, mirrors SQLAlchemy 2.x.
- **`in_` is typed `list[T] | tuple[T, ...] | set[T]`** to align the static contract with the existing runtime guard.
- **`col()` lives in `src/ferro/query/nodes.py`** alongside `FieldProxy` rather than in a new `dsl.py`. Co-locating the proxy primitives keeps the layer's surface easy to scan.
- **`QueryProxy` lives in `src/ferro/query/nodes.py`** for the same reason. Public re-export from `src/ferro/query/__init__.py`.
- **`Query.where` runtime dispatch order**: `isinstance(node, QueryNode)` → existing path; `elif callable(node)` → lambda path; else `TypeError`. `QueryNode` is not callable today, so this order is unambiguous.
- **`Relation.where` overrides for parity**: `Relation` already overrides `where` to refine the return type to `Relation[T]`; the new overload set must be repeated on the override or relationship queries lose lambda support.
- **`QueryProxy` proxy attributes are typed `FieldProxy[Any]`** until a future PR can teach Pyright/`ty` to map per-field types from a model class. Matches the origin scope decision.
- **Tests for the new behavior live in a new file `tests/test_query_typing.py`** rather than extending `tests/test_query*`. Keeps the typing-focused suite easy to find, leaves existing tests as the unmodified regression bar for R7.

---

## Open Questions

### Resolved During Planning

- **Where does `col()` live?** → `src/ferro/query/nodes.py`, exported from `ferro.query`.
- **Does `Relation.where` need the new overload?** → Yes (parity); origin didn't call it out explicitly but it's required to avoid a typing dead end.
- **How precise should `like` typing be?** → Use `self: FieldProxy[str]`. Worth the one-line cost.

### Deferred to Implementation

- **Exact `TypeVar` bounds for `FieldProxy[T]`**: probably unbounded, but if `FieldProxy.__hash__` / equality semantics force a bound it's discovered when the type-checker actually runs.
- **Whether to add a `__class_getitem__` runtime no-op on `FieldProxy`**: needed only if user code subscripts at runtime; will surface from a test.

---

## Implementation Units

- U1. **Make `FieldProxy` generic and tighten operator types**

**Goal:** `FieldProxy` becomes `FieldProxy[T]`; operator overloads accept `T | FieldProxy[T]` and return `QueryNode`; `like` is gated to `FieldProxy[str]`; `in_` and `__lshift__` are typed with `T`.

**Requirements:** R1, R2.

**Dependencies:** None.

**Files:**
- Modify: `src/ferro/query/nodes.py`
- Test: `tests/test_query_typing.py` (created in U4; U1's regression coverage is the existing suite)

**Approach:**
- Add `TField = TypeVar("TField")`.
- Change `class FieldProxy:` to `class FieldProxy(Generic[TField]):`.
- Update operator signatures to `__eq__(self, other: TField | "FieldProxy[TField]")`, etc.
- Type `in_` as `Iterable[TField]` at runtime (existing guard stays); narrow signature to `list[TField] | tuple[TField, ...] | set[TField]` for the static contract.
- Type `like` with `self: "FieldProxy[str]"`.
- Keep `__hash__` behavior unchanged (Python autogenerates `None` when `__eq__` is overridden; existing code already lives with this — do not introduce a new hash contract).

**Patterns to follow:**
- `Query[T]` / `Relation[T]` in `src/ferro/query/builder.py` for `Generic[TVar]` usage.

**Test scenarios:**
- Happy path: existing operator-path tests under `tests/` still pass with no edits (regression for R7).
- Happy path: at runtime `FieldProxy[bool]("active") == True` returns a `QueryNode` (subscription is type-time only, no runtime change).
- Edge case: `like` still works at runtime when called on any `FieldProxy` (subclass typing constraint must not affect runtime dispatch).
- Edge case: `in_` raises `TypeError` for non-iterable input — unchanged from current behavior.

**Verification:**
- `uv run pytest -q` passes with no source-test edits.
- `uv run pyright src/ferro/query/nodes.py` (or repo-wide) does not regress on existing files.

---

- U2. **Add `col()` runtime-identity wrapper**

**Goal:** Introduce `col(value: TField) -> FieldProxy[TField]` that returns its argument unchanged at runtime when it is a `FieldProxy`, raises `TypeError` otherwise, and statically normalizes a model class attribute back to `FieldProxy[T]`.

**Requirements:** R3, R8.

**Dependencies:** U1.

**Files:**
- Modify: `src/ferro/query/nodes.py`
- Modify: `src/ferro/query/__init__.py`
- Test: `tests/test_query_typing.py` (created in U4)

**Approach:**
- Add `col` as a module-level function near `FieldProxy` so they read together.
- Runtime: `if not isinstance(value, FieldProxy): raise TypeError(f"col() expects a model column reference, got {type(value).__name__}")`; `return value`.
- Static: `def col(value: TField) -> FieldProxy[TField]: ...` plus `# type: ignore[return-value]` on the return because the static narrowing is intentional.
- Add `col` to `__all__` and the import block in `src/ferro/query/__init__.py`. Do not re-export from `ferro.__init__` unless a follow-up touches that surface — this PR keeps the export at `ferro.query`.

**Patterns to follow:**
- The existing module-level `_serialize_query_value` helper in `nodes.py` for placement style.

**Test scenarios:**
- Happy path: `col(User.archived)` returns the same `FieldProxy` instance (identity check via `is`).
- Happy path: `col(User.archived) == False` builds a `QueryNode` with `column="archived"`, `operator="=="`, `value=False`.
- Error path: `col(False)` raises `TypeError` whose message contains `"bool"`.
- Error path: `col("archived")` raises `TypeError` whose message contains `"str"`.
- Integration: `await User.where(col(User.archived) == False).all()` round-trips against an in-memory SQLite fixture and returns the matching rows (proves the existing pipeline accepts the produced `QueryNode` unchanged).

**Verification:**
- `uv run pytest tests/test_query_typing.py -q` is green.
- A static-only assertion (e.g., `assert_type(col(User.archived), FieldProxy[bool])` under `if TYPE_CHECKING:`) compiles cleanly under Pyright; not run as a test, just present as a documented snippet in `tests/test_query_typing.py` so type-checkers exercise it.

---

- U3. **Add lambda predicate API to `Query.where` and `Relation.where`**

**Goal:** Extend `where` to accept `Callable[[QueryProxy[TModel]], QueryNode]`. Construct a per-call `QueryProxy` whose attribute access yields a `FieldProxy(name)`; pass it to the predicate; append the returned `QueryNode`. Repeat on `Relation.where` for parity.

**Requirements:** R4, R5, R6, R8.

**Dependencies:** U1.

**Files:**
- Modify: `src/ferro/query/nodes.py` (add `QueryProxy`)
- Modify: `src/ferro/query/builder.py` (overloads + dispatch on `Query.where` and `Relation.where`)
- Modify: `src/ferro/query/__init__.py` (export `QueryProxy`, `Predicate`)
- Test: `tests/test_query_typing.py` (created in U4)

**Approach:**
- Add `class QueryProxy(Generic[TModel])` in `nodes.py`. Runtime body: `__slots__ = ()`, `__getattr__(self, name) -> FieldProxy[Any]: return FieldProxy(name)`. Static: type the return as `FieldProxy[Any]` for now (origin scope decision).
- Add `Predicate = Callable[[QueryProxy[TModel]], QueryNode]` (TypeAlias) in `nodes.py`.
- In `builder.py`, add `@overload` declarations on `Query.where`: one for `QueryNode`, one for `Predicate[T]`. Implementation signature stays untyped (`node`) to keep both paths legal.
- Implementation body:
  ```
  if isinstance(node, QueryNode):
      self.where_clause.append(node)
  elif callable(node):
      proxy = QueryProxy()  # type: ignore[var-annotated]
      result = node(proxy)
      if not isinstance(result, QueryNode):
          raise TypeError("predicate callable must return QueryNode, got ...")
      self.where_clause.append(result)
  else:
      raise TypeError("where() expected QueryNode or predicate callable, got ...")
  return self
  ```
  *(Directional pseudo-code; do not copy verbatim. The implementer may inline or refactor.)*
- Mirror the overloads on `Relation.where`. The runtime body can delegate to `super().where(node)` once the parent dispatches both shapes.

**Technical design:** *(directional)*

```
where(QueryNode)   ─► append(node)
where(Predicate)   ─► proxy = QueryProxy()
                     node  = predicate(proxy)
                     append(node)
where(other)       ─► TypeError
```

**Patterns to follow:**
- Existing `Relation.where` override at `src/ferro/query/builder.py` for the subclass-narrowing pattern.
- The `if TYPE_CHECKING: @overload` block on `Relation.all` / `Relation.first` for how this codebase mixes static overloads with a single runtime body.

**Test scenarios:**
- Happy path: `Query(User).where(lambda t: t.id == 1)` appends a single `QueryNode` whose `column == "id"`.
- Happy path: `Query(User).where(lambda t: (t.role == "admin") & (t.active == True))` appends one compound `QueryNode` whose `is_compound is True`.
- Happy path: `await User.where(lambda t: t.archived == False).all()` round-trips against in-memory SQLite and returns the expected rows.
- Edge case: `Query(User).where(QueryNode(...))` (the existing path) still works unchanged — regression for R7.
- Error path: `Query(User).where(lambda t: True)` raises `TypeError` because the predicate did not return a `QueryNode`.
- Error path: `Query(User).where(123)` raises `TypeError` whose message names the offending type.
- Integration: `Relation.where(lambda t: t.published == True)` on a `BackRef` relationship round-trips end-to-end (covers R5 parity, not just typing).
- Integration: a single chain — `User.where(User.id == 1).where(col(User.archived) == False).where(lambda t: t.role == "admin")` — produces three accumulated `QueryNode`s and executes against SQLite. *(Covers AE5; this exact assertion is the U4 integration test.)*

**Verification:**
- `uv run pytest tests/test_query_typing.py -q` is green.
- `uv run pytest tests/test_relations*.py -q` (or whatever name covers `Relation`) still green.

---

- U4. **Integration tests for combined predicate styles**

**Goal:** Single end-to-end test file exercising operator + `col()` + lambda styles together against a real SQLite fixture, plus the static-typing snippets that make Pyright actually exercise the new generic types.

**Requirements:** R7, R10.

**Dependencies:** U1, U2, U3.

**Files:**
- Create: `tests/test_query_typing.py`

**Approach:**
- Reuse the existing `db_url` / `db_engine` fixtures used by `tests/test_crud.py` so the file is consistent with the rest of the suite.
- Define a small `Model` per the project's TDD convention (`.cursorrules` §4 step 1).
- Group tests by predicate style (`class TestColWrapper`, `class TestLambdaPredicates`, `class TestCombinedStyles`) so future readers can find the right block fast.
- Include a `if TYPE_CHECKING:` block at the top of the file with `assert_type(...)` calls for `col()` and a lambda-built `QueryNode` to anchor the static contract. These are not runtime assertions — they exist for Pyright/`ty` to consume.

**Patterns to follow:**
- `tests/test_crud.py` for fixture wiring and async test style.
- `.cursorrules` §4 (TDD workflow) — the integration tests are the primary acceptance gate.

**Test scenarios:**
- *Covers AE1.* `col(User.archived) == False` builds a usable `QueryNode` and Pyright sees `FieldProxy[bool]`.
- *Covers AE2.* The unchanged `User.email == "a@b.com"` operator path still executes and returns the row (regression).
- *Covers AE3.* Compound lambda `lambda t: (t.role == "admin") & (t.active == True)` filters correctly.
- *Covers AE4.* `col(False)` raises `TypeError`.
- *Covers AE5.* Mixed chain `User.where(User.id == 1).where(col(User.archived) == False).where(lambda t: t.role == "admin")` returns the expected rows from a seeded SQLite fixture.
- Integration: `Relation.where(lambda t: ...)` via a `BackRef` relationship returns the expected child rows.

**Verification:**
- `uv run maturin develop && uv run pytest tests/test_query_typing.py -q` is green.
- The full suite (`uv run pytest -q`) is green.

---

- U5. **Concept doc for query predicate styles**

**Goal:** Single canonical page documenting the three predicate styles, when to reach for each, and showing them combined on one chain.

**Requirements:** R9.

**Dependencies:** U1, U2, U3 (so the doc reflects the shipping API).

**Files:**
- Create: `docs/concepts/query-typing.md`

**Approach:**
- Title: "Typed query predicates".
- Sections: *Why this exists* (one paragraph on the static-typing gap) → *The three styles* (operator, `col()`, lambda — each with a runnable example) → *When to use which* (lambda for new code; `col()` when a single attribute trips your type checker; operator when the existing form already type-checks) → *Combining styles* (the AE5 chain) → *What this does not change* (no metaclass, Pydantic, or Rust changes; existing code keeps working).
- Cross-link from `docs/concepts/index.md` if one exists; otherwise leave as a standalone page.

**Patterns to follow:**
- `docs/concepts/identity-map.md` for tone, depth, and structure.

**Test scenarios:**
- Test expectation: none — documentation only. Verified manually by reading the rendered page.

**Verification:**
- Renders correctly under the site's markdown renderer (no broken code blocks, no orphan links).

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Pyright/`ty` resolve `where` overloads ambiguously and reject existing operator-path code. | U4's `if TYPE_CHECKING:` `assert_type` block plus the unchanged-existing-tests regression bar from R7 catch this in CI before merge. If the overload order matters, the dispatch keeps `QueryNode` first. |
| `QueryProxy.__getattr__` returning `FieldProxy[Any]` makes the lambda style appear less "typed" than `col()`. | Documented as a known scope boundary in the concept doc; future PR can add `dataclass_transform` if there's appetite. Not a correctness risk. |
| Subscripting `FieldProxy[bool]` at runtime fails because `FieldProxy` doesn't support `__class_getitem__`. | `Generic[T]` provides `__class_getitem__` automatically — no extra work needed. Verified by U1's runtime test. |
| `Relation.where` parity is forgotten in U3, leaving relationship queries with no lambda support. | Explicit unit dependency (U3 names `Relation.where` in **Files** and **Test scenarios**); U4's relation-lambda integration test fails loudly if dropped. |

---

## Documentation / Operational Notes

- New concept doc lands as part of this PR (U5). No existing docs need rewrites — `docs/concepts/identity-map.md` and friends do not reference the typing posture.
- No release-notes / migration guide needed; the change is purely additive and backward compatible.

---

## Sources & References

- **Origin document:** [docs/brainstorms/2026-05-08-typed-query-predicates-requirements.md](docs/brainstorms/2026-05-08-typed-query-predicates-requirements.md)
- Related code: `src/ferro/query/nodes.py`, `src/ferro/query/builder.py`, `src/ferro/query/__init__.py`, `src/ferro/metaclass.py`
- Related work: prior identity-map config PR on the same branch family.
