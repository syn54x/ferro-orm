# Mutations

Creating, updating, and deleting records. All mutations are executed by the Rust engine, and all of them participate in an active [transaction](transactions.md) automatically.

Ferro's write surface is three verbs with three distinct intents — none of them ever silently does more work than you asked for:

| Verb | Intent | On a primary-key / unique conflict |
| --- | --- | --- |
| `Model.create(**fields)` | Insert a new row | Raises [`UniqueViolationError`](../api/exceptions.md) |
| `instance.save()` | Persist *this* instance | INSERT if never persisted, otherwise UPDATE by primary key |
| `Model.upsert(**fields)` | Insert **or** replace by primary key | Updates the existing row |

## Creating Records

### create

`Model.create(**fields)` validates, inserts, and returns the persisted instance in one call. For inserting many rows, `Model.bulk_create(instances)` batches them into a single statement and returns the inserted count:

```python
--8<-- "docs/examples/quickstart.py:create"
```

`create()` is a plain INSERT — it never updates an existing row. A duplicate primary key or unique value raises [`UniqueViolationError`](../api/exceptions.md), and the existing row is left untouched:

```python
--8<-- "docs/examples/mutations.py:create-strict"
```

Reach for [`upsert()`](#upsert) when you want insert-or-update semantics.

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

## Saving: INSERT or UPDATE

Every instance is either **transient** (constructed with `Model(...)` and never persisted) or **persisted** (fetched from the database, or successfully saved). `save()` uses that state to decide what to do:

- a transient instance is **INSERTed** — a duplicate primary key or unique value raises `UniqueViolationError`, exactly like `create()`;
- a persisted instance is **UPDATEd** by primary key (`UPDATE ... WHERE pk = ?`).

```python
--8<-- "docs/examples/mutations.py:save-insert-update"
```

`instance.delete()` returns the instance to transient, so a subsequent `save()` INSERTs a new row. `refresh()` keeps it persisted.

If the row behind a persisted instance no longer exists — deleted by another writer, or you mutated the primary-key field before saving — the UPDATE matches nothing and `save()` raises `ModelDoesNotExist` rather than silently resurrecting the row.

!!! note "Copies share persistence state"
    `model_copy()` copies persistence state: saving a copy of a persisted instance UPDATEs the **same row**. To clone a row, construct a fresh instance instead. See [Identity Map](../concepts/identity-map.md) for how instances are tracked per connection.

## Upsert

`Model.upsert(**fields)` inserts the row, or updates the existing row when the **primary key** conflicts:

```python
--8<-- "docs/examples/mutations.py:upsert"
```

Things to know:

- The conflict target is the primary key only. A conflict on some *other* unique column still raises `UniqueViolationError`.
- The whole row is written on update — fields you leave unset are written with their defaults, they do not preserve the stored values (see the `name` field above).
- With an autoincrement primary key left unset there is nothing to conflict on, so the call degrades to a plain INSERT.
- `upsert()` is sugar for the primitive `instance.save(on_conflict="update")`, which applies insert-or-update semantics to an instance you already hold, regardless of its persistence state.

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
    Both helpers are a read followed by a write, not a single atomic upsert. Under concurrent writers, two processes can race past the lookup; with a unique constraint on the filter columns the loser's INSERT raises [`UniqueViolationError`](../api/exceptions.md) — catch it and retry the get. For an atomic insert-or-update keyed on the primary key, use [`upsert()`](#upsert).

## Batch Updates

Update many rows in one statement — no instances are loaded — and delete the same way. `update(**values)` and `delete()` are query terminals that return the affected row count:

```python
--8<-- "docs/examples/quickstart.py:update-delete"
```

!!! warning "Batch updates bypass in-memory instances"
    A `where(...).update(...)` writes directly to the database. Instances you already hold (including identity-mapped ones) are **not** mutated — call `refresh()` on them if you need the new values.

### No limit/offset on mutations

Portable SQL has no `DELETE ... LIMIT` or `UPDATE ... LIMIT`, so calling `update()` or `delete()` on a query with `limit()` or `offset()` set raises `ValueError` instead of silently mutating every matching row. To mutate a bounded subset, fetch primary keys first and mutate by primary-key set:

```python
--8<-- "docs/examples/mutations.py:mutation-guard"
```

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

removed = await User.where(lambda user: user.archived == True).delete()  # noqa: E712
```

Deleting a parent row triggers the `on_delete` behavior of any foreign keys pointing at it — `CASCADE` by default. See [Delete Behavior](relationships.md#delete-behavior) before deleting rows with children.

## Handling Database Errors

Every database failure raises a typed exception from the DBAPI-shaped tree rooted at `FerroError` — you never need to match on driver message text. Constraint violations are subclasses of `IntegrityError`, and the original driver detail is preserved as attributes:

```python
--8<-- "docs/examples/mutations.py:handling-errors"
```

See the [Exceptions API reference](../api/exceptions.md) for the full hierarchy.

## Bulk Operations and the Identity Map

By default Ferro keeps a per-connection [identity map](../concepts/identity-map.md): loading the same primary key twice yields the same Python object, and `create()`/`save()` register instances in it.

`bulk_create()` is the deliberate exception — it serializes the given instances straight to the engine and **skips the identity map** for throughput. The instances you passed in are not registered (and auto-generated IDs are not written back onto them); re-query the rows when you need tracked instances.

```python
inserted = await User.bulk_create([User(name="a", age=1), User(name="b", age=2)])
fresh = await User.where(lambda user: user.name.in_(["a", "b"])).all()
```

## Not Yet Supported

!!! note "On the roadmap"
    Atomic update expressions — e.g. `update(views=Post.views + 1)` or `update(price=Product.price * 0.9)` — are **not yet implemented**; see the [Roadmap](../roadmap.md). In the meantime, load–modify–`save()` (last write wins), or use [raw SQL](raw-sql.md) for a truly atomic `UPDATE ... SET views = views + 1`.

## See Also

- [Queries](queries.md) — fetching and filtering data
- [Exceptions](../api/exceptions.md) — the typed error hierarchy
- [Transactions](transactions.md) — grouping mutations atomically
- [Relationships](relationships.md) — creating related records, cascade rules
- [Identity Map](../concepts/identity-map.md) — instance caching semantics
- [Testing](../howto/testing.md) — testing code that mutates data
