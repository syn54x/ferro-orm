# Typed Query Predicates

`Model.where`, `Query.where`, and `Relation.where` accept a single predicate
shape: a lambda that receives a `QueryProxy` and returns a `QueryNode`.

```python
rows = await User.where(lambda user: user.archived == False).all()
rows = await User.where(
    lambda user: (user.role == "admin") & (user.active == True)
).all()
```

## How It Works

The lambda receives a fresh `QueryProxy` for the model being queried.
Attribute access on the proxy is validated against the model's declared
columns (plus shadow `{fk}_id` columns) and returns a `FieldProxy`, so
`user.archived == False` builds a `QueryNode`. A misspelled column —
`user.archievd` — raises `AttributeError` naming the closest valid match and
listing every queryable column, at build time, before any query reaches the
database.

Name the lambda parameter after the model in lowercase singular (`user` for
`User`, `post` for `Post`) so predicates read like English. The full operator
surface is available: `==`, `!=`, `<`, `<=`, `>`, `>=`, `.like()`, `.in_()`,
`&`, `|`, `== None`, and shadow FK columns (`user.author_id`).

The proxy attribute type is `FieldProxy[Any]`: per-field static types for
bare lambda parameters need TypeScript-style mapped types, proposed for
Python in PEP 827 (draft, targeting 3.16) — adopted here when type checkers
support it. Pyright and `ty` still resolve the predicate's *return* type as
`QueryNode` correctly.

`Relation.where` (used on `BackRef` collections) accepts the same shape:

```python
published = await author.posts.where(lambda post: post.published == True).all()
```

## What This Doesn't Change

- Your model annotations. `archived: bool = False` stays exactly as it is.
- Pydantic schema generation, JSON schema output, or model validation.
- The Rust FFI bridge architecture (predicates serialize through QueryIR envelopes).

## Reference

- `ferro.query.QueryProxy` — validating attribute proxy passed to lambda predicates.
- `ferro.query.Predicate` — `Callable[[QueryProxy[TModel]], QueryNode]`, the type of any lambda predicate.
- `ferro.query.FieldProxy` — generic over the column's Python type (`FieldProxy[T]`); the mechanism a `QueryProxy` attribute access returns.

## See Also

- [Queries Guide](../guide/queries.md)
- [Type Safety](type-safety.md)
