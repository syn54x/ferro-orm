# Pagination

Ferro supports the two standard pagination strategies: offset pagination with `limit()` / `offset()`, and keyset (cursor) pagination with a `where` filter on the sort key. This page shows both and when to pick each.

## Offset Pagination

The simplest approach: order the rows, skip `(page - 1) * per_page`, take `per_page`.

```python
--8<-- "docs/examples/pagination.py:offset"
```

Offset pagination is easy to implement and lets users jump to an arbitrary page number. It has two well-known costs:

- **Drift.** If rows are inserted or deleted between requests, page boundaries shift — a user paging through results can see duplicates or miss rows.
- **Deep offsets are expensive.** The database still scans and discards every skipped row, so `OFFSET 100000` does real work before returning anything. Latency grows with page depth.

For small datasets and admin-style page numbers, neither matters much. For feeds and large tables, use keyset pagination instead.

## Keyset (Cursor) Pagination

Instead of skipping rows, remember the last value seen and filter past it:

```python
--8<-- "docs/examples/pagination.py:keyset"
```

The client passes back the `id` of the last item it received (the cursor); the next query is a plain indexed range scan (`WHERE id > ? ORDER BY id LIMIT ?`). This makes keyset pagination:

- **Stable** — inserts and deletes elsewhere in the table don't shift the window, so no duplicates or gaps.
- **Fast at any depth** — page 1 and page 10,000 cost the same, since the database seeks directly to the cursor instead of scanning skipped rows.

The trade-off: clients can only walk forward (or backward) from a cursor — there is no "jump to page 57".

## Using It in an API

A keyset endpoint returns the items plus the cursor for the next request:

=== "Assignment"

    ```python
    from fastapi import FastAPI, Query

    from ferro import Field, Model


    class Article(Model):
        id: int | None = Field(default=None, primary_key=True)
        title: str


    app = FastAPI()


    @app.get("/articles")
    async def list_articles(
        cursor: int | None = Query(None),
        limit: int = Query(20, ge=1, le=100),
    ):
        query = Article.select() if cursor is None else Article.where(lambda t: t.id > cursor)
        items = await query.order_by(Article.id).limit(limit).all()
        return {
            "items": items,
            "next_cursor": items[-1].id if items else None,
            "has_more": len(items) == limit,
        }
    ```

=== "Annotated"

    ```python
    from typing import Annotated

    from fastapi import FastAPI, Query

    from ferro import Field, Model


    class Article(Model):
        id: Annotated[int | None, Field(default=None, primary_key=True)]
        title: str


    app = FastAPI()


    @app.get("/articles")
    async def list_articles(
        cursor: int | None = Query(None),
        limit: int = Query(20, ge=1, le=100),
    ):
        query = Article.select() if cursor is None else Article.where(lambda t: t.id > cursor)
        items = await query.order_by(Article.id).limit(limit).all()
        return {
            "items": items,
            "next_cursor": items[-1].id if items else None,
            "has_more": len(items) == limit,
        }
    ```

Clients call `/articles`, then `/articles?cursor=<next_cursor>` until `has_more` is false. Ferro models are Pydantic models, so FastAPI serializes them directly.

## Tips

- **Always `order_by`.** Without an explicit ordering, the database returns rows in whatever order it likes, and `limit`/`offset` windows become non-deterministic. Order by a unique column (or end the ordering with one) so ties can't straddle a page boundary.
- **Index your sort columns.** Both strategies turn into an ordered scan over the sort key. Declare an index on non-primary-key sort columns, e.g. `created_at: datetime = Field(index=True)`. Primary keys are already indexed.
- **Cap page sizes.** Enforce a maximum `limit` at the API boundary (as the FastAPI example does with `le=100`) so a single request can't ask for the whole table.

## Choosing a Strategy

| | Offset | Keyset (cursor) |
|---|---|---|
| Jump to arbitrary page | Yes | No |
| Stable under concurrent writes | No (drift) | Yes |
| Cost of deep pages | Grows with depth | Constant |
| Implementation effort | Trivial | Small (track a cursor) |
| Best for | Small tables, admin UIs, page numbers | Feeds, infinite scroll, large tables, APIs |

## See Also

- [Queries guide](../guide/queries.md) — filtering, ordering, `limit` and `offset`
- [Queries API](../api/queries.md) — the full `Query` builder reference
