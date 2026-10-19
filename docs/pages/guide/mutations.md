---
description: "Insert, update and delete rows with create(), save(), batch update() and delete(), and the insert-or-update helpers."
---

# Mutations

This guide shows how to insert, update and delete rows: `create()`, `save()`, batch `update()` and `delete()`, and the insert-or-update helpers.

Every call runs inside a session. Connect, open one, and create a customer:

```python
--8<-- "docs/examples/mutations.py:session"
```

`create()` returns the saved instance, with the primary key the database assigned and `plan` filled from its default. The blocks below run inside this session; see [Sessions](connections.md#sessions).

The examples use one model, a customer with a unique email:

=== "Assignment"

    ```python
    --8<-- "docs/examples/mutations.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/mutations_annotated.py:models"
    ```

Because `email` is unique, a second customer with the same email is refused.

Ferro has four ways to write a row, and each does only what its name says:

| Call | Intent | Behaviour |
| --- | --- | --- |
| `Customer.create(**fields)` | Insert a new row | Raises [`UniqueViolationError`](../api/exceptions.md#ferro.UniqueViolationError) on a duplicate primary key or unique value |
| `Customer.bulk_create(instances)` | Insert many new rows | Inserts the whole batch or none of it |
| `customer.save()` | Persist this instance | Inserts it if it was never saved, otherwise updates its row by primary key |
| `Customer.upsert(**fields)` | Insert or replace by primary key | Updates the existing row on a primary-key conflict |

## Creating rows

`create()` validates the fields, inserts the row and returns the saved instance. `bulk_create()` inserts a list of instances and returns how many it inserted:

```python
--8<-- "docs/examples/mutations.py:create"
```

The table now holds four customers. Fields you leave out take their defaults, so Carol's `plan` is `"free"`.

To create a row with a foreign key, see [Relationships](relationships.md). Pydantic validates each instance when it is built, so invalid data raises before Ferro sends anything to the database.

A batch can be any size. SQLite binds at most 32,766 parameters in one statement and PostgreSQL 65,535, so Ferro splits a large batch across as many `INSERT` statements as the database needs. The batch stays atomic: inside a [`transaction()`](transactions.md) block the transaction is the boundary, and outside one, a failing row (a unique violation late in the batch, say) leaves none of the batch inserted.

!!! note "`bulk_create()` skips the identity map"
    For speed, `bulk_create()` sends the instances straight to the database. It does not register them in the session's [identity map](../concepts/identity-map.md) and does not write generated IDs back onto them. Query the rows again when you need tracked instances:

    ```python
    --8<-- "docs/examples/mutations.py:bulk-identity"
    ```

    The instance passed to `bulk_create()` still has `id = None`; the one loaded back has the database's ID, `5`.

`create()` always inserts. A duplicate primary key or unique value raises `UniqueViolationError` and leaves the existing row as it was:

```python
--8<-- "docs/examples/mutations.py:create-strict"
```

Alice's row still holds the name `"Alice"`. Use [`upsert()`](#upsert) when you want insert-or-update instead.

## Saving: insert or update

An instance is either **transient** (built with `Customer(...)` and never saved) or **persisted** (loaded from the database, or saved). `save()` reads that state: it inserts a transient instance and updates a persisted one by primary key:

```python
--8<-- "docs/examples/mutations.py:save-insert-update"
```

The first `save()` inserted Frank and set his `id`; the second updated the same row, so there is still one Frank. A transient instance with a duplicate primary key or unique value raises `UniqueViolationError`, as `create()` does.

If the row behind a persisted instance is gone (another writer deleted it, or you changed the primary key before saving), the update matches nothing. `save()` then raises `ModelDoesNotExist` instead of inserting the row again:

```python
--8<-- "docs/examples/mutations.py:save-missing"
```

Frank stays deleted. `instance.delete()` makes an instance transient again, so a later `save()` on it inserts a new row.

!!! note "Copies share persistence state"
    `model_copy()` copies the persistence state too, so saving a copy of a persisted instance updates the same row. To clone a row, build a new instance.

### Partial updates with `only=` and `exclude=`

`save(only=...)` writes only the columns you name and leaves the rest of the row alone:

```python
--8<-- "docs/examples/mutations.py:save-only"
```

The row kept `plan = "pro"`, while the instance still held `"enterprise"` until `refresh()` reloaded it. Ferro runs no `SELECT` after the update, which is why the instance and the row can differ.

`save(exclude=...)` writes every column except the ones you name:

```python
--8<-- "docs/examples/mutations.py:save-exclude"
```

The name changed and the plan did not.

Two rules matter most. A partial save is an update, so it needs a persisted instance: on a transient one, `only=` and `exclude=` raise `ValueError`. And a call takes one write-set, not both. The [`Model.save`](../api/model.md#ferro.Model.save) reference lists the rest: which names count as columns, and which write-sets are empty and refused.

## Batch updates and deletes

`update(**values)` and `delete()` on a query change every matching row in one statement, without loading instances. Each returns the number of rows it changed:

```python
--8<-- "docs/examples/mutations.py:update-delete"
```

Three free customers moved to the `"trial"` plan, and Erin's row is gone.

!!! warning "Batch updates do not touch instances you hold"
    `where(...).update(...)` writes to the database only. Instances already in memory, including those in the identity map, keep their old values until you call `refresh()` on them.

### Expression updates

`update()` with keyword arguments sets literal values. To compute a value in the database, pass a lambda that returns a dict of column expressions:

```python
--8<-- "docs/examples/mutations.py:recipe-update"
```

The database added 1 to Alice's `views` and stamped `updated_at` with its own clock.

`+` and `-` follow SQL, so `NULL + 1` is `NULL`. `now` is a value you import (`from ferro import now`), not a function you call, and it can only be assigned to a `datetime` column.

On PostgreSQL, `.merge()` merges a dict into a JSON object column, with the patch winning where keys overlap. It replaces top-level keys only. A `NULL` column counts as `{}`. This block needs a PostgreSQL database:

=== "Assignment"

    ```python {compile-only="needs a PostgreSQL database"}
    from ferro import Field, Model


    class Conversation(Model):
        id: int | None = Field(default=None, primary_key=True)
        turns: dict = Field(default_factory=dict)


    chat = await Conversation.create()
    await Conversation.where(lambda conversation: conversation.id == chat.id).update(
        lambda conversation: {"turns": conversation.turns.merge({"r1": {"role": "user"}})}
    )
    ```

=== "Annotated"

    ```python {compile-only="needs a PostgreSQL database"}
    from typing import Annotated

    from ferro import Field, Model


    class Conversation(Model):
        id: Annotated[int | None, Field(primary_key=True)] = None
        turns: Annotated[dict, Field(default_factory=dict)]


    chat = await Conversation.create()
    await Conversation.where(lambda conversation: conversation.id == chat.id).update(
        lambda conversation: {"turns": conversation.turns.merge({"r1": {"role": "user"}})}
    )
    ```

After the update, `turns` holds `{"r1": {"role": "user"}}`. `.merge()` raises when the query is built if the connection is SQLite, the column holds a list, or the patch is a list.

### No `limit()` on batch writes

SQL has no portable `UPDATE ... LIMIT` or `DELETE ... LIMIT`. Rather than change every matching row, `update()` and `delete()` raise `ValueError` on a query with `limit()`, `offset()`, `after()` or `before()`:

```python
--8<-- "docs/examples/mutations.py:mutation-guard"
```

The error prints:

```text
delete() does not support limit/offset/after/before: portable SQL has no DELETE ... LIMIT. Remove the .limit()/.offset()/.after()/.before() call, or fetch primary keys first and delete by primary-key set.
```

To change a bounded set of rows, fetch their primary keys first, then filter on them:

```python
--8<-- "docs/examples/mutations.py:mutation-by-key"
```

Only Alice, the oldest trial customer, was deleted.

## Deleting one row

`instance.delete()` deletes the row behind an instance:

```python
--8<-- "docs/examples/mutations.py:delete-one"
```

Carol's row is gone. Deleting a parent row applies the `on_delete` rule of every foreign key that points at it, `CASCADE` by default; read [Delete behavior](relationships.md#delete-behavior) before deleting rows that have children.

## `upsert()`

`upsert(**fields)` inserts the row, or updates the existing row when the primary key already exists:

```python
--8<-- "docs/examples/mutations.py:upsert"
```

The second call updated row 100 in place. It wrote the whole row, so `name` went back to its default `""`: pass every field you want to keep.

The conflict target is the primary key only, so a conflict on another unique column still raises `UniqueViolationError`. Without a primary key there is nothing to conflict on, and the call inserts a new row. `upsert()` is shorthand for `instance.save(on_conflict="update")`, which does the same for an instance you already hold.

## `get_or_create()` and `update_or_create()`

`get_or_create(defaults={...}, **filters)` looks up a row by exact-match filters and creates it if none matches. It returns `(instance, created)`, and applies `defaults` only when it creates:

```python
--8<-- "docs/examples/mutations.py:get-or-create"
```

The first call created Heidi; the second found the same row.

`update_or_create(defaults={...}, **filters)` takes the same arguments, but when a row matches it applies `defaults` to it and saves it:

```python
--8<-- "docs/examples/mutations.py:update-or-create"
```

Heidi's row now has the `"pro"` plan.

!!! note "Not atomic"
    Both helpers read, then write. Two writers can both miss the row and both try to insert it; with a unique constraint on the filter columns, the second insert raises [`UniqueViolationError`](../api/exceptions.md#ferro.UniqueViolationError), so catch it and look the row up again. For an atomic insert-or-update on the primary key, use [`upsert()`](#upsert).

## `refresh()`

`refresh()` reloads an instance from its row and discards its in-memory values:

```python
--8<-- "docs/examples/mutations.py:refresh"
```

The batch update changed the row but not the instance; `refresh()` brought the new name in. It raises `RuntimeError` if the instance has no primary key or its row no longer exists.

## Database errors

Every database failure raises a typed exception under `FerroError`, so you never match on driver message text. Constraint violations are subclasses of `IntegrityError` and keep the driver's details as attributes:

```python
--8<-- "docs/examples/mutations.py:handling-errors"
```

On SQLite it prints:

```text
2067
None
UNIQUE constraint failed: customer.email
```

`sqlstate` is the database's error code (`"23505"` on PostgreSQL, `"2067"` on SQLite), `constraint` names the violated constraint on PostgreSQL and is `None` on SQLite, and `driver_message` is the driver's own text, for logs. The [exceptions reference](../api/exceptions.md) lists the whole hierarchy.

## See also

- [Transactions](transactions.md): group several writes so they commit or roll back together
- [Queries](queries.md): find the rows to change
- [Identity map](../concepts/identity-map.md): how a session tracks instances
- [`Model` reference](../api/model.md): every write method and its options
