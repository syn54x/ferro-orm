# Mutations

Creating, updating, and deleting records. All mutations are executed by the Rust engine, and all of them participate in an active [transaction](transactions.md) automatically.

## Creating Records

### create

`Model.create(**fields)` validates, inserts, and returns the persisted instance in one call. For inserting many rows, `Model.bulk_create(instances)` batches them into a single statement and returns the inserted count:

```python
--8<-- "docs/examples/quickstart.py:create"
```

Pass related instances directly (`author=alice`) or set the shadow foreign-key column (`author_id=alice.id`) — see [Relationships](relationships.md). Pydantic validation runs when each instance is constructed, so invalid data raises *before* the database is touched.

### Defaults

Fields with `default` or `default_factory` fill themselves in:

=== "Assignment"

    ```python
    from datetime import datetime

    from ferro import Field, Model


    class Article(Model):
        id: int | None = Field(default=None, primary_key=True)
        title: str
        draft: bool = True
        created_at: datetime = Field(default_factory=datetime.now)
    ```

=== "Annotated"

    ```python
    from datetime import datetime
    from typing import Annotated

    from ferro import Field, Model


    class Article(Model):
        id: Annotated[int | None, Field(default=None, primary_key=True)]
        title: str
        draft: bool = True
        created_at: Annotated[datetime, Field(default_factory=datetime.now)]
    ```

```python
article = await Article.create(title="Hello")
# article.draft is True, article.created_at is set
```

## Get-or-Create

`get_or_create(defaults={...}, **filters)` looks up a row by exact-match filters and creates it when missing. It returns an `(instance, created)` tuple; `defaults` are applied **only** on the create path:

```python
--8<-- "docs/examples/mutations.py:get-or-create"
```

## Update-or-Create

`update_or_create(defaults={...}, **filters)` has the same shape, but when a match exists it applies `defaults` to the instance and saves it:

```python
--8<-- "docs/examples/mutations.py:update-or-create"
```

!!! note "Concurrency"
    Both helpers are a read followed by a write, not a single atomic upsert. Under concurrent writers, two processes can race past the lookup; a unique constraint on the filter columns turns that race into an integrity error you can handle.

## Updating

### Instance save()

Mutate fields on an instance and persist with `save()`:

```python
user = await User.get(1)
user.email = "new@example.com"
await user.save()
```

### Batch updates

Update many rows in one statement — no instances are loaded — and delete the same way. `update(**values)` and `delete()` are query terminals that return the affected row count:

```python
--8<-- "docs/examples/quickstart.py:update-delete"
```

!!! warning "Batch updates bypass in-memory instances"
    A `where(...).update(...)` writes directly to the database. Instances you already hold (including identity-mapped ones) are **not** mutated — call `refresh()` on them if you need the new values.

## Refreshing from the Database

`refresh()` reloads an instance from its primary key, discarding local state:

```python
--8<-- "docs/examples/mutations.py:refresh"
```

It raises `RuntimeError` if the instance has no primary key or the row no longer exists.

## Deleting

Delete a single instance, or batch-delete via a query:

```python
user = await User.get_or_none(42)
if user is not None:
    await user.delete()

removed = await User.where(lambda t: t.archived == True).delete()  # noqa: E712
```

Deleting a parent row triggers the `on_delete` behavior of any foreign keys pointing at it — `CASCADE` by default. See [Delete Behavior](relationships.md#delete-behavior) before deleting rows with children.

## Bulk Operations and the Identity Map

By default Ferro keeps a per-connection [identity map](../concepts/identity-map.md): loading the same primary key twice yields the same Python object, and `create()`/`save()` register instances in it.

`bulk_create()` is the deliberate exception — it serializes the given instances straight to the engine and **skips the identity map** for throughput. The instances you passed in are not registered (and auto-generated IDs are not written back onto them); re-query the rows when you need tracked instances.

```python
inserted = await User.bulk_create([User(name="a", age=1), User(name="b", age=2)])
fresh = await User.where(lambda t: t.name.in_(["a", "b"])).all()
```

## Not Yet Supported

!!! note "On the roadmap"
    Atomic update expressions — e.g. `update(views=Post.views + 1)` or `update(price=Product.price * 0.9)` — are **not yet implemented**; see the [Roadmap](../roadmap.md). In the meantime, load–modify–`save()` (last write wins), or use [raw SQL](raw-sql.md) for a truly atomic `UPDATE ... SET views = views + 1`.

## See Also

- [Queries](queries.md) — fetching and filtering data
- [Transactions](transactions.md) — grouping mutations atomically
- [Relationships](relationships.md) — creating related records, cascade rules
- [Identity Map](../concepts/identity-map.md) — instance caching semantics
- [Testing](../howto/testing.md) — testing code that mutates data
