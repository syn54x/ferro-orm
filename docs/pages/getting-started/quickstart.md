# Quickstart

In about 10 minutes you'll build a small blog backend: two related models (`Author` and `Post`), an in-memory SQLite database, and every core Ferro operation — create, query, traverse relationships, update, delete, and transactions.

Every code block on this page comes from one runnable script, shown in full at the [bottom of the page](#complete-script). Follow along in a file of your own, or just run the script.

## Define Your Models

Ferro supports two equivalent field-declaration styles — options on the assignment side, or inside `typing.Annotated`. Every model example in these docs shows both; pick one and stay consistent in your project.

=== "Assignment"

    ```python
    --8<-- "docs/examples/quickstart.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/quickstart_annotated.py:models"
    ```

A Ferro model is a Pydantic model — annotated fields become columns, and defaults work exactly as in Pydantic:

- `Field(default=None, primary_key=True)` marks `id` as the primary key. It's `int | None` because the database assigns it on insert.
- `Field(unique=True)` adds a unique constraint; `default_factory=datetime.now` gives each post a creation timestamp.
- `Annotated[Author, ForeignKey(related_name="posts")]` declares the many-to-one side: each `Post` stores an `author_id` column pointing at an `Author`.
- `Relation[list["Post"]] = BackRef()` is the reverse side: `author.posts` becomes a chainable query for that author's posts. `related_name="posts"` is what links the two.

## Connect

```python
--8<-- "docs/examples/quickstart.py:connect"
```

`connect()` takes a database URL. `sqlite::memory:` gives you a throwaway in-memory database — perfect for this tutorial and for tests. For a file-backed database use `sqlite:app.db?mode=rwc` (`rwc` = read/write/create), or a `postgres://...` URL for PostgreSQL.

`auto_migrate=True` creates tables for every registered model on connect. It's great for development; for production schemas, use [Alembic migrations](../guide/migrations.md).

## Create Data

```python
--8<-- "docs/examples/quickstart.py:create"
```

- `Model.create(...)` validates the data, inserts one row, and returns the instance with its database-assigned `id` populated. Notice you can pass a model instance (`author=alice`) for the foreign key.
- `Model.bulk_create([...])` inserts many rows in a single statement — use it whenever you're loading more than a handful of rows. Here we set `author_id` directly instead of passing the instance.

## Query

```python
--8<-- "docs/examples/quickstart.py:query"
```

- `Post.get(pk)` fetches one row by primary key.
- `where(...)` filters, `order_by(...)` sorts, `limit(...)` slices — and nothing touches the database until a terminal like `.all()`, `.first()`, `.count()`, or `.exists()` runs the query.
- `lambda t: t.published == True` is a lambda predicate — the officially recommended query style. Two other styles exist for compatibility; see [Queries](../guide/queries.md#predicate-styles) for the comparison.

!!! note "What happened"
    Thanks to Ferro's identity map, `Post.get(post.id)` returns the *same Python object* as the `post` you created earlier — not a duplicate copy. One row, one instance.

## Work with Relationships

```python
--8<-- "docs/examples/quickstart.py:relationships"
```

Two directions, two idioms:

- **Forward** (`post.author`): awaiting the foreign key attribute loads the related `Author`.
- **Reverse** (`author.posts`): the `BackRef` is a query, so you can chain `.where()`, `.order_by()`, and friends before awaiting it.

## Update & Delete

```python
--8<-- "docs/examples/quickstart.py:update-delete"
```

- For a single instance: mutate attributes, then `await post.save()`.
- For many rows: chain `.update(field=value)` or `.delete()` onto a `where()` query. Both return the number of affected rows.

## Wrap It in a Transaction

```python
--8<-- "docs/examples/quickstart.py:transaction"
```

Everything inside `async with transaction():` commits together when the block exits cleanly — and rolls back entirely if it raises. Use it whenever multiple writes must succeed or fail as one. More in [Transactions](../guide/transactions.md).

## Complete Script

The whole tutorial as one runnable file — it lives in the repo at `docs/examples/quickstart.py`:

```python
--8<-- "docs/examples/quickstart.py"
```

## What's Next

- [Next Steps](next-steps.md) — pick a path based on what you're building
- [Models & Fields](../guide/models-and-fields.md) — every field type and constraint
- [Queries](../guide/queries.md) — the lambda predicate style in depth, ordering, slicing, terminals
- [Relationships](../guide/relationships.md) — foreign keys, back-references, many-to-many
