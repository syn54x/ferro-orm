# Left join when the foreign key is nullable

Ferro never picks a join type from a foreign key's nullability. A `where()`,
`order_by()` or `select()` that follows a relation is an `INNER JOIN` at every
step, so rows without the relation drop out. `left_join()` on that path keeps
them, with `None` in the fields they cannot reach:

```python
rows = await (
    Article.left_join(lambda article: article.category)
    .select(lambda article: {"id": article.id, "category_name": article.category.name})
    .all()
)
```

## Why it holds

If nullability chose the join, changing `category_id: int` to
`category_id: int | None` would change what every existing query on that path
returns, with no edit to the query. With one rule, a query means the same
thing before and after a schema change, and a reader can tell the join from
the query alone. ADR-0006 (`docs/adr/0006-relation-traversal-inner-joins.md`)
records the decision; `docs/pages/guide/queries.md` documents it for filters,
ordering and projections.

## Revisit when

A query surface appears where the caller cannot add `left_join()` to the path
they traverse. Today every method that follows a relation chains with it.

## Asked by

- #259: "`feat` Query-time joins: filter/sort on related-model columns (PRD from Pinch)"
- #871: "A projection through a nullable forward relation drops the rows whose key is null"
