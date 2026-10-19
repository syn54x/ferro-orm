---
description: "Which workloads Ferro's Rust core speeds up, which it does not, and how to get the most from it."
---

# Performance

Ferro builds SQL, binds values and turns rows into instances in compiled Rust, and waits on the database without holding Python's interpreter lock; this page explains which workloads that speeds up, which it does not, and how to get the most from it.

The examples on this page use this `User` model, with four users saved (ada, grace, alan and edsger):

=== "Assignment"

    ```python
    --8<-- "docs/examples/concepts.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/concepts_annotated.py:models"
    ```

A batch insert is where the Rust core does the most work for you:

```python {test=run prelude=concepts}
from ferro import engines

async with engines.session():
    inserted = await User.bulk_create(
        [
            User(username=f"user_{i}", email=f"user{i}@example.com", age=20 + i % 50)
            for i in range(10_000)
        ]
    )
    assert inserted == 10_000
```

Ten thousand rows went in with one call. Python dumped each instance to plain values once, and Rust bound the whole batch and wrote it as multi-row `INSERT` statements, as few as the backend's limit on bound parameters allows, all or nothing.

## Where Rust helps

The Rust core helps where a pure-Python ORM spends its CPU time in Python:

- **Bulk inserts.** `bulk_create` builds the statements and binds every value in Rust, so the per-row Python work is building, validating and dumping each instance.
- **Large result sets.** Decoding rows and filling instances happens in Rust, not in a Python loop over the driver's rows.
- **Concurrent work.** The database wait runs on Rust's tokio runtime without Python's interpreter lock, so other coroutines keep running while queries are in flight. [Architecture › Async from end to end](architecture.md#async-from-end-to-end) explains how.

## Where Rust does not help

Be skeptical of any ORM benchmark, ours included, that ignores these:

- **The network and the disk.** A single-row `get()` spends most of its time waiting on the database, and Rust cannot make that wait shorter.
- **Slow queries.** A missing index or a full table scan costs the same whichever library sends the SQL. Ferro does not change query plans.
- **Application logic.** If an endpoint spends its time in business logic, template rendering or calls to other services, a faster ORM does not change it. Profile first.

## Tuning checklist

Each block in this section runs inside a [session](../guide/connections.md#sessions).

### `bulk_create` instead of a `create` loop

A loop of `create()` calls sends one `INSERT` per row, each its own round trip; `bulk_create` sends one multi-row `INSERT` per chunk:

```python {test=run prelude=concepts}
new_users = [
    User(username=f"intern_{i}", email=f"intern{i}@example.com", age=19)
    for i in range(1_000)
]

# 1,000 statements:
#   for user in new_users:
#       await User.create(**user.model_dump(exclude={"id"}))

inserted = await User.bulk_create(new_users)
assert inserted == 1_000
```

The 1,000 interns went in through one call. Batch size has no limit of its own: Ferro splits a batch under the backend's parameter limit and keeps the whole call all or nothing, so size batches by what your application can hold in memory. `bulk_create` returns a count, not instances, and skips the [identity map](identity-map.md#what-enters-the-map).

### Bulk `update()` and `delete()` instead of instance loops

Fetching rows to change and save them one by one sends a query plus one `UPDATE` per row. A query's `update()` sends one statement:

```python {test=run prelude=concepts}
changed = await User.where(lambda user: user.active == False).update(
    active=True
)
assert changed == 1
assert await User.where(lambda user: user.active == False).count() == 0
```

One `UPDATE` reactivated edsger, the one inactive user, and returned the number of rows it changed. [Mutations](../guide/mutations.md) covers bulk updates and deletes.

### A transaction around a burst of writes

Outside a transaction, each write commits on its own. A transaction pays for one commit across the batch and pins the work to one connection:

```python {test=run prelude=concepts}
from ferro import transaction

async with transaction():
    ada = await User.get(1)
    ada.age += 1
    await ada.save()
    await User.create(username="barbara", email="barbara@example.com", age=33)

assert (await User.get(1)).age == 37
```

Both writes committed together. Keep transactions short, and do not hold one open across a call to another service.

### Indexes for frequent filters

A filter on a column with no index reads the whole table. Add `index=True` to a field you filter on often; `unique=True`, as on `username`, creates a unique index too:

```python {test=run prelude=concepts}
taken = await User.where(lambda user: user.username == "grace").exists()
assert taken
```

SQLite and PostgreSQL can answer the lookup on `username` from the `uq_user_username` index. `exists()` runs a `COUNT`, so it costs what `count()` costs; use whichever reads better. [Models and fields › Uniques and indexes](../guide/models-and-fields.md#uniques-and-indexes) covers both options.

### The identity map

The [identity map](identity-map.md) holds weak references and lasts as long as the session, so it does not grow a long job's memory; once your code drops an instance, its entry goes too. It saves object identity, not queries: every fetch still runs its query. For ETL-style reads where instance identity means nothing, `connect(..., identity_map=False)` skips the deduplication work.

### Relations in one query with `include()`

Reading `post.author` on each post of a list runs one query per post. `Post.where(...).include(lambda post: post.author)` loads every author in the same statement as the posts; [Queries › Populating relations](../guide/queries.md#populating-relations-with-include) shows it. `BackRef` and many-to-many collections are not populated by `include()` yet ([roadmap](../roadmap.md#reverse-and-many-to-many-population)); batch them by id with `in_()`.

## Benchmarking

Ferro publishes no "N times faster" table: results depend on the database, hardware, network, batch size and row shape. If performance decides your choice, time your own workload. This block runs inside a [session](../guide/connections.md#sessions):

```python {test=run prelude=concepts}
import time

rows = [
    User(username=f"bench_{i}", email=f"bench{i}@example.com", age=30)
    for i in range(5_000)
]

start = time.perf_counter()
await User.bulk_create(rows)
print(f"bulk_create: {time.perf_counter() - start:.3f}s")

start = time.perf_counter()
users = await User.all()
print(f"all() with hydration: {time.perf_counter() - start:.3f}s")
assert len(users) == 5_004
```

It prints two lines, `bulk_create: <seconds>s` and `all() with hydration: <seconds>s`, for the 5,000 new rows and the 5,004 rows read back. The numbers are your machine's; run the same block against your own database and with your own models before you compare.

For a fair comparison with another ORM, use the same database and schema, warm the connections, use realistic batch sizes and run each measurement several times. Compare the operations your application performs, not the ones that flatter either library.

## See also

- [Architecture](architecture.md): why the engine is shaped this way
- [Identity map](identity-map.md): what the map keeps and for how long
- [Mutations](../guide/mutations.md): `bulk_create`, bulk updates and deletes
- [Queries › Populating relations](../guide/queries.md#populating-relations-with-include): `include()`
