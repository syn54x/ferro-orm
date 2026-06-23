# Queries

`Model.where(...)` and `Model.select()` return a `Query` — an immutable, chainable builder that executes when awaited via `all()`, `first()`, `count()`, `exists()`, `update()`, or `delete()`. Predicates are lambda-first (`User.where(lambda t: t.age >= 18)`), `col()` is the compatibility bridge for operator-shaped predicates, and direct operator style is deprecated for `v0.14.0` removal. For migration steps, see [Migrating to v0.12.0](../howto/migrating-to-v0-12-0.md).

::: ferro.query.builder.Query

::: ferro.query.col

::: ferro.query.QueryProxy

::: ferro.query.Predicate
