# Typed Query Predicates

Ferro's query DSL accepts three predicate styles on `Model.where`, `Query.where`, and `Relation.where`. They are interchangeable, run on the same code path, and can be mixed freely in the same chain. Pick the one that reads best for the call site you're writing.

## Why this exists

Ferro's metaclass replaces every model field with a `FieldProxy` at class-creation time, so `User.archived` is a `FieldProxy` at runtime — and `User.archived == False` builds a `QueryNode`, not a Python `bool`. Static type checkers (Pyright, `ty`, mypy, basedpyright) only see your Pydantic annotations, though, so they read `User.archived` as a `bool` and reject the same expression they would happily run.

The two new predicate styles below give you the runtime ergonomics back without forcing a model-annotation rewrite, a type-checker plugin, or any change to the existing operator path.

## The three styles

### 1. Operator (the original)

```python
rows = await User.where(User.id == 1).all()
rows = await User.where(User.email.like("%@example.com")).all()
```

Works at runtime, always has, always will. Type checkers may flag boolean-column comparisons (`User.archived == False` resolves statically to `bool`) — when that bites, reach for one of the styles below.

### 2. `col()` wrapper

```python
from ferro.query import col

rows = await User.where(col(User.archived) == False).all()
```

`col()` is a runtime-identity helper that statically narrows its argument back to `FieldProxy[T]`. It does no work at runtime beyond an `isinstance` guard (and raises `TypeError` if you accidentally hand it a literal). Reach for it when a single attribute trips your type checker and you don't want to restructure the call site.

### 3. Lambda predicate

```python
rows = await User.where(lambda t: t.archived == False).all()
rows = await User.where(
    lambda t: (t.role == "admin") & (t.active == True)
).all()
```

The lambda receives a `QueryProxy` whose attribute access yields a fresh `FieldProxy` for each name — so `t.archived == False` is a `QueryNode` from the type checker's point of view as well as at runtime. This is the recommended style for new code: it keeps the call site free of `# type: ignore` even when comparing booleans, integers, or any other value type.

The proxy attribute type is currently `FieldProxy[Any]`, which is a deliberate scope decision (see [Scope boundaries](#scope-boundaries) below). Pyright still resolves the predicate's *return* type as `QueryNode` correctly.

## When to use which

| Style | Use when |
|------|----------|
| Operator | Existing code that already type-checks; quick filters where the value type isn't `bool`. |
| `col()` | One attribute on an existing chain trips your type checker and you want minimal diff. |
| Lambda | New code, especially boolean comparisons or compound predicates; preferred idiom. |

All three are equally efficient at runtime — every one of them produces a `QueryNode` and appends it to `where_clause`.

## Combining styles

You can mix all three on a single chain. They compose because they all funnel through the same dispatch in `Query.where`:

```python
rows = await (
    User.where(User.id == 1)                    # operator
    .where(col(User.archived) == False)         # col()
    .where(lambda t: t.role == "admin")         # lambda
    .all()
)
```

`Relation.where` (used on `BackRef` collections) accepts the same three shapes:

```python
published = await author.posts.where(lambda t: t.published == True).all()
```

## What this does not change

- Your model annotations. `archived: bool = False` stays exactly as it is.
- The metaclass's `FieldProxy` injection. Class attribute access is unchanged.
- Pydantic schema generation, JSON schema output, or model validation.
- The Rust FFI bridge or how `QueryNode`s are serialized for the engine.
- The operator-path runtime. Existing `Model.field == value` calls take the same code path they always have.

## Scope boundaries

The current implementation deliberately stops short of:

- **Per-field types on the lambda proxy.** `t.archived` resolves to `FieldProxy[Any]`, not `FieldProxy[bool]`. Wiring per-field types through the proxy needs `@dataclass_transform` plumbing on the metaclass; that's a future PR.
- **A type-checker plugin.** Ferro stays plugin-free.
- **A kwargs-style or template-string predicate API.** Both have been considered; neither shipped here.

If `t.archived` resolving as `FieldProxy[Any]` ever bites you statically, drop back to `col(Model.archived) == ...` for that one comparison — that's exactly the role `col()` plays.

## Reference

- `ferro.query.col` — runtime-identity wrapper, raises `TypeError` for non-`FieldProxy` input.
- `ferro.query.QueryProxy` — attribute proxy passed to lambda predicates.
- `ferro.query.Predicate` — `Callable[[QueryProxy[TModel]], QueryNode]`, the type of any lambda predicate.
- `ferro.query.FieldProxy` — generic over the column's Python type (`FieldProxy[T]`).

See the [Query API reference](../api/query.md) for full signatures.

## See Also

- [Queries Guide](../guide/queries.md)
- [Type Safety](type-safety.md)
- [Query API](../api/query.md)
