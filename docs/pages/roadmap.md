# Roadmap

This page lists the features Ferro does not have yet, each with the workaround to use today. A filter on grouped results is the first gap; with the `Author` and `Post` models below, you filter the groups in Python after `all()`:

=== "Assignment"

    ```python
    --8<-- "docs/examples/faq.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/faq_annotated.py:models"
    ```

Each block below runs inside `async with engines.session():`; see [Connections › Sessions](guide/connections.md#sessions).

## Query features

### `having()`

Post-aggregation filtering for [grouped queries](guide/aggregations.md) does not exist, and `where()` rejects an aggregate predicate. Until `having()` ships ([tracked here](https://github.com/syn54x/ferro-orm/issues/291)), filter the groups in Python:

```python {test=run prelude=reference}
from ferro import engines

async with engines.session():
    rows = await Post.select(
        lambda post: {"author_id": post.author_id, "total": post.views.sum()}
    ).all()
    busy = [row.author_id for row in rows if row.total > 1000]
    assert busy == [1]
```

The query sums views per author, and the list comprehension keeps the groups above 1000.

### Typed record fields

A projected `Row` types its fields as `Any` on access, so `row.total` above does not check as `int | None`. Inferring a type per field from the selector is [tracked here](https://github.com/syn54x/ferro-orm/issues/290).

### Reverse and many-to-many population

[`include()`](guide/queries.md#populating-relations-with-include) populates forward foreign keys in one statement. Populating `BackRef` collections and many-to-many sets is not built: it needs a separate mechanism, a batched second query stitched onto the results. Until then, each awaited collection is its own query:

```python {test=run prelude=reference}
authors = await Author.all()
counts = [len(await author.posts.all()) for author in authors]
assert counts == [1, 1]
```

The comprehension issues one query per author. *Filtering* on reverse or many-to-many membership already works; see [existence tests](guide/queries.md#existence-tests-on-reverse-many-to-many-relations).

### Cross-scope correlation in existence tests

An inner lambda cannot compare its column to the outer scope. Ferro rejects the comparison when the query is built instead of rendering it wrongly:

```python {test=run prelude=reference}
try:
    Author.where(
        lambda author: author.posts.exists(lambda post: post.views > author.id)
    )
except TypeError as error:
    assert "cross-scope correlation" in str(error)
else:
    raise AssertionError("expected TypeError")
```

The `TypeError` names the comparison and says to compare against a value. Column-to-column correlation is [tracked here](https://github.com/syn54x/ferro-orm/issues/309).

### `ilike()`

Case-insensitive pattern matching has no operator, and no issue tracks it yet. Store a lowercased copy of the value and match it with `like()`.

### `.concat()`, `*` and `/` in update recipes

An update recipe supports `+`, `-`, `now` and Postgres `.merge()`, but not list `.concat()`, `*` or `/`. No issue tracks them yet. Load the row, change it and `save()` it, or use [raw SQL](guide/raw-sql.md):

```python {test=run prelude=reference}
post = await Post.get(1)
post.views = post.views * 2
await post.save()
assert (await Post.get(1)).views == 3686
```

The multiplication happens in Python, so the read and the write are two statements. See [Mutations](guide/mutations.md#expression-updates) for the recipes that run in the database.

## Connections

### `disconnect()`

There is no graceful pool shutdown for an application's shutdown hook; pools are cleaned up at process exit. No issue tracks it yet. In a test suite, `ferro.reset_engine()` tears the pools down.

### Health checks

There is no `check_connection()` probe for readiness endpoints, and no issue tracks one yet. Run a trivial query and catch the failure:

```python {test=run prelude=reference}
from ferro import fetch_one

row = await fetch_one("SELECT 1 AS ok")
assert row == {"ok": 1}
```

A readiness endpoint treats an exception from this query as unhealthy.

### Richer pool configuration

`PoolConfig` has `max_connections`, `min_connections` and `settings_delivery`. Acquire timeouts, idle timeouts and a maximum connection lifetime are future work, and no issue tracks them yet. See [Connections](guide/connections.md#pool-size).

## Influencing priorities

None of this has a promised schedule. If a gap blocks you, say so on the [issue tracker](https://github.com/syn54x/ferro-orm/issues): a concrete use case is the strongest signal, and [contributions](contributing.md) are welcome.
