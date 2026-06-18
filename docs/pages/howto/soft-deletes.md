# Soft Deletes

Soft deletes mark rows as deleted instead of removing them, so records can be audited or restored later. In Ferro this is two flag fields plus a small mixin that supplies the lifecycle methods.

## The Pattern

=== "Assignment"

    ```python
    --8<-- "docs/examples/soft_deletes.py:model"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/soft_deletes_annotated.py:model"
    ```

- **Fields on the concrete model.** `is_deleted` and `deleted_at` are declared on `Invoice` itself. Every model that wants soft deletes repeats these two declarations.
- **`SoftDeleteMixin` for behavior.** `soft_delete()` and `restore()` flip the flags and `save()`; `active()` is a classmethod returning a normal [`Query`](../api/queries.md) filtered to non-deleted rows, so it chains like any other query.

The mixin is a plain class, not a `Model` subclass. Ferro registers table schemas and query proxies on each model class as it is defined, so a `Model` base class cannot contribute fields to subclasses — declare fields on each concrete model and keep shared behavior in the mixin. (See the [Timestamps how-to](timestamps.md) for the same pattern.)

## Usage

```python
--8<-- "docs/examples/soft_deletes.py:usage"
```

`soft_delete()` keeps the row in the table — `Invoice.select().count()` still sees it — while `Invoice.active()` excludes it. `restore()` brings it back.

## Querying

Use `active()` as the entry point everywhere you would otherwise use `select()` or `where()`:

```python
unpaid = await Invoice.active().where(lambda t: t.number.like("INV-%")).all()
trashed = await Invoice.where(lambda t: t.is_deleted == True).all()  # noqa: E712
```

Two things to remember:

- **Nothing is filtered automatically.** `Invoice.all()`, `Invoice.select()`, `Invoice.get(pk)` and relationship traversals still return soft-deleted rows. The `active()` discipline is a convention your code must follow.
- **Batch and instance deletes bypass soft delete.** `await invoice.delete()` and `await Invoice.where(...).delete()` issue real `DELETE` statements — the mixin only adds `soft_delete()`, it does not intercept the built-in delete paths. Reach for the hard delete deliberately (e.g. retention cleanup), not by accident.

If you want soft-deleted rows to age out, a periodic job can purge them for real:

```python
async def purge_deleted() -> int:
    return await Invoice.where(lambda t: t.is_deleted == True).delete()  # noqa: E712
```

## Trade-offs

- **Unique constraints see soft-deleted rows.** A unique column like `number: str = Field(unique=True)` still holds the value after a soft delete, so creating a replacement with the same number fails. Options: hard-delete in that flow, rename the value on soft delete (e.g. suffix the primary key), or drop the database-level constraint and enforce uniqueness among active rows in application code.
- **Data growth.** Soft-deleted rows stay in the table and its indexes, so table scans, backups, and index sizes grow forever unless you purge. Pair soft deletes with a retention policy.
- **Privacy.** "Deleted" data is still data. If users expect deletion to remove personal information, soft delete alone does not satisfy that — schedule a real purge.

## See Also

- [Timestamps how-to](timestamps.md) — the mixin pattern explained in detail
- [Queries guide](../guide/queries.md) — building filtered queries
- [Mutations guide](../guide/mutations.md) — `save()`, `delete()`, and batch operations
