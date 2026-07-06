# FF-F — Query builder 1.0 shape (design)

**Status:** approved (brainstorming) · **Date:** 2026-07-06 · **Branch:** `ff-f/query-builder-1.0`
**Closes finding:** F12 · **Roadmap:** `docs/plans/2026-07-02-001-fable-fixes-roadmap.md` Epic FF-F (L321–365)
**In-development version:** 0.13.0 → semantic-release cuts **0.14.0** from FF-E's `feat(...)!` commits; FF-F's breaking commits land in the same in-development minor. Do not hand-edit version files.

## Objective

Make the query builder 1.0-shaped: the builder is **immutable** (copy-on-write chaining), **misspelled columns fail at build time** with an actionable message, predicates are **statically type-checked as far as Python's type system allows**, deprecation bookkeeping leaves the AST, and `ferro_schema_ir::QueryNode` becomes the **only** query shape in Rust (the `QueryDef` shadow layer and IR↔legacy conversion deleted).

Scope is **F-1…F-5**. **F-6 (aggregations + partial selects) is spun into its own downstream epic** built on the settled shape (see §7).

## The central decision — Fork A (lambda-only)

F-3 (typed proxies) and F-5 (post-operator surface) are one typing-and-injection surface, settled together.

### Why per-field bare-lambda typing is a type-system boundary, not a design choice

The goal "`lambda u: u.age >= "x"` fails the type checker while `lambda u: u.age >= 5` still returns `QueryNode`" was tested empirically against the repo's checker (`ty`). Results:

| Mechanism | rejects `u.age >= "x"` | valid `u.age >= 5` still `QueryNode` |
|---|---|---|
| `QueryProxy.__getattr__ → FieldProxy[Any]` (status quo) | ❌ (Any swallows it) | ✅ |
| lambda param typed as the **model instance** | ✅ (`int >= str` errors) | ❌ comparison yields `bool`, valid predicate fails |
| `@dataclass_transform` over plain `age: int` | ❌ does not retype attribute access | — |
| `Mapped[int]`-style descriptor + param typed `type[T]` | ✅ | ✅ |

Making a **bare** `lambda t: t.field OP value` reject a wrong-typed RHS is **impossible for plain-Pydantic `age: int` declarations**. It works only with SQLAlchemy-`Mapped`-style descriptor columns (`age: Col[int]`), which would replace Ferro's plain-Pydantic field identity and break the both-declaration-styles docs rule.

The missing language construct is a TypeScript-style *mapped type* (`{ [K in keyof T]: FieldProxy<T[K]> }`). Python has no equivalent today. **[PEP 827 – Type Manipulation](https://peps.python.org/pep-0827/)** (Draft, targeting Python 3.16) proposes exactly this (`Members`/`GetMemberType`/`NewProtocol` + iteration), with only an in-progress proof-of-concept in mypy — realistically 2028+ before `ty`/pyright could enforce it. It cannot factor into the 1.0 shape.

### Fork A — the decision

Lambda predicates are the **only** predicate surface. Delete class-attribute `FieldProxy` injection, operator style, and `col()` (all user-observable breaks, `!`, on the v0.14.0 removal track the roadmap already scheduled).

- **Runtime:** `where()` accepts only a predicate callable `Callable[[QueryProxy[T]], QueryNode]`. The lambda receives a validating `QueryProxy` (F-2). `User.age` reverts to normal Pydantic semantics (raises `AttributeError` at class level, as Pydantic v2 does).
- **Static typing:** lambda param typed `QueryProxy[T]`; attribute access returns `FieldProxy[Any]`. **Guarantee 1** holds (valid predicates type-check and return `QueryNode`; `lambda t: True` is rejected — `bool` is not `QueryNode`). **Guarantee 2** (wrong-RHS rejection for bare lambdas) is a documented non-goal, deferred to PEP 827.
- **Forward compatibility:** Fork A is the shape that *best* positions Ferro for PEP 827. When checkers support mapped types, we change **one type alias** — `QueryProxy[T]`'s attribute typing goes from `FieldProxy[Any]` to the mapped protocol — and every existing user lambda gains full RHS checking with **zero runtime change and zero user migration**. Fork C (`Mapped[]`) would instead have forced a breaking declaration migration to buy typing the language is about to provide for free.

**Rejected:** Fork B (keep injection + `col()` as the typed escape hatch) — retains injection and a second predicate surface that PEP 827 would make redundant. Fork C (`Mapped[]` columns) — abandons plain-Pydantic identity; its own epic at most, recommended against.

### Exit-gate re-scope (consequence of Fork A)

The written exit-gate line "`lambda u: u.age >= "x"` fails the type checker" is replaced by what plain-Pydantic can express and what a real gate can enforce:

- Valid lambda predicates type-check and statically resolve to `QueryNode`; `lambda t: True` (and other non-`QueryNode` bodies) are rejected by the checker.
- Operator style and `col()` are removed (import/attribute errors, asserted).
- Misspelled columns fail at **build time** (runtime `AttributeError`), naming valid columns.
- The RHS-type limitation is documented in `test_static_contracts.py` and the queries guide, citing PEP 827 as the enabling future work.

## F-1 — Immutable chaining (copy-on-write)

A private `Query._clone()` performs `copy.copy(self)` then **replaces the mutable containers with fresh copies** so no state is aliased:

```python
def _clone(self) -> "Query[T]":
    new = copy.copy(self)                       # subclass-safe (Relation)
    new.where_clause = list(self.where_clause)
    new.order_by_clause = list(self.order_by_clause)
    new._m2m_context = dict(self._m2m_context) if self._m2m_context is not None else None
    return new                                   # _limit/_offset are immutable scalars
```

Every chain method returns a clone with the single change applied:

```python
def where(self, predicate):
    new = self._clone()
    new.where_clause.append(_resolve_where_node(predicate, self.model_cls))
    return new
```

`where` / `order_by` / `limit` / `offset` and `_m2m` all follow this shape. `first()` becomes `await self.limit(1).all()` — **no** `self._limit` save/restore. `exists()` already delegates to `count()` (no mutation); confirmed unchanged.

Because `copy.copy` preserves the concrete class, `Relation`'s eight `return self` method overrides collapse to **typing-only** overloads (or disappear where the base return type already fits). **Trap guarded:** the container copies are the whole point — a shallow copy sharing `where_clause` would reintroduce the aliasing bug F-1 exists to kill; the exit-gate test asserts `q1` is unchanged after `q2 = q1.where(...)`.

## F-2 — Column validation at build time

`QueryProxy` takes a required `model_cls`. `where()` builds `QueryProxy(self.model_cls)`; `order_by()` validates against the same source.

- **Valid-column set** (single source of truth, computed once per model and cached on the class): `set(model_cls.model_fields)` ∪ `{f"{name}_id" for name, meta in ferro_relations if isinstance(meta, ForeignKey)}` (the shadow FK columns injected at `metaclass.py:583`).
- **`QueryProxy.__getattr__(name)`** — on miss, raise `AttributeError` naming the invalid column, a `difflib` did-you-mean, and the valid columns. On hit, return `FieldProxy(name)`.
- **Timing:** fires at proxy-attribute access (build time, inside the lambda) — the error points at the exact attribute. `order_by` reuses the same validator for lambda-derived and string column names.
- **No unvalidated mode.** `QueryProxy` always carries a model; there is no `QueryProxy()` escape that skips validation (that would be a stop-gap trap).

This deletes the documented "`order_by` lambda produces a junk column — never show it" trap by making it structurally impossible. Grep the docs and remove the warning.

## F-3 / F-5 — the 1.0 `where` / `order_by` surface

- **`where(predicate)`** accepts **only** a predicate callable. The raw-`QueryNode` overload is removed: a hand-built `QueryNode` would bypass F-2 validation, and its only legitimate producers (`col()`, operator style) are gone. Compound predicates compose inside one lambda (`&` / `|`) or across `.where()` calls (implicit AND).
- **`order_by(field, direction="asc")`** accepts a **lambda** (`order_by(lambda u: u.created_at, "desc")`, the documented 1.0 style, consistent with lambda-official) or a **validated string** (`order_by("created_at", "desc")`, shorthand). Both validated against the F-2 column set. The old `order_by(User.username, "desc")` class-attribute form cannot survive (attributes stop being proxies); docs flip from "not a lambda" to lambda-first.
- **Deleted as consequences:** class-attribute `FieldProxy` injection (`metaclass.py:431` real fields, `:583` shadow FK — the FK `{fk}_id` **name** still feeds the F-2 valid-column set, only the proxy `setattr` goes); `predicate_style` bookkeeping in `QueryNode`/`FieldProxy` (the "deprecation state inside the AST" finding); `uses_operator_style`; `_deprecated_operator_query_node` (`builder.py:53`) and the `@deprecated` operator path; `col()` (`nodes.py:334`); the `deprecated_operator_path` pytest marker and its tests. `FieldProxy` **stays** — it is what proxy attributes return inside lambdas — it is simply never injected on model classes.
- **Migration surface (mechanical):** ~10 test files and most docs examples use `order_by(Model.field)` or operator/`col()` predicates; all rewrite to lambda/string. This is the scheduled v0.14.0 operator-style removal, on schedule.

## F-4 — Collapse `QueryDef` onto the IR types

A thin runtime wrapper replaces the shadow layer. New `struct QueryPlan` in `src/query.rs`:

```rust
pub struct QueryPlan {
    pub model_name: String,
    pub where_clause: Vec<ferro_schema_ir::QueryNode>,   // pure IR node (tagged enum)
    pub order_by: Vec<ferro_schema_ir::QueryOrderBy>,     // empty = no ORDER BY
    pub limit: Option<u64>,
    pub offset: Option<u64>,
    pub m2m: Option<M2mContext>,                          // deserialized from payload.m2m
    // runtime-only, NOT IR — never serialized:
    pub postgres_enum_udt: HashMap<String, String>,
    pub registration: Option<Arc<crate::state::RegisteredModel>>,
}
```

- **Constructed from `QueryIrPayload`** (not via serde on the wrapper): `QueryPlan::from_ir_payload(payload) -> Result<QueryPlan, String>` deserializes `m2m` into `M2mContext`, resolves `registration` from `MODEL_REGISTRY`, and leaves `postgres_enum_udt` empty for callers to populate from the catalog before building SQL (unchanged sequencing).
- **Runtime-vs-IR split honored:** `postgres_enum_udt` and `registration` are `#[serde(skip)]` runtime state and live **only** on the wrapper, never on the pure IR type.
- **FF-E loud error preserved:** the `registration == None → "Model '…' not found"` error at `operations.rs:2892` is preserved at/after construction; no fallback is reintroduced (FF-E made the legacy path loud).
- **Condition building** (`to_condition_for_backend`, `node_to_condition_for_backend`, `value_rhs_simple_expr_for_backend`) moves onto `QueryPlan` and walks `ferro_schema_ir::QueryNode` (`Leaf { operator, column, value: QueryValue }` / `Compound { operator, left, right }`) directly. Null handling keys off `value.value.is_null()` / `kind == "null"`.
- **`semantic_signature`** reimplemented over IR nodes.
- **Deleted outright:** legacy `struct QueryNode` (`query.rs:20`), `struct OrderBy` (superseded by `QueryOrderBy`), `struct QueryDef` (`:59`), `query_node_from_ir` (`:466`), `query_node_to_ir` (`:418`), `query_def_from_ir_payload` (`:378`), `to_ir_payload` (`:305`), and the legacy-JSON fallback branch in `operations.rs:2887-2891`.
- **Shadow comparator consequence (explicit):** the query-path shadow comparator exists to compare legacy-vs-IR lowering. After the collapse there is exactly one shape, so `shadow_artifact_from_ir_roundtrip` and the compare pyfunction's query path are **deleted, not kept as a tautology**. Shadow-strict verification still applies to the other paths FF-F touches.
- **Exit-gate grep:** `grep -rn 'struct QueryNode' src/ crates/` returns only the IR crate.

## The `ty` gate

Today `ty` is **not** wired into CI or the justfile; the `# ty: ignore` comments are unenforced, and the repo is not clean (61 diagnostics in `src/ferro`, 294 in tests). Repo-wide enforcement is out of scope (dirty-baseline rule: no bulk reformat/fix).

- Add a **scoped** `just check` target + CI step: `ty check` over `src/ferro/query/` + `tests/test_query_typing.py` + `tests/test_static_contracts.py`.
- The query module's current 6 diagnostics are fixed as part of F-3 so the gate **starts green and actually bites**.
- Repo-wide expansion is an additive follow-up (the gate is a path list; widening is not a redesign) — noted for FF-G.

## F-6 — spun off (confirmed)

Aggregations + partial selects become their own downstream epic on the post-F shape (IR payload extensions + hydration-ABI-aware partial materialization), tracked as a separate roadmap entry/issue. FF-F's PR is **F-1…F-5** only.

## Exit gate (tests written first)

1. **Immutable:** `q1 = User.where(...); q2 = q1.limit(5)` leaves `q1` unchanged; `first()` / `exists()` do not mutate.
2. **Build-time column error:** a misspelled column (in `where` lambda and in `order_by`) raises `AttributeError` naming valid columns.
3. **Static contracts** (`test_static_contracts.py`, under the new `ty` gate): valid lambda predicates resolve to `QueryNode`; `lambda t: True` is rejected; operator style and `col()` are removed (import/attribute error). The bare-lambda RHS limitation is documented, citing PEP 827.
4. **One query shape in Rust:** `grep -rn 'struct QueryNode' src/ crates/` → only the IR crate.
5. **Full parity:** sqlite + postgres matrix green; shadow-strict clean where the query path is touched (`FERRO_SHADOW_RUNTIME=1 FERRO_SHADOW_RUNTIME_STRICT=1` — both flags).

## Build order (for the plan)

F-1 immutable chaining → F-2 validation → F-3/F-5 typed proxies + injection/operator/`col()` removal + `ty` gate → F-4 Rust IR collapse (widest mechanical change, strongest model) → docs.

## User-observable breaking changes (all `!`, land in 0.14.0)

- **Immutable chaining:** `q2 = q1.where(...)` no longer aliases/mutates `q1` (code relying on aliasing was almost certainly already buggy).
- **Operator predicate style removed:** `User.age == x` no longer builds a predicate (class attributes are no longer `FieldProxy`).
- **`col()` removed.**
- **`order_by(Model.field)` removed:** use `order_by(lambda u: u.field)` or `order_by("field")`.
- **`where()` no longer accepts a raw `QueryNode`** (only predicate callables).
