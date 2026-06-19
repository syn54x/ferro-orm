# Typed Query Predicates

Ferro's query DSL accepts three predicate styles on `Model.where`, `Query.where`, and `Relation.where`. They run on the same code path and can be mixed in the same chain, but they are not equal: **lambda predicates are the officially recommended style**, `col()` is the type-safe escape hatch, and the operator style is slated for deprecation.

## Why Three Predicate Styles Exist

Ferro's metaclass replaces every model field with a `FieldProxy` at class-creation time, so `User.archived` is a `FieldProxy` at runtime — and `User.archived == False` builds a `QueryNode`, not a Python `bool`. Static type checkers (Pyright, `ty`, mypy, basedpyright) only see your Pydantic annotations, though, so they read `User.archived` as a `bool` and reject the same expression they would happily run.

The lambda and `col()` styles give you the runtime ergonomics back without forcing a model-annotation rewrite, a type-checker plugin, or any change to the existing operator path.

## The Styles

### 1. Lambda predicate (recommended)

```python
rows = await User.where(lambda t: t.archived == False).all()
rows = await User.where(
    lambda t: (t.role == "admin") & (t.active == True)
).all()
```

This is the officially recommended style — use it for all new code. The lambda receives a `QueryProxy` whose attribute access yields a fresh `FieldProxy` for each name, so `t.archived == False` is a `QueryNode` from the type checker's point of view as well as at runtime. The call site stays free of `# type: ignore` even when comparing booleans, integers, or any other value type, and the full operator surface is available: `.like()`, `.in_()`, `&`, `|`, `== None`, and shadow FK columns (`t.author_id`).

The proxy attribute type is currently `FieldProxy[Any]`, which is a deliberate scope decision (see [Scope boundaries](#scope-boundaries) below). Pyright still resolves the predicate's *return* type as `QueryNode` correctly.

### 2. `col()` wrapper

```python
from ferro.query import col

rows = await User.where(col(User.archived) == False).all()
```

`col()` is a runtime helper that returns a typed `FieldProxy[T]` for the same column while preserving the operator shape. It validates input with an `isinstance` guard (and raises `TypeError` if you accidentally hand it a literal). Reach for it when you want to keep the operator shape on an existing call site while staying type-safe.

### 3. Operator (legacy)

```python
rows = await User.where(User.id == 1).all()
rows = await User.where(User.email.like("%@example.com")).all()
```

!!! warning "Operator style is deprecated"
    The operator style is compatible today but on the Phase 7 removal track (next major release). It also fails static type checking: checkers read `User.id == 1` through your Pydantic annotations as a `bool`, while `where()` expects a `QueryNode | Predicate`. Use lambda predicates for new code, or `col()` when migrating existing operator-style call sites with minimal diff.

## When to Use Which

| Style | Use when |
|------|----------|
| Lambda | All new code — the official default. Fully type-checked, full operator surface. |
| `col()` | Migrating existing operator-style call sites with minimal diff while staying type-safe. |
| Operator | Legacy/untyped codebases only. Slated for deprecation; fails static type checking. |

All three are equally efficient at runtime — every one of them produces a `QueryNode` and appends it to the query's where clause.

## Combining Styles

You can mix all three on a single chain — useful mid-migration. They compose because they all funnel through the same dispatch in `Query.where`:

```python
from ferro.query import col

rows = await (
    User.where(lambda t: t.role == "admin")     # lambda (recommended)
    .where(col(User.archived) == False)         # col()
    .where(User.id == 1)                        # operator (legacy)
    .all()
)
```

`Relation.where` (used on `BackRef` collections) accepts the same three shapes:

```python
published = await author.posts.where(lambda t: t.published == True).all()
```

## What This Doesn't Change

- Your model annotations. `archived: bool = False` stays exactly as it is.
- The metaclass's `FieldProxy` injection. Class attribute access is unchanged.
- Pydantic schema generation, JSON schema output, or model validation.
- The Rust FFI bridge architecture (predicates now serialize through QueryIR envelopes).
- The operator-path runtime. Existing `Model.field == value` calls take the same code path they always have.

## Scope Boundaries

The current implementation deliberately stops short of:

- **Per-field types on the lambda proxy.** `t.archived` resolves to `FieldProxy[Any]`, not `FieldProxy[bool]`. Wiring per-field types through the proxy needs `@dataclass_transform` plumbing on the metaclass; that's future work.
- **A type-checker plugin.** Ferro stays plugin-free.
- **A kwargs-style or template-string predicate API.** Both have been considered; neither shipped here.

If `t.archived` resolving as `FieldProxy[Any]` ever bites you statically, drop back to `col(Model.archived) == ...` for that one comparison — that's exactly the role `col()` plays.

## Reference

- `ferro.query.col` — runtime-identity wrapper, raises `TypeError` for non-`FieldProxy` input.
- `ferro.query.QueryProxy` — attribute proxy passed to lambda predicates.
- `ferro.query.Predicate` — `Callable[[QueryProxy[TModel]], QueryNode]`, the type of any lambda predicate.
- `ferro.query.FieldProxy` — generic over the column's Python type (`FieldProxy[T]`).

## See Also

- [Queries Guide](../guide/queries.md)
- [Type Safety](type-safety.md)
