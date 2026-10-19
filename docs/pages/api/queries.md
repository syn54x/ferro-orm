# Query

`Model.where(...)` and `Model.select()` return a `Query`: an immutable, chainable builder that runs when you await `all()`, `first()`, `count()`, `exists()`, `update()` or `delete()`. Predicates are lambdas over a `QueryProxy`, which checks every column name against the model when the query is built:

```python {test=run prelude=reference}
popular = await Post.where(lambda post: post.views > 1000).order_by("title").all()
assert [post.title for post in popular] == ["Notes on the Analytical Engine"]
```

The query ran once, at `all()`, and returned the one post with more than 1,000 views.

The [Queries guide](../guide/queries.md) covers each feature with worked examples:

- `~` negates any predicate, rendering SQL `NOT (...)`: [Negation and NULL values](../guide/queries.md#negation-and-null-values).
- A lambda that follows a foreign key adds one `INNER JOIN` per relation path; `left_join()` keeps rows without the relation: [Querying across relationships](../guide/queries.md#querying-across-relationships).
- A `BackRef` or many-to-many relation in a predicate takes one verb, `.exists()`, rendered as a correlated `EXISTS`: [Existence tests](../guide/queries.md#existence-tests-on-reverse-many-to-many-relations).
- `include()` returns each result with a forward relation loaded, so reading it needs no `await`: [Populating relations with `include()`](../guide/queries.md#populating-relations-with-include).
- `nulls=` on `order_by()` defaults to `NULLS LAST` on every backend: [Ordering, limit and offset](../guide/queries.md#ordering-limit-offset).

## `select()` overloads

`select()` has four forms, resolved when the query is built:

| Call | Returns | Result of `.all()` |
| :--- | :--- | :--- |
| `Post.select()` | `Query[Post]` | `list[Post]`: the full query, unchanged. |
| `Post.select(lambda post: (post.id, post.author.name))` | `ProjectedQuery[Post]` | `Rows[Row]`: one record per row; a field may follow forward foreign keys at any depth and takes the leaf column's name (one field: `select(lambda post: post.views)`). |
| `Post.select(lambda post: {"author_email": post.author.email})` | `ProjectedQuery[Post]` | `Rows[Row]`: the dict keys name the output fields; the values are field references or aggregates. |
| `Post.select("id", "views")` | `ProjectedQuery[Post]` | `Rows[Row]`: root columns only, never a traversal, never mixed with a lambda. |

A `ProjectedQuery` chains like a `Query` (`where()`, `order_by()`, `limit()`, `offset()`, `first()`, `count()`, `exists()`). `update()`, `delete()`, a second `select()`, two fields with one output name and a nested selector raise when the query is built. See [Selecting a column subset](../guide/queries.md#selecting-a-column-subset).

## Aggregates and grouped queries

`count()`, `sum()`, `avg()`, `min()` and `max()` on a field build aggregates for the dict selector, also across a relation (`post.author.id.count()`). An aggregate-only projection returns one record; adding plain fields groups by them. Result types are the same on every backend:

| Aggregate | Takes | Returns | On no rows |
| :--- | :--- | :--- | :--- |
| `count()` | any column | `int` | `0` |
| `sum()` | a numeric column | the column's type | `None` |
| `avg()` | a numeric column | `float` for `int` and `float`, `Decimal` for `Decimal` | `None` |
| `min()`, `max()` | a numeric, text or date/time column | the column's type | `None` |

See [Aggregations and grouped queries](../guide/aggregations.md).

## Builders

::: ferro.query.Query
    options:
      # add, remove and clear work only on a many-to-many relation; they are
      # documented on ferro.Relation.
      filters: ["!^_", "!^(add|remove|clear)$"]

::: ferro.query.ProjectedQuery

## Predicates

::: ferro.query.QueryProxy

::: ferro.query.Predicate

::: ferro.query.FieldProxy

::: ferro.query.RelationProxy

::: ferro.query.QueryNode

::: ferro.query.ValueExpr

## Projections

::: ferro.Row

::: ferro.Rows

::: ferro.query.RowSelector

::: ferro.query.AggregateExpr

::: ferro.now
