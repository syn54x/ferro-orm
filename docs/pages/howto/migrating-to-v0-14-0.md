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

## Changed surfaces at a glance

| Surface | v0.13 behavior | v0.14 behavior | Migration |
| --- | --- | --- | --- |
| `Model.create(**fields)` on existing PK | Silently replaced the row | Raises `UniqueViolationError` | `Model.upsert(...)` if you wanted replace |
| `instance.save()` on a new instance with taken PK | Silently replaced the row | Raises `UniqueViolationError` | `save(on_conflict="update")` |
| `instance.save()` after the row was deleted | Silently re-inserted | Raises `ModelDoesNotExist` | Catch and re-`create()` if intended |
| `Query.limit(...).update()/.delete()` | Pagination silently ignored | Raises `ValueError` | Fetch PKs, mutate by PK set |
| Database failures | `RuntimeError(...)` with driver text | Typed `FerroError` subclasses | Catch by type, not message |

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
