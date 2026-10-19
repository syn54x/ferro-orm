# How to soft-delete rows

This page marks rows as deleted instead of removing them, so they can be audited or restored later: two fields on the model hold the mark, and a mixin supplies `soft_delete()`, `restore()` and `active()`.

## The `SoftDeleteMixin`

Declare `is_deleted` and `deleted_at` on the model, and put the mixin first among its bases:

=== "Assignment"

    ```python
    --8<-- "docs/examples/soft_deletes.py:model"
    --8<-- "docs/examples/soft_deletes.py:session"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/soft_deletes_annotated.py:model"
    --8<-- "docs/examples/soft_deletes_annotated.py:session"
    ```

The table now holds three live invoices. The blocks below run inside this session; [Sessions](../guide/connections.md#sessions) explains why every call needs one.

`soft_delete()` and `restore()` set the two fields and save. `active()` returns an ordinary [`Query`][ferro.query.builder.Query] filtered to rows that are not deleted, so it chains like any other query. Its lambda names its parameter `row` rather than after a model, because the mixin serves every model that uses it.

The mixin is a plain class, not a `Model` subclass, so every model that uses it declares the two fields itself. A `Model` base class would get a table of its own; [Fields on a model base class](../guide/models-and-fields.md#fields-on-a-model-base-class) shows that table.

## Deleting and restoring

Soft-delete an invoice, count, then restore it:

```python
--8<-- "docs/examples/soft_deletes.py:usage"
```

After `soft_delete()`, `active()` counts two invoices while `select()` still counts three: the row stays in the table. `restore()` brings it back to `active()`.

## Querying live rows

Start every query that should skip deleted rows from `active()`, and chain from there. With INV-001 soft-deleted again:

```python
--8<-- "docs/examples/soft_deletes.py:querying"
```

`live` holds the two live invoices from INV-002 on, and `trashed` holds only the soft-deleted INV-001.

!!! warning "Nothing is filtered automatically"
    `Invoice.all()`, `Invoice.select()`, `Invoice.get(pk)` and relationship traversals still return soft-deleted rows. Only queries that start from `active()` skip them.

`await invoice.delete()` and `Invoice.where(...).delete()` still issue a real `DELETE`: the mixin adds `soft_delete()` beside them and changes neither.

## Trade-offs

**A unique column still holds a soft-deleted row's value.** `number` is unique, so while INV-001 is soft-deleted, a new invoice cannot take its number:

```python
--8<-- "docs/examples/soft_deletes.py:unique"
```

It prints `INV-001 is still taken`. When a flow must reuse the value, hard-delete the row in that flow, or change the value as part of the soft delete (append the primary key, for example). Checking for a live duplicate in application code before inserting is not safe: two requests can both pass the check and both insert.

**Soft-deleted rows keep their space.** They stay in the table and its indexes, so scans, backups and indexes grow. Pair soft deletes with a retention period, and a job that purges the rows past it with a real `DELETE`:

```python
--8<-- "docs/examples/soft_deletes.py:purge"
```

The purge removes INV-001 for good and leaves the two live invoices.

**Soft-deleted data is still stored.** If users expect deletion to remove their personal data, a soft delete does not do that; purge the row for real.

## See also

- [How to record created and updated times](timestamps.md): the same mixin pattern for timestamps.
- [Fields on a model base class](../guide/models-and-fields.md#fields-on-a-model-base-class): why the fields go on each model.
- [Batch updates and deletes](../guide/mutations.md#batch-updates-and-deletes) and [`Query.delete()`][ferro.query.builder.Query.delete] in the API reference.
