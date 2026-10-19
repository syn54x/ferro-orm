# Quickstart

This tutorial builds a small blog backend on an in-memory SQLite database: two related models, `Author` and `Post`, and every core Ferro operation: create, query, traverse a relationship, update, delete and run a transaction.

Every code block comes from one runnable script, shown in full under [Complete script](#complete-script). To follow along in a file of your own, start with these imports:

```python
--8<-- "docs/examples/quickstart.py:imports"
```

The model block comes next at module level. Every block after it uses `await`, so it goes inside one `async def main()` that you run with `asyncio.run(main())`, in the order the page shows them; the script has that shape. You can also run the script as it is.

## Models

Ferro supports two equivalent field-declaration styles: options on the assignment side, or inside `typing.Annotated`. Every model example in these docs shows both, so pick one and stay consistent in your project. Declare the two models:

=== "Assignment"

    ```python
    --8<-- "docs/examples/quickstart.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/quickstart_annotated.py:models"
    ```

A Ferro model is a Pydantic model: annotated fields become columns, and defaults work as they do in Pydantic.

- `Field(default=None, primary_key=True)` marks `id` as the primary key. The type is `int | None` because the database assigns the value on insert.
- `Field(unique=True)` adds a unique constraint, and `default_factory` gives each post a creation timestamp in UTC.
- `Annotated[Author, ForeignKey(related_name="posts")]` declares the many-to-one side: each `Post` stores an `author_id` column that points at an `Author`.
- `Relation[list["Post"]] = BackRef()` declares the reverse side, so `author.posts` becomes a query for that author's posts. `related_name="posts"` links the two.

## Connect

Connect to a database, then open a session. Every ORM call runs inside `async with engines.session():`, and raises `No database route` outside it:

```python
--8<-- "docs/examples/quickstart.py:connect"
```

The new database has no authors yet, so the count is `0`. `connect()` takes a database URL: `sqlite::memory:` is a throwaway in-memory database, `sqlite:app.db?mode=rwc` is a file that is created if missing (`rwc` means read, write, create), and a `postgres://...` URL connects to PostgreSQL. `auto_migrate=True` creates a table for every registered model on connect. Use it in development and tests; for a database whose data you would mind losing, use [migrations](../guide/schema/migrations.md) (`ferro migrate new`, then `ferro migrate up`), which [Schema management](../guide/schema/overview.md) compares with auto-migrate.

The blocks that follow run inside this session. See [Sessions](../guide/connections.md#sessions) for named connections and request-scoped sessions.

## Create data

Create an author and a first post, then insert two more posts in one statement with `bulk_create()`:

```python
--8<-- "docs/examples/quickstart.py:create"
```

`create()` validates the data, inserts one row and returns the instance with its database-assigned `id`. Passing the instance (`author=alice`) sets the foreign key. A duplicate unique value raises [`UniqueViolationError`](../api/exceptions.md); `create()` never overwrites a row. `bulk_create()` writes the whole batch at once, so use it whenever you load more than a handful of rows. The table now holds three posts.

## Query

Fetch the first post by primary key:

```python
--8<-- "docs/examples/quickstart.py:get"
```

`get()` returned the same Python object as the `post` you created, because within a session one database row is one object. See [Identity map](../concepts/identity-map.md).

Filter, order and slice the posts:

```python
--8<-- "docs/examples/quickstart.py:filter"
```

`where()` takes a lambda predicate, the only predicate style it accepts; name the parameter after the model in lowercase singular. `order_by()` and `limit()` shape the result, and nothing touches the database until a terminal such as `.all()`, `.first()`, `.count()` or `.exists()` runs. See [Queries](../guide/queries.md#predicate-style) for the operators and how column names are checked when you build the query.

Count the posts and check for drafts:

```python
--8<-- "docs/examples/quickstart.py:aggregate"
```

There are three posts, and at least one is unpublished.

## Relationships

Load a post's author, then the author's published posts:

```python
--8<-- "docs/examples/quickstart.py:relationships"
```

Awaiting the foreign key (`post.author`) loads the related `Author`. The reverse side (`author.posts`) is a query, so you can chain `.where()` and `.order_by()` before awaiting it.

## Update and delete

Change one post with `save()`, publish every draft with `update()` and remove a post with `delete()`:

```python
--8<-- "docs/examples/quickstart.py:update-delete"
```

`save()` updates an instance that came from the database and inserts one that did not; see [Mutations](../guide/mutations.md#saving-insert-or-update). Chained `.update(field=value)` and `.delete()` act on every matching row and return the number of rows they changed, here `1` each.

## Transactions

Create an author and their post together:

```python
--8<-- "docs/examples/quickstart.py:transaction"
```

Everything inside `async with transaction():` commits when the block exits cleanly and rolls back if it raises. Use it whenever several writes must succeed or fail together. See [Transactions](../guide/transactions.md).

## Complete script

The whole tutorial as one runnable file, also at `docs/examples/quickstart.py` in the repository:

```python
--8<-- "docs/examples/quickstart.py"
```

Running it prints `quickstart example ran successfully`.

## Next steps

Pick a path by what you are building.

For an API with FastAPI, Starlette or similar:

1. [Models and fields](../guide/models-and-fields.md) covers field types, constraints and defaults.
2. [Relationships](../guide/relationships.md) covers foreign keys, back-references and many-to-many.
3. [Queries](../guide/queries.md) covers filtering, ordering and slicing.
4. [Pagination](../howto/pagination.md) shows offset and cursor pagination.
5. [Testing](../howto/testing.md) shows fast, isolated tests on in-memory SQLite.

For data processing and bulk loads:

1. [Mutations](../guide/mutations.md) covers `bulk_create` and bulk update and delete.
2. [Transactions](../guide/transactions.md) makes multi-step writes atomic.
3. [Queries](../guide/queries.md) shows how to filter in the database instead of in Python.

For shipping to production:

1. [Connections](../guide/connections.md) covers connection URLs and pool settings.
2. [Schema management](../guide/schema/overview.md) and [Deploying migrations](../guide/schema/deploying.md) cover evolving a production schema.
3. [CLI reference](../reference/cli.md) and [Configuration](../reference/configuration.md) list the `ferro` commands and settings.
4. [Multiple databases](../howto/multiple-databases.md) covers more than one database.

More recipes are under [How-to guides](../howto/testing.md), every public class and method is in the [API reference](../api/model.md), and [Releases on GitHub](https://github.com/syn54x/ferro-orm/releases) lists new versions.
