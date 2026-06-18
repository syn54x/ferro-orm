# Queries

`Model.where(...)` and `Model.select()` return a `Query` — an immutable, chainable builder that executes when awaited via `all()`, `first()`, `count()`, `exists()`, `update()`, or `delete()`. Predicates are written against the typed field proxies on the model class (`User.age >= 18`); `col()` is the untyped escape hatch for dynamic field names.

::: ferro.query.builder.Query

::: ferro.query.col

::: ferro.query.QueryProxy

::: ferro.query.Predicate
