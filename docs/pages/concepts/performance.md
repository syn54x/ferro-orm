# Performance

Ferro moves SQL generation, parameter binding, and row hydration out of Python and into compiled Rust, with the GIL released during database I/O. This page is honest about where that helps, where it doesn't, and how to get the most out of it.

## Where the Rust Core Pays Off

The Rust core helps most where a traditional ORM spends significant CPU time in Python:

**Bulk inserts.** `bulk_create` serializes and binds an entire batch in Rust and writes it as a single statement. The per-row Python overhead — building parameter lists, driver round-trips, object bookkeeping — largely disappears:

```python
users = [
    User(username=f"user_{i}", email=f"user{i}@example.com")
    for i in range(10_000)
]
await User.bulk_create(users)
```

**Hydrating large result sets.** Converting thousands of database rows into model instances is CPU work. Ferro decodes rows and assembles instances in Rust, so queries returning many rows spend far less time in pure-Python parsing loops than a traditional ORM does.

**Concurrent workloads.** Because the GIL is released while queries are in flight, other coroutines keep running. Many in-flight queries don't serialize behind Python's interpreter lock.

## Where It Doesn't

Be skeptical of any ORM benchmark — including ours — that ignores these:

**Network and disk dominate small operations.** A single-row `get()` spends most of its wall-clock time waiting for the database. Rust can't make the network faster; for point lookups, Ferro performs in the same neighborhood as any async ORM.

**Slow queries stay slow.** A missing index or a full-table scan costs the same regardless of which library sends the SQL. Ferro doesn't fix query plans.

**Application logic bottlenecks.** If your endpoint spends its time in business logic, template rendering, or external API calls, swapping the ORM moves nothing. Profile first.

## Getting the Most Out of Ferro

**Use `bulk_create` instead of create loops.** One statement beats N statements:

```python
# Slow: N round-trips
for i in range(1000):
    await User.create(username=f"user_{i}")

# Fast: one bulk statement
await User.bulk_create([User(username=f"user_{i}") for i in range(1000)])
```

Batches in the low thousands (roughly 1,000–5,000 rows) are a good unit of work — large enough to amortize overhead, small enough to keep statements and memory reasonable. Note that `bulk_create` skips the [identity map](identity-map.md) by design.

**Use batch update/delete instead of instance loops.** Push the work into one SQL statement:

```python
# Slow: fetch everything, save row by row
users = await User.where(lambda t: t.active == False).all()
for user in users:
    user.status = "archived"
    await user.save()

# Fast: one UPDATE
await User.where(lambda t: t.active == False).update(status="archived")
```

**Wrap write bursts in a transaction.** Each standalone write commits individually; a transaction amortizes the commit cost across the batch and pins all work to one connection:

```python
from ferro import transaction

async with transaction():
    order = await Order.create(total=total)
    await OrderLine.bulk_create(lines)
```

Keep transactions short — don't hold one open across external API calls.

**Prefer `.exists()` over `.count()` for presence checks**, and add `index=True` to fields you filter on frequently:

```python
if await User.where(lambda t: t.email == email).exists():
    raise ValueError("Email taken")
```

**Know your identity map effects.** Repeated fetches of the same row return the cached instance rather than re-hydrating, which is a win for hot rows. The flip side: every hydrated instance stays cached for the connection's lifetime, so long-running jobs sweeping huge tables should paginate and evict as they go, or connect with `identity_map=False`. See [Identity Map](identity-map.md).

**Watch for N+1 relationship access.** Awaiting `post.author` in a loop issues one query per post. Eager loading (`prefetch_related`-style) is [on the roadmap](../roadmap.md); until then, restructure hot paths to filter on the parent side or batch by IDs with `in_()`.

## Benchmark It Yourself

We deliberately don't publish "Nx faster" tables here: results vary enormously with database engine, hardware, network latency, batch size, and row shape. If performance matters to your decision, measure your own workload:

```python
import time

rows = [User(username=f"user_{i}") for i in range(5000)]

start = time.perf_counter()
await User.bulk_create(rows)
print(f"bulk_create: {time.perf_counter() - start:.3f}s")

start = time.perf_counter()
fetched = await User.all()
print(f"all() with hydration: {time.perf_counter() - start:.3f}s")
```

For a fair comparison against another ORM: use the same database, the same schema, warmed connections, realistic batch sizes, and run each measurement several times. Compare the operations your application actually performs — not just the ones that flatter either library.

## See Also

- [Architecture](architecture.md) — why the engine is shaped this way
- [Identity Map](identity-map.md) — caching behavior and memory
- [Queries](../guide/queries.md) — batch update and delete
