---
date: 2026-05-08
topic: typed-query-predicates
---

# Typed Query Predicates

## Summary

Add two opt-in query-predicate styles to Ferro — a `col()` wrapper and a lambda predicate API — so that `Model.field` comparisons type-check cleanly under Pyright and `ty` without changing user model annotations or breaking the current operator API. A concept doc covers when to reach for each.

---

## Problem Frame

Ferro models are Pydantic models, so user fields carry plain-type annotations like `archived: bool` and `transcript_id: int`. At runtime the metaclass replaces each class attribute with a `FieldProxy` whose operator overloads return `QueryNode`, so `User.archived == False` builds a query predicate that `Query.where()` accepts.

Static type checkers don't see that runtime swap. Pyright and `ty` resolve `User.archived` from the source annotation as `bool`, so `User.archived == False` is interpreted as `bool.__eq__(bool)` and returns `bool`. `Query.where()` then complains that it expected a `QueryNode`. Users hitting this on every typed predicate currently silence each line with a `# type: ignore` pragma.

Other Python ORMs work around this by changing what users annotate (SQLAlchemy 2.x uses `Mapped[T]`), changing the API away from class-attribute predicates (Django and Tortoise use kwargs), or shipping a mypy plugin. None of those fit Ferro: `Mapped[T]`-style breaks the "user types directly" stance and adds Pydantic interop work, and a mypy plugin doesn't help Pyright or `ty` users — Pyright has no public plugin API.

---

## Requirements

**`col()` wrapper**

- R1. Ferro exposes a `col()` function whose static signature is `(value: T) -> FieldProxy[T]`. At runtime it is identity, with a guard that raises `TypeError` if the input is not a `FieldProxy`.
- R2. `col(Model.field) == value` (and the other comparison operators) produces a `QueryNode` that `Query.where()` accepts in mypy, Pyright, basedpyright, and `ty` without ignore pragmas.
- R3. `FieldProxy` becomes generic over its column type (`FieldProxy[T]`), with operator overloads typed to accept `T | FieldProxy[T]` and return `QueryNode`. Runtime behavior is unchanged; users do not write the generic parameter directly.

**Lambda predicate API**

- R4. `Query.where()` accepts a callable that takes a typed query proxy and returns a `QueryNode`. The proxy's static type is parameterized on the model type; attribute access on the proxy is statically typed as `FieldProxy[Any]`.
- R5. At runtime, when `where()` receives a callable that is not a `QueryNode`, it constructs a per-call proxy whose `__getattr__` returns `FieldProxy(name)`, invokes the callable with the proxy, and appends the returned node to the query.
- R6. Lambda predicates support compound expressions via the existing `&` and `|` operators on `QueryNode`.

**Backward compatibility**

- R7. The existing `Model.field == value` operator path continues to work unchanged at runtime. No existing test, query, or user model needs modification.
- R8. Adding the lambda overload does not change the runtime semantics of any current `where()` call. Dispatch checks `isinstance(QueryNode)` first, then `callable()`.
- R9. No changes to the model metaclass, Pydantic schema generation, JSON schema bridge, or the Rust hydration paths.

**Documentation**

- R10. A concept document under `docs/concepts/` covers the three predicate styles (operator, `col()`, lambda), shows them combined in one query chain, and recommends when to reach for each. Lambda is positioned as the recommended idiom for new code; `col()` is the lower-ceremony fallback for users who want to keep the operator shape.

---

## Acceptance Examples

- AE1. **Covers R1, R2.** Given `archived: bool` declared on a model and a strict type checker run, when the user writes `.where(col(T.archived) == False)`, the line type-checks without ignore pragmas in mypy, Pyright, basedpyright, and `ty`.
- AE2. **Covers R7, R8.** Given an existing test that uses `.where(T.field == value)`, when the new code lands, the test continues to pass with no modification.
- AE3. **Covers R4, R5, R6.** Given the predicate `lambda t: (t.archived == False) & (t.org_id == 42)`, when passed to `.where(...)`, the runtime invokes the lambda with a model proxy, appends a single compound `QueryNode` to the query, and produces the same SQL as the equivalent operator-form predicate.
- AE4. **Covers R1.** Given a value that is not a `FieldProxy` (e.g., a raw string), when passed to `col()` at runtime, `col()` raises `TypeError` whose message names the actual argument type.
- AE5. **Covers R2, R7, R4.** Given a single `Query` chain that combines `.where(T.a == 1)`, `.where(col(T.b) == 2)`, and `.where(lambda t: t.c == 3)` in any order, when executed, the resulting SQL contains all three predicates and the result hydrates correctly.

---

## Success Criteria

- Ferro users hitting the original `ty` complaint can fix it by wrapping the attribute reference with `col(...)` — a single small change per call site, with no model edits and no plugin installation.
- New Ferro code in user projects can adopt the lambda predicate style and have those predicates type-check cleanly in every supported checker.
- Downstream agents (planning, future API work) and human reviewers have a clear definition of which `where()` shapes are supported and which were deliberately deferred.
- Backward-compat tests in `tests/` pass without modification, demonstrating the operator-path runtime is unchanged.

---

## Scope Boundaries

- `Mapped[T]` / descriptor-based field annotations are deferred indefinitely; they conflict with Ferro's "user types directly" stance and would require Pydantic interop work disproportionate to the typing win.
- A mypy plugin for Ferro is out of scope; Pyright and `ty` are the target checkers and neither has an equivalent plugin API.
- Per-field type precision in the lambda proxy via `@dataclass_transform` is deferred; this work uses `FieldProxy[Any]` for proxy attribute types.
- A kwargs-style filter API (e.g., `.where_kw(field=value)`) is deferred until user demand surfaces.
- A `t""` template-string predicate API is deferred until Ferro raises its minimum Python to 3.14.
- Per-model code generation, distributed `.pyi` stubs for user models, and `if TYPE_CHECKING` shadow declarations as a documented pattern are not adopted.
- No changes to the metaclass, Pydantic interop, JSON schema generation, or the Rust FFI bridge.

---

## Key Decisions

- **Single PR over staged PRs.** The three pieces (`col()`, lambda API, doc) ship together on `feat/col-and-lambda-queries`. They are small, low-risk, and meaningfully more useful as a unit than incrementally.
- **`col()` is identity at runtime, not a constructor.** `Model.field` is already a `FieldProxy` thanks to the metaclass, so `col()` is a typed pass-through, not a wrapping layer. This keeps the runtime cost zero and avoids a parallel class hierarchy.
- **Lambda predicate proxy is per-call, not stored on the model.** Avoids touching the metaclass and keeps the lambda path opt-in. Users who never call `where(lambda ...)` never construct the proxy.
- **`FieldProxy[Any]` over per-field precision for the lambda proxy.** Per-field types would require `@dataclass_transform` plumbing and tighter user-facing typing semantics. Deferred to a follow-up if real demand materializes.
- **Lambda is the recommended idiom; `col()` is the fallback.** Lambda has lower per-call ceremony for chains with several predicates; `col()` is one wrapper per reference but doesn't ask users to rewrite their query shape.

---

## Dependencies / Assumptions

- The current metaclass behavior of replacing each `model_fields` attribute with a `FieldProxy` is a stable invariant the runtime no-op `col()` relies on.
- `QueryNode` does not currently expose `__call__`; the runtime dispatch rule "isinstance(QueryNode) first, then callable()" depends on this remaining true.
- Generic parameters on `FieldProxy[T]` are runtime-erased (standard Python typing semantics); existing `isinstance(x, FieldProxy)` checks remain valid.
