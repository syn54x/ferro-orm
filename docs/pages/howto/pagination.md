# How to paginate query results

This page splits a query's results into pages two ways: by page number with `limit()` and `offset()`, and by position with `after()` and `before()`. Use page numbers when users jump to page 7 of a small table; use positions for feeds, infinite scroll, APIs and large tables.

The examples page through seven articles, newest first. Declare the model, connect, and add the rows inside a session:

=== "Assignment"

    ```python
    --8<-- "docs/examples/pagination.py:model"
    --8<-- "docs/examples/pagination.py:session"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/pagination_annotated.py:model"
    --8<-- "docs/examples/pagination_annotated.py:session"
    ```

`published_on` carries an index because both kinds of paging read the table in `published_on` order. The blocks below run inside this session; [Sessions](../guide/connections.md#sessions) explains why every call needs one.

## Pages by number with `offset()`

Order the rows, skip `(page - 1) * per_page` of them, and take `per_page`:

```python
--8<-- "docs/examples/pagination.py:offset"
```

Each call reads one page. Page 2, three to a page, holds the fourth to sixth newest articles:

```python
--8<-- "docs/examples/pagination.py:offset-call"
```

Two articles share 12 January, so the second `order_by()` on `id` decides which comes first. Without it the database may return tied rows in either order, and a row can move from one page to the next between two requests.

Offset paging has two costs:

- **Drift.** A row added or deleted between two requests shifts every later page by one, so the reader sees a row twice or misses one.
- **Deep pages are slow.** The database reads and discards every skipped row, so `offset(100_000)` reads 100,000 rows before it returns any.

## Keyset (cursor) pagination

[`after()`][ferro.query.builder.Query.after] starts a page just after a row you already have. Build the ordered query once, read the first page, then pass its last row to `after()`:

```python
--8<-- "docs/examples/pagination.py:after"
```

The second page starts with Keyset paging in practice, the row after Soft deletes and unique keys. The database seeks straight to that position, so page 1,000 costs what page 2 costs, and rows added or deleted elsewhere do not shift the page.

[`before()`][ferro.query.builder.Query.before] goes the other way. Pass it the first row of a page to get the page before it, still newest first:

```python
--8<-- "docs/examples/pagination.py:before"
```

`previous_page` holds the same three articles as `first_page`. The order keys must end with the primary key, so that every row has exactly one position; `after()` and `before()` raise `ValueError` when it is missing. [Paging with `after()` and `before()`](../guide/queries.md#paging-with-after-and-before) lists the other rules.

## A paginated API endpoint

An API hands the client a cursor: a string that encodes the last row's position, which the client sends back for the next page. [`position_of()`][ferro.query.builder.Query.position_of] reads a row's position as a tuple of its order-key values, and `after()` accepts that tuple in place of the row:

```python
--8<-- "docs/examples/pagination.py:cursor"
```

`article_page()` asks for one row more than the page holds. If that row comes back, there is a next page and `next_cursor` points at the page's last row; if it does not, `next_cursor` is `None`, even when the last page is exactly full. Three calls walk all seven articles:

```python
--8<-- "docs/examples/pagination.py:cursor-calls"
```

The third page holds the one remaining article, and its `next_cursor` is `None`.

A FastAPI endpoint opens a session for the request and returns `article_page()`'s result. Ferro models are Pydantic models, so FastAPI serializes the articles directly:

```python {compile-only="needs FastAPI, which the docs test environment does not install; article_page() runs in docs/examples/pagination.py"}
from fastapi import FastAPI, Query

from ferro import engines
from myapp.articles import article_page

app = FastAPI()


@app.get("/articles")
async def list_articles(
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=100),
):
    async with engines.session():
        return await article_page(cursor=cursor, limit=limit)
```

Clients call `/articles`, then `/articles?cursor=<next_cursor>` until `next_cursor` is `null`. `le=100` caps the page size, so one request cannot ask for the whole table. [A session per request](../guide/connections.md#a-session-per-request) shows other ways to open the session.

## See also

- [Paging with `after()` and `before()`](../guide/queries.md#paging-with-after-and-before): the rules for positions, `None` order keys and related columns.
- [`Query.after()`][ferro.query.builder.Query.after], [`Query.before()`][ferro.query.builder.Query.before], [`Query.limit()`][ferro.query.builder.Query.limit] and [`Query.offset()`][ferro.query.builder.Query.offset] in the API reference.
