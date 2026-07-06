# Queries

`Model.where(...)` and `Model.select()` return a `Query` — an immutable, chainable builder that executes when awaited via `all()`, `first()`, `count()`, `exists()`, `update()`, or `delete()`. Predicates are lambda-only (`User.where(lambda user: user.age >= 18)`) — a fresh `QueryProxy` validates column names against the model at build time.

::: ferro.query.builder.Query

::: ferro.query.QueryProxy

::: ferro.query.Predicate
