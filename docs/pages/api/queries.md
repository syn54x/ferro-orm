# Queries

`Model.where(...)` and `Model.select()` return a `Query` — an immutable, chainable builder that executes when awaited via `all()`, `first()`, `count()`, `exists()`, `update()`, or `delete()`. Predicates are lambda-only (`User.where(lambda user: user.age >= 18)`) — a fresh `QueryProxy` validates column names against the model at build time.

`where()` and `order_by()` lambdas may **traverse** a forward-FK relation (`lambda t: t.account.ledger_id == 1`): each hop renders one INNER join, deduplicated by relation path (ADR-0006). `join()` forces a join on a relation path (a bare `join()` is an existence filter on a nullable relation), and `left_join()` marks the whole path LEFT to keep relation-less rows. See the [Querying Across Relationships](../guide/queries.md#querying-across-relationships) guide for worked examples.

## `select()` overloads

`select()` has three forms, resolved at build time:

| Call | Returns | Result of `.all()` |
| :--- | :--- | :--- |
| `Model.select()` | `Query[Model]` | `list[Model]` — the full query, unchanged. |
| `Model.select(lambda t: (t.id, t.amount))` | `ProjectedQuery[Model]` | `Rows[Row]` — projected records (single-field form: `select(lambda t: t.amount)`). |
| `Model.select("id", "amount")` | `ProjectedQuery[Model]` | `Rows[Row]` — `order_by()`'s string contract: root columns only, never mixed with a lambda. |

A `ProjectedQuery` composes like any `Query` (`where()` with traversal, `order_by()` by unselected columns, `limit()`/`offset()`, `first()`, `count()`, `exists()`); `update()`/`delete()` and a second `select()` raise at build time. See [Selecting a Column Subset](../guide/queries.md#selecting-a-column-subset) for worked examples and the complete-instance invariant behind the `Row` result shape.

::: ferro.query.builder.Query

::: ferro.query.builder.ProjectedQuery

::: ferro.query.Row

::: ferro.query.Rows

::: ferro.query.QueryProxy

::: ferro.query.Predicate

::: ferro.query.RowSelector
