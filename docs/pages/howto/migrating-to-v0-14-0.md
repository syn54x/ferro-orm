# Migrating to v0.14.0

`v0.14.0` makes the mutation surface strict: no write operation silently does
more (or different) work than you asked for, and every database failure is
catchable by exception type.

Three behaviors change **without a deprecation window** — they are silent-data-
loss fixes, so there was no safe way for the old and new behavior to coexist:

1. `create()` no longer updates an existing row on primary-key conflict.
2. `save()` no longer upserts — it INSERTs new instances and UPDATEs persisted
   ones. Upsert is now an explicit, named operation.
3. `limit()`/`offset()` on a mutating query now raise instead of being
   silently ignored.

Your model definitions do not change, and code that never relied on the old
conflict behavior keeps working unchanged.

## What you need to do

### 1. Stop relying on `create()` to overwrite rows

Previously `create()` compiled to `INSERT ... ON CONFLICT (pk) DO UPDATE`: a
duplicate primary key silently **replaced** the existing row. It is now a plain
INSERT that raises [`UniqueViolationError`](../api/exceptions.md) and leaves
the existing row untouched.

=== "Before (v0.13: silent clobber)"

    ```python
    # Overwrote the stored row when id 1 already existed
    user = await User.create(id=1, email="taylor@example.com")
    ```

=== "After (insert-or-update, explicit)"

    ```python
    user = await User.upsert(id=1, email="taylor@example.com")
    ```

=== "After (insert only)"

    ```python
    from ferro import UniqueViolationError

    try:
        user = await User.create(id=1, email="taylor@example.com")
    except UniqueViolationError:
        ...  # duplicate — the stored row is untouched
    ```

### 2. Replace save-as-upsert with the explicit upsert surface

`save()` now tracks whether the instance has been persisted: a new instance is
INSERTed, a fetched or previously saved instance is UPDATEd by primary key. Two
consequences for code written against the old behavior:

- Saving a *new* instance whose primary key is already taken raises
  `UniqueViolationError` instead of overwriting the stored row.
- Saving a *persisted* instance whose row has been deleted underneath raises
  `ModelDoesNotExist` instead of silently re-inserting it.

Where you deliberately used `save()` to insert-or-update, say so explicitly:

=== "Before (v0.13: every save upserted)"

    ```python
    profile = Profile(id=user_id, theme="dark")
    await profile.save()  # inserted or replaced, whichever applied
    ```

=== "After (explicit upsert)"

    ```python
    profile = Profile(id=user_id, theme="dark")
    await profile.save(on_conflict="update")

    # or the one-call convenience:
    profile = await Profile.upsert(id=user_id, theme="dark")
    ```

The conflict target is the **primary key only**, and the whole row is written
on update — see [Upsert](../guide/mutations.md#upsert) for the details.

### 3. Remove `limit()`/`offset()` from mutating queries

Portable SQL has no `DELETE ... LIMIT`; previously the pagination was silently
dropped and the mutation touched **every** matching row. `update()` and
`delete()` now raise `ValueError` when the query carries `limit()` or
`offset()`. To mutate a bounded subset, fetch primary keys first:

=== "Before (v0.13: limit silently ignored!)"

    ```python
    # Looked bounded — actually deleted every matching row
    await Job.where(lambda job: job.done == True).limit(100).delete()  # noqa: E712
    ```

=== "After (bounded by primary-key set)"

    ```python
    batch = await Job.where(lambda job: job.done == True).limit(100).all()  # noqa: E712
    ids = [job.id for job in batch]
    await Job.where(lambda job: job.id.in_(ids)).delete()
    ```

### 4. Catch typed exceptions instead of `RuntimeError`

Database failures now raise a DBAPI-shaped hierarchy rooted at
[`ferro.FerroError`](../api/exceptions.md) instead of bare `RuntimeError` /
`ConnectionError`. Broad handlers keep working during the transition
(`FerroError` is still an `Exception`), but move them over:

=== "Before"

    ```python
    try:
        await order.save()
    except RuntimeError as exc:
        if "unique" in str(exc).lower():  # matching driver text
            ...
    ```

=== "After"

    ```python
    from ferro import UniqueViolationError

    try:
        await order.save()
    except UniqueViolationError as exc:
        ...  # exc.sqlstate / exc.constraint / exc.driver_message for detail
    ```

`connect()` failures are now `ferro.OperationalError` (server or environment)
or `ferro.InterfaceError` (bad scheme or configuration) instead of
`ConnectionError`.

## Schema-emission changes

v0.14.0 also unifies how the Rust runtime emitter and the Alembic bridge derive
column types and artifact names — one decision table, consumed by both (see
`docs/solutions/patterns/derived-type-and-naming-decision-table.md`). Three
storage defaults change for *derived* types (fields with no explicit
`db_type`). **Auto-migrate never rewrites an existing column silently**: where
your database was bootstrapped by an older emitter, Ferro warns and skips, and
you choose between the keep-recipe and the convert-recipe below.

### 5. Plain `datetime` fields are timezone-aware (`timestamptz`)

The Alembic bridge previously mapped a plain `datetime.datetime` field to a
timezone-naive `DateTime`; the runtime already used `timestamptz`. Both now
emit `timestamptz`. If your schema has naive `timestamp` columns (bootstrapped
via Alembic), auto-migrate refuses the conversion and warns.

=== "Keep the column naive"

    ```python
    class Event(Ferro):
        occurred_at: datetime.datetime = FerroField(db_type="timestamp")
    ```

=== "Convert intentionally (reviewed Alembic migration)"

    ```sql
    ALTER TABLE event
        ALTER COLUMN occurred_at TYPE timestamptz
        USING occurred_at AT TIME ZONE 'UTC';  -- your source timezone
    ```

### 6. Enum fields default to native Postgres enum types

The runtime emitter previously stored Enum fields as `varchar`; the Alembic
bridge already emitted a native enum type. Both now emit the native type
(created idempotently; on SQLite the column is `varchar(<max label length>)`,
matching SQLAlchemy). If your schema has varchar enum columns (bootstrapped by
the runtime), auto-migrate refuses the conversion and warns.

=== "Keep varchar storage"

    ```python
    class Account(Ferro):
        role: Role = FerroField(db_type="varchar", db_check=True)
    ```

=== "Convert intentionally (reviewed Alembic migration)"

    ```sql
    CREATE TYPE role AS ENUM ('admin', 'member');
    ALTER TABLE account
        ALTER COLUMN role TYPE role USING role::role;
    ```

### 7. `datetime.time` fields store as `time`

The runtime emitter previously stored `datetime.time` fields as `varchar` (the
bridge already said `TIME`). Both now emit `time`. Existing varchar columns:
auto-migrate refuses the conversion and warns — keep with
`db_type="varchar"`, or convert with a reviewed migration using an explicit
`USING` cast.

### 8. Constraint and index names are single-sourced

- **Foreign keys are now named** (`fk_<table>_<col>_<to_table>`) in the DDL of
  both emitters. Existing databases need **no action**: Alembic autogenerate
  compares foreign keys by signature, not name, and auto-migrate never alters
  existing foreign keys.
- **Single-column uniques become named unique indexes** (`uq_<table>_<col>`),
  the same shape composite uniques always had. On an existing database
  auto-migrate adds the named index (`CREATE UNIQUE INDEX IF NOT EXISTS` —
  additive and idempotent); the old inline artifact (e.g. `account_email_key`
  on Postgres) remains as a redundant duplicate until you drop it:

    ```sql
    ALTER TABLE account DROP CONSTRAINT account_email_key;
    ```

- Identifiers longer than 63 characters now truncate deterministically on both
  emitters (previously only some name kinds guarded, and PostgreSQL truncated
  the rest silently).

## Changed surfaces at a glance

| Surface | v0.13 behavior | v0.14 behavior | Migration |
| --- | --- | --- | --- |
| `Model.create(**fields)` on existing PK | Silently replaced the row | Raises `UniqueViolationError` | `Model.upsert(...)` if you wanted replace |
| `instance.save()` on a new instance with taken PK | Silently replaced the row | Raises `UniqueViolationError` | `save(on_conflict="update")` |
| `instance.save()` after the row was deleted | Silently re-inserted | Raises `ModelDoesNotExist` | Catch and re-`create()` if intended |
| `Query.limit(...).update()/.delete()` | Pagination silently ignored | Raises `ValueError` | Fetch PKs, mutate by PK set |
| Database failures | `RuntimeError(...)` with driver text | Typed `FerroError` subclasses | Catch by type, not message |
| Plain `datetime` field (Alembic bridge) | Naive `timestamp` | `timestamptz` | Keep via `db_type="timestamp"`, or convert with `USING ... AT TIME ZONE` |
| Enum field (runtime emitter) | `varchar` | Native PG enum type | Keep via `db_type="varchar"`, or convert with `USING col::<enum>` |
| `datetime.time` field (runtime emitter) | `varchar` | `time` | Keep via `db_type="varchar"`, or convert with `USING` |
| FK constraints | Anonymous | Named `fk_<table>_<col>_<to_table>` | None (names not compared) |
| Single-column unique | Inline column `UNIQUE` | Named `uq_<table>_<col>` unique index | Optional: drop the old inline constraint |

## What about the deprecated v0.12 surfaces?

[Migrating to v0.12.0](migrating-to-v0-12-0.md) announced that operator-style
predicates, implicit default-connection routing, and the private Alembic
helpers are planned for removal in `v0.14.0`. That removal is tracked
separately and may ship in a later release — but the warnings still mean what
they say. If you have not migrated yet, do it now:

```bash
uv run pytest -W error::DeprecationWarning
```

## Verifying your migration

The new failure modes are all loud, so your test suite is the verification:
run it and look specifically for `UniqueViolationError` where you relied on
clobbering, `ValueError` on paginated mutations, and `except RuntimeError`
handlers that no longer fire. Grep for the risky patterns:

```bash
grep -rn "on_conflict\|\.limit(" --include="*.py" src/ | grep -n "delete()\|update("
```

A green suite with typed `except` handlers means you are on the new mutation
surface.
