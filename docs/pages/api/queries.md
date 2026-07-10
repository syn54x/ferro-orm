# Queries

`Model.where(...)` and `Model.select()` return a `Query` — an immutable, chainable builder that executes when awaited via `all()`, `first()`, `count()`, `exists()`, `update()`, or `delete()`. Predicates are lambda-only (`User.where(lambda user: user.age >= 18)`) — a fresh `QueryProxy` validates column names against the model at build time.

`where()` and `order_by()` lambdas may **traverse** a forward-FK relation (`lambda t: t.account.ledger_id == 1`): each hop renders one INNER join, deduplicated by relation path (ADR-0006). `join()` forces a join on a relation path (a bare `join()` is an existence filter on a nullable relation), and `left_join()` marks the whole path LEFT to keep relation-less rows. See the [Querying Across Relationships](../guide/queries.md#querying-across-relationships) guide for worked examples.

::: ferro.query.builder.Query

::: ferro.query.QueryProxy

::: ferro.query.Predicate
