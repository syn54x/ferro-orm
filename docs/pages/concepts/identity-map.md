---
description: "Why a row fetched twice in one session is the same Python object, refreshed to the database's current values."
---

# Identity map

Within a session, Ferro's identity map makes one database row one Python object: fetching a row you already hold returns that same instance, refreshed to the database's current values.

The examples on this page use this `User` model, with four users saved (ada, grace, alan and edsger):

=== "Assignment"

    ```python
    --8<-- "docs/examples/concepts.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/concepts_annotated.py:models"
    ```

Fetching ada twice in one session, with a change to her row in between, gives back one object with the new value:

```python
--8<-- "docs/examples/concepts.py:refresh-on-load"
```

The second `User.get(1)` ran a query, found the instance the session already held and updated it in place. The unsaved edit was overwritten the same way: on a re-fetch, the database wins.

## Refresh on load

Every fetch runs a query; the identity map decides only whether its row comes back as a new instance or as the one you hold. It is a table for removing duplicates, not a query cache, so a held instance always shows what the last fetch read.

Ferro does not merge old and new values, because a merge policy is how stale-read bugs survive. If you have local changes you care about, `save()` them before the next fetch of that row, or do not hold a changed, unsaved instance across an `await` that could fetch it again.

## One row, one instance

Without an identity map, two queries that return the same row would give you two separate copies, and a change to one would not show through the other. With it, every way of fetching the row returns the same object. This block runs inside a [session](../guide/connections.md#sessions):

```python {test=run prelude=concepts}
by_key = await User.get(2)
by_filter = await User.where(lambda user: user.username == "grace").first()
assert by_filter is by_key

by_key.email = "grace@navy.mil"
assert by_filter.email == "grace@navy.mil"
```

The change made through `by_key` shows through `by_filter`, because they are one object. The guarantee holds within one session on one connection; it does not reach across processes or across sessions in the same process, since each session has its own map.

## Weak-reference map

The identity map lives in the Rust engine, one per session, keyed by connection, model name and primary key. Each entry is a weak reference to a Python instance, not the instance itself, and two things follow from that:

- **The map never keeps an instance alive.** When your code drops the last reference to an instance, Python releases it like any other object. Its entry is cleaned up on the next lookup that finds it, or by a periodic sweep, and the next fetch of that row builds a new instance.
- **`a is b` across two fetches holds only while you hold the first instance.** Once it is released, there is nothing for the second fetch to return.

Each session's map is its own, so a fetch in one session never returns an instance from another.

## Sessionless operations

The session bounds the map's lifetime, so identity is a session feature. Ferro resolves the connection for every operation once, with three outcomes:

- **A session is active**, from `async with engines.session():` or an explicit `session=`: the operation uses that session's identity map.
- **`using="name"` with no session**: the operation runs against that connection with no identity map. Every load returns a new instance, and nothing is kept, so nothing can go stale.
- **No session and no `using=`**: Ferro raises `RuntimeError` instead of guessing a connection.

These calls run with no session open:

```python
--8<-- "docs/examples/concepts.py:sessionless"
```

Both `using=` loads read grace's row, as two separate objects. The last call has no route and raises `RuntimeError: No database route for this operation. Open a session (...)`.

A `using=` that names a different connection from the session you are inside raises `ValueError`, so an operation never runs around the session you opened without telling you.

## What enters the map

Inside a session, an instance enters the map whenever Ferro hydrates or saves a full model:

- `Model.get(pk)` and `Model.get_or_none(pk)`
- query results from `.first()` and `.all()`
- `Model.create(...)`
- `instance.save()` and `instance.refresh()`

Three kinds of operation leave the map alone:

- Any of the above with `using=` and no session, as [Sessionless operations](#sessionless-operations) describes.
- `Model.bulk_create([...])`, which returns a row count, not instances. Query the rows again if you need instances in the map.
- Raw SQL (`fetch_all`, `fetch_one`), whose rows are plain dicts.

## Eviction

Because a re-fetch already refreshes a held instance, you rarely need to manage the map by hand. Two operations let you do so. These blocks run inside a [session](../guide/connections.md#sessions).

`refresh()` reloads an instance you hold, in place. It is the same mechanism as a re-fetch, as a method:

```python {test=run prelude=concepts}
from ferro import execute

alan = await User.get(3)
await execute('UPDATE "user" SET age = 18 WHERE id = 3')
await alan.refresh()
assert alan.age == 18
```

The `UPDATE` went around `alan`, and `refresh()` read the new age into the same object.

`evict_instance()` removes one entry from the session's map, so the next fetch builds a new instance instead of returning the one you hold. Use it when you want to detach from an instance you still reference:

```python {test=run prelude=concepts}
from ferro import evict_instance

ada = await User.get(1)
evict_instance(User, str(ada.id))
fresh = await User.get(1)
assert fresh is not ada
```

`fresh` is a new object for the same row, and `ada` is now detached from the map. The `pk` argument is the primary key as a string. `evict_instance` resolves its route the way any operation does; pass `using=` or `session=` to target a map other than the current session's.

Ferro also evicts on its own, scoped to the session:

- **A rolled-back transaction** empties the session's whole map. After a rollback, any instance created or changed inside it may no longer match the database, so Ferro starts over rather than guess which ones.
- **A bulk `update()` or `delete()`** evicts that model's entries in the session. Other models' instances stay.

One session's rollback or bulk write never touches another session's map.

!!! note "`ferro.migrate()` does not evict"
    Running `ferro.migrate()` while a session is open leaves that session's identity map as it is. Sequence schema changes and open sessions yourself.

## Turning the map off

The identity map is on by default. Pass `identity_map=False` to `connect()` to turn it off for every session on that connection:

```python
--8<-- "docs/examples/concepts.py:opt-out"
```

Inside the session, two loads of the same row returned two objects. The map is weak-valued and session-scoped, so turning it off saves the deduplication work, not memory; it suits read-heavy, ETL-style jobs where instance identity means nothing.

## Identity map in tests

A session's map lives as long as the session, so tests that share a process reset connection state between cases. `reset_engine()` closes every connection and any sessions still open on them:

```python {test=compile reason="a pytest fixture; pytest runs it, not the docs harness"}
import pytest

from ferro import connect, engines, reset_engine


@pytest.fixture
async def db():
    await connect("sqlite::memory:", auto_migrate=True)
    async with engines.session():
        yield
    reset_engine()
```

Each test that takes `db` starts with an empty database and a session of its own. [Testing](../howto/testing.md) covers fixtures in full.

## See also

- [Architecture](architecture.md): where the identity map sits in the engine
- [Performance](performance.md): what the map costs and saves
- [Connections › Sessions](../guide/connections.md#sessions): opening and nesting sessions
- [`evict_instance`][ferro.evict_instance]: the reference entry
