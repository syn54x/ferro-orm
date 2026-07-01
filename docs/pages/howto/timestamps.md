# Timestamps

Track when rows are created and last modified with `created_at` / `updated_at` fields: `created_at` is filled by a field default, and `updated_at` is refreshed by a small mixin that hooks `save()`.

## The Pattern

=== "Assignment"

    ```python
    --8<-- "docs/examples/timestamps.py:model"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/timestamps_annotated.py:model"
    ```

Two pieces work together:

- **Field defaults on the concrete model.** `created_at` and `updated_at` are declared on `Note` itself with `default_factory=utcnow`, so both are set when an instance is constructed.
- **`TimestampMixin` for behavior.** The mixin overrides `save()` to touch `updated_at` before delegating to `Model.save()`. It is a *plain class* — not a `Model` subclass — and contributes only behavior, never fields.

Every model that wants timestamps repeats the same two field declarations and adds the mixin to its bases (mixin first, so its `save()` wins in the MRO).

!!! note "Why a mixin instead of a `Model` base class?"

    You might expect to declare the fields once on a shared base, e.g. `class Timestamped(Model)` with `created_at` / `updated_at`, and inherit from it. Ferro does not support this: the ORM registers a table schema and query proxies (the class attributes behind `Note.created_at > ...`) on each model class as it is defined, so fields declared on a `Model` base class are not contributed to its subclasses' tables. Keep shared *behavior* in a plain mixin and declare *fields* on each concrete model.

## Usage

```python
--8<-- "docs/examples/timestamps.py:usage"
```

`created_at` is set once when the instance is created; every subsequent `save()` advances `updated_at`. Note that the mixin only hooks instance `save()` (which `create()` paths go through) — batch updates like `Note.where(...).update(...)` write columns directly and will not touch `updated_at` unless you set it explicitly in the update.

## Timezone Notes

Store UTC, always. The example's `utcnow()` helper returns a timezone-aware datetime:

```python
from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)
```

Avoid naive `datetime.now()` — it captures the server's local clock, which makes values ambiguous and breaks comparisons across hosts and DST changes. Keep storage in UTC and convert to the user's timezone only at the display layer.

### Naive vs timezone-aware columns

A `datetime` field maps to Postgres `timestamptz` (SQLite stores both the same
way). If you point a model at a pre-existing plain `timestamp` (no time zone)
column, Ferro will **not** silently convert it — auto-migrate warns and leaves the
column untouched, because a `timestamp` → `timestamptz` cast reinterprets stored
values under the connection's timezone and can shift your data.

- To keep the column naive, declare the field with `db_type="timestamp"`.
- To convert it intentionally, run a reviewed migration (Alembic) with an explicit
  source timezone, e.g. `USING occurred_at AT TIME ZONE 'UTC'`.

## See Also

- [Models & Fields guide](../guide/models-and-fields.md) — field defaults and `default_factory`
- [Soft Deletes how-to](soft-deletes.md) — the same mixin pattern applied to deletion
- [Mutations guide](../guide/mutations.md) — instance `save()` vs batch `update()`
