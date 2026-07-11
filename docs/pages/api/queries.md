# Queries

`Model.where(...)` and `Model.select()` return a `Query` — an immutable, chainable builder that executes when awaited via `all()`, `first()`, `count()`, `exists()`, `update()`, or `delete()`. Predicates are lambda-only (`User.where(lambda user: user.age >= 18)`) — a fresh `QueryProxy` validates column names against the model at build time.

`where()` and `order_by()` lambdas may **traverse** a forward-FK relation (`lambda t: t.account.ledger_id == 1`): each hop renders one INNER join, deduplicated by relation path (ADR-0006). `join()` forces a join on a relation path (a bare `join()` is an existence filter on a nullable relation), and `left_join()` marks the whole path LEFT to keep relation-less rows. See the [Querying Across Relationships](../guide/queries.md#querying-across-relationships) guide for worked examples.

## `include()` and populated relations

`include(lambda t: t.account)` delivers each result with the relation **populated** (ADR-0008): access becomes a plain attribute holding the complete related instance — no await, no query — while unpopulated relations keep the awaitable contract. Include is the third orthogonal query axis (joins decide membership, projection decides shape, include decides attached data): it never changes which rows come back, `.all()` still returns `list[Model]`, and `count()`/`exists()` are unaffected. Paths populate whole (`include(lambda t: t.account.owner)` populates both hops); includes are cumulative, order-free, and idempotent; populated instances run the full session identity-map protocol, and a refresh keeps a population only while the row's FK still points at it.

Loud limits, at build time: forward-FK lambda paths only (a `BackRef`/M2M selector, a string, or a column selector raises `TypeError`); combining include with a projection raises `ValueError` in either chain order (one materialization plan per query, #282); `update()`/`delete()` on an included query raise `ValueError`. See [Populating Relations with include()](../guide/queries.md#populating-relations-with-include) for worked examples.

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
