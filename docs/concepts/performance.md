# Performance

Understanding where Ferro is fast, where to optimize, and how to get the best performance.

## Where Ferro Excels

### Bulk Operations

Ferro's Rust engine shines with large datasets:

```python
# Create 10,000 users
users = [User(username=f"user_{i}", email=f"user{i}@example.com")
         for i in range(10000)]

await User.bulk_create(users)
# Ferro: ~100-300ms
# Traditional Python ORM: ~5-10 seconds
```

**Why:** Rust handles serialization, parameter binding, and SQL generation without the GIL.

### Complex Queries

Multi-join queries with filtering:

```python
posts = await Post.where(
    (Post.published == True) &
    (Post.author.username.like("a%")) &
    (Post.created_at > cutoff_date)
).order_by(Post.views, "desc").limit(100).all()

# Ferro: ~10-50ms
# Traditional ORM: ~50-200ms
```

**Why:** Sea-Query generates optimized SQL, SQLx parses rows efficiently.

### Row Hydration

Converting database rows to Python objects:

```python
users = await User.all()  # 1000 users

# Ferro: ~20ms (Rust hydration)
# Traditional ORM: ~100-200ms (Python hydration)
```

**Why:** Rust parses rows and populates memory directly, Python just wraps the result.

## Where Ferro is Similar

### Single Row Operations

```python
user = await User.get(1)
# Ferro: ~3ms
# Traditional ORM: ~3-5ms
```

**Why:** Network latency dominates. Both ORMs spend similar time waiting for the database.

### Schema Introspection

```python
from ferro import get_metadata
metadata = get_metadata()
# Similar speed to SQLAlchemy
```

**Why:** Schema introspection happens infrequently (mostly at startup).

## Optimization Techniques

### 1. Use Bulk Operations

```python
# Slow (N queries)
for i in range(1000):
    await User.create(username=f"user_{i}")

# Fast (1 query)
users = [User(username=f"user_{i}") for i in range(1000)]
await User.bulk_create(users)
```

### 2. Use Batch Updates

```python
# Slow (N queries)
users = await User.where(User.is_active == False).all()
for user in users:
    user.status = "archived"
    await user.save()

# Fast (1 query)
await User.where(User.is_active == False).update(status="archived")
```

### 3. Index Frequently Filtered Fields

```python
class User(Model):
    email: Annotated[str, FerroField(unique=True, index=True)]
    status: Annotated[str, FerroField(index=True)]
    created_at: Annotated[datetime, FerroField(index=True)]
```

### 4. Use `.exists()` Instead of `.count()`

```python
# Slow
if await User.where(User.email == email).count() > 0:
    raise ValueError("Email taken")

# Fast
if await User.where(User.email == email).exists():
    raise ValueError("Email taken")
```

### 5. Avoid N+1 Queries

```python
# Bad (N+1 queries)
posts = await Post.all()  # 1 query
for post in posts:
    author = await post.author  # N queries!

# Good (prefetch if supported)
# Check your Ferro version for eager loading support
posts = await Post.select().prefetch_related("author").all()
```

### 6. Use Connection Pooling

```python
await ferro.connect(
    "postgresql://localhost/db",
    max_connections=50,  # Tune for your load
    min_connections=10
)
```

### 7. Keep Transactions Short

```python
# Bad: Long transaction
async with transaction():
    users = await User.all()
    for user in users:
        await external_api_call(user)  # Slow!
        await user.save()

# Good: Minimize transaction scope
users = await User.all()
for user in users:
    await external_api_call(user)
    async with transaction():
        await user.save()
```

## Profiling

### Query Timing

```python
import time

start = time.time()
users = await User.where(User.is_active == True).all()
elapsed = time.time() - start

print(f"Query took {elapsed*1000:.2f}ms")
```

### Enable SQL Logging

```python
# Check your Ferro version for SQL logging configuration
import logging

logging.basicConfig(level=logging.DEBUG)
# SQL queries will be logged
```

## Benchmarking

Compare operations:

```python
import asyncio
import time

async def benchmark():
    # Bulk create
    users = [User(username=f"user_{i}") for i in range(1000)]

    start = time.time()
    await User.bulk_create(users)
    print(f"Bulk create: {time.time() - start:.3f}s")

    # Query all
    start = time.time()
    all_users = await User.all()
    print(f"Query all: {time.time() - start:.3f}s")

    # Update all
    start = time.time()
    await User.where(User.id > 0).update(is_active=True)
    print(f"Batch update: {time.time() - start:.3f}s")

asyncio.run(benchmark())
```

## See Also

- [Architecture](architecture.md) - How Ferro achieves performance
- [Queries](../guide/queries.md) - Query optimization
- [How-To: Pagination](../howto/pagination.md) - Efficient pagination
