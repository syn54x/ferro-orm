# Queries

`Model.where(...)` and `Model.select()` return a `Query` — an immutable, chainable builder that executes when awaited via `all()`, `first()`, `count()`, `exists()`, `update()`, or `delete()`. Predicates are lambda-only (`User.where(lambda user: user.age >= 18)`) — a fresh `QueryProxy` validates column names against the model at build time.

Prefix `~` negates **any** predicate — leaf comparison or `&`/`|` compound — rendering as SQL `NOT (...)` over the condition it wraps (ADR-0008). It is the universal negation rule: there are no per-operator negative forms (`~t.role.in_([...])` is NOT IN, `~t.email.like(p)` is NOT LIKE), and double negation nests. Like SQL `NOT` and the `!=` operator, a negated comparison excludes rows where the compared column is `NULL` — see [Negation and NULL values](../guide/queries.md#negation-and-null-values).

`where()` and `order_by()` lambdas may **traverse** a forward-FK relation (`lambda t: t.account.ledger_id == 1`): each hop renders one INNER join, deduplicated by relation path (ADR-0006). `join()` forces a join on a relation path (a bare `join()` is an existence filter on a nullable relation), and `left_join()` marks the whole path LEFT to keep relation-less rows. Omitted `nulls=` on `order_by` means `NULLS LAST` on every backend; pass `nulls="first"`, `nulls="last"`, or `nulls="native"` (dialect default) to override — see [Ordering, Limit & Offset](../guide/queries.md#ordering-limit--offset). See the [Querying Across Relationships](../guide/queries.md#querying-across-relationships) guide for worked examples.

A reverse (`BackRef`) or many-to-many relation in a predicate supports exactly one verb — the **existence test** `t.rel.exists(inner_lambda=None)` (ADR-0007). It renders as a correlated `EXISTS` at every cardinality (never a join, so the result stays root-shaped and each matching root returns once), negates with `~`, and the optional inner lambda is a full ferro predicate over the related model (operators, `&`/`|`/`~`, forward traversal rendered inside the subquery, nested tests). Everything else on a reverse edge — column access, comparisons (including `!= None`), `in_` (including a query RHS), `join()`/`left_join()` — raises at build time naming `.exists()`; an inner lambda referencing any scope but its own parameter is likewise rejected ([#309](https://github.com/syn54x/ferro-orm/issues/309)). See [Existence Tests](../guide/queries.md#existence-tests-on-reverse-many-to-many-relations) for worked examples.

## `include()` and populated relations

`include(lambda t: t.account)` delivers each result with the relation **populated** (ADR-0008): access becomes a plain attribute holding the complete related instance — no await, no query — while unpopulated relations keep the awaitable contract. Include is the third orthogonal query axis (joins decide membership, projection decides shape, include decides attached data): it never changes which rows come back, `.all()` still returns `list[Model]`, and `count()`/`exists()` are unaffected. Paths populate whole (`include(lambda t: t.account.owner)` populates both hops); includes are cumulative, order-free, and idempotent; populated instances run the full session identity-map protocol, and a refresh keeps a population only while the row's FK still points at it.

Loud limits, at build time: forward-FK lambda paths only (a `BackRef`/M2M selector, a string, or a column selector raises `TypeError`); combining include with a projection raises `ValueError` in either chain order (one materialization plan per query; record results are flat — reach across a relation with traversed projection instead); `update()`/`delete()` on an included query raise `ValueError`. See [Populating Relations with include()](../guide/queries.md#populating-relations-with-include) for worked examples.

## `select()` overloads

`select()` has four forms, resolved at build time:

| Call | Returns | Result of `.all()` |
| :--- | :--- | :--- |
| `Model.select()` | `Query[Model]` | `list[Model]` — the full query, unchanged. |
| `Model.select(lambda t: (t.id, t.account.name))` | `ProjectedQuery[Model]` | `Rows[Row]` — projected records; fields may traverse forward-FK paths at any depth and take the bare leaf column name (single-field form: `select(lambda t: t.amount)`). |
| `Model.select(lambda t: {"owner_email": t.account.owner.email})` | `ProjectedQuery[Model]` | `Rows[Row]` — dict keys name the output fields (output aliases); values are field references or aggregate expressions. |
| `Model.select("id", "amount")` | `ProjectedQuery[Model]` | `Rows[Row]` — `order_by()`'s string contract: root columns only, never traversal, never mixed with a lambda. |

A `ProjectedQuery` composes like any `Query` (`where()` with traversal, `order_by()` by unselected columns, `limit()`/`offset()`, `first()`, `count()`, `exists()`); `update()`/`delete()`, a second `select()`, output-name collisions, and nested selector shapes raise at build time. Projection traversal narrows per ADR-0006 (INNER, one join per path, shared with `where()`/`order_by()`); `left_join()` keeps relation-less rows with their traversed fields decoded to `None`. See [Selecting a Column Subset](../guide/queries.md#selecting-a-column-subset) for worked examples and the complete-instance invariant behind the `Row` result shape.

## Aggregates and grouped queries

Five methods on column references build aggregate fields for the dict selector: `t.amount.count()` / `.sum()` / `.avg()` / `.min()` / `.max()` (traversal included: `t.account.balance.avg()`). Source families validate at build time — `sum`/`avg` take numeric columns, `min`/`max` orderable ones (numeric, text, date/time), `count` any column; enum/uuid/json/bool are rejected. Result types are a pinned cross-backend contract derived from the source column (`count → int`, `min`/`max` → source type, `sum` → source numeric type, `avg` → `float` for int/float and `Decimal` for Decimal); empty input passes SQL through verbatim (`None`, `count → 0`).

An aggregate-only projection collapses to one record (read with `first()`). Mixing plain fields in makes the query **grouped**: every plain field is a group key — `GROUP BY` is derived from the projection, never declared. On a projected query `order_by()` strings resolve output field names first, then root columns; the lambda form spells source expressions including aggregates (`order_by(lambda t: t.amount.sum(), "desc")`); on an aggregate projection every sort key must be a group key or an aggregate — anything else raises at build time, as do `count()`/`exists()` (ambiguous between rows and groups) and aggregate predicates in `where()` (post-aggregation filtering is `having()`, #291). See [Aggregations & Grouped Queries](../guide/aggregations.md) for worked examples.

::: ferro.query.builder.Query

::: ferro.query.builder.ProjectedQuery

::: ferro.query.Row

::: ferro.query.Rows

::: ferro.query.QueryProxy

::: ferro.query.Predicate

::: ferro.query.RowSelector

::: ferro.query.AggregateExpr
