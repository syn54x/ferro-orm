# How to record created and updated times

This page gives a model `created_at` and `updated_at` fields: a field default sets both when the row is created, and a mixin sets `updated_at` again in every `save()`.

## The `TimestampMixin`

Declare both fields on the model with a default of the current UTC time, and put the mixin first among the bases so its `save()` runs before `Model.save()`:

=== "Assignment"

    ```python
    --8<-- "docs/examples/timestamps.py:model"
    --8<-- "docs/examples/timestamps.py:session"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/timestamps_annotated.py:model"
    --8<-- "docs/examples/timestamps_annotated.py:session"
    ```

The new note's `created_at` is an aware datetime in UTC. `create()` saves through `save()`, so the mixin runs for it too. The blocks below run inside this session; [Sessions](../guide/connections.md#sessions) explains why every call needs one.

The mixin is a plain class, not a `Model` subclass, so every model that uses it declares the two fields itself. A `Model` base class would get a table of its own; [Fields on a model base class](../guide/models-and-fields.md#fields-on-a-model-base-class) shows that table.

## Updating a note

Change a field and save, and the mixin moves `updated_at` forward:

```python
--8<-- "docs/examples/timestamps.py:usage"
```

`created_at` keeps the time the note was created, and `updated_at` is now later than before the save.

## Batch updates

!!! warning "Batch updates skip the mixin"
    `Note.where(...).update(...)` writes its columns in one `UPDATE` statement without loading the rows, so no `save()` runs and `updated_at` keeps its old value unless the update sets it.

Pass `updated_at=utcnow()` beside the columns the update changes:

```python
--8<-- "docs/examples/timestamps.py:batch"
```

The archived note now has the latest `updated_at`, so it sorts first, and it reads back as an aware datetime that compares with `utcnow()`. Ferro stores `utcnow()` in an update exactly as `save()` stores it, so rows touched either way sort together.

!!! warning "`now` on SQLite"
    The database-clock recipe `update(lambda note: {"updated_at": now})` writes SQLite's own time format, whole seconds with no time zone (`2026-10-19 14:56:27`), while `save()` writes `2026-10-19T14:56:27.736246Z`. SQLite compares the two as text, so such a row sorts before every row `save()` wrote on the same day, however much earlier. It also reads back as a naive datetime, which raises `TypeError` when compared with `utcnow()`. Use `utcnow()` on SQLite; on Postgres, `now` stores a `timestamptz` like any other.

## Storing UTC

`utcnow()` returns `datetime.now(UTC)`, an aware datetime. A naive `datetime.now()` reads the server's local clock, so its values change meaning between hosts and across daylight-saving changes. Store UTC and convert to the reader's time zone when you display the value.

## Naive and aware columns

A `datetime` field is a `timestamptz` column on Postgres; SQLite stores both kinds as text. If a model's `datetime` field points at an existing `timestamp` column (no time zone), auto-migrate warns and leaves the column as it is: casting `timestamp` to `timestamptz` reads the stored values in the connection's time zone and can shift them.

- To keep the column naive, declare the field with `db_type="timestamp"`.
- To convert it, write the cast yourself with an explicit source time zone, such as `USING occurred_at AT TIME ZONE 'UTC'`, in a [hand-written migration step](../guide/schema/migrations.md#hand-written-steps) or an Alembic revision.

## See also

- [Fields on a model base class](../guide/models-and-fields.md#fields-on-a-model-base-class): why the fields go on each model.
- [How to soft-delete rows](soft-deletes.md): the same mixin pattern for deletion.
- [Batch updates and deletes](../guide/mutations.md#batch-updates-and-deletes) and [`Query.update()`][ferro.query.builder.Query.update] in the API reference.
