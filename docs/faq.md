# Frequently Asked Questions

## General

### What is Ferro?

Ferro is a high-performance async ORM for Python with a Rust engine. It provides Pydantic-native models with 10-100x faster bulk operations compared to traditional Python ORMs.

### How does Ferro compare to SQLAlchemy?

**Ferro:**
- Faster (Rust engine)
- Simpler API (Pydantic-based)
- Async-first
- Smaller ecosystem

**SQLAlchemy:**
- More mature and battle-tested
- Larger ecosystem
- More flexible (multiple APIs)
- Steeper learning curve

Choose Ferro for performance and simplicity. Choose SQLAlchemy for maturity and maximum flexibility.

See [Why Ferro?](why-ferro.md) for detailed comparison.

### Do I need to know Rust to use Ferro?

**No.** Ferro is a pure Python API. The Rust engine is completely transparent. You write 100% Python code.

You only need Rust if you want to:
- Build Ferro from source
- Contribute to the Rust engine
- Create custom extensions

### Can I use Ferro with FastAPI?

**Yes!** Ferro works great with FastAPI:

```python
from fastapi import FastAPI
from ferro import connect
from myapp.models import User

app = FastAPI()

@app.on_event("startup")
async def startup():
    await connect("postgresql://localhost/db")

@app.get("/users")
async def list_users():
    return await User.all()
```

### Can I use Ferro with Django?

Ferro is a standalone ORM and doesn't integrate with Django's ORM system. You can use Ferro in a Django project as a separate database layer, but you'll lose Django admin, migrations, and other Django ORM features.

For Django projects, we recommend using Django ORM.

### Is Ferro production-ready?

Ferro is suitable for production use, but consider:

**Pros:**
- Fast and reliable
- Well-tested
- Active development

**Cons:**
- Newer than SQLAlchemy/Django ORM
- Smaller community
- Fewer integrations

We recommend thorough testing before deploying to production.

## Performance

### How much faster is Ferro?

Typical improvements:
- Bulk inserts: **10-100x faster**
- Complex queries: **5-10x faster**
- Single row operations: **1.5-2x faster**

Exact numbers depend on database, hardware, and workload. See [Performance](concepts/performance.md).

### Will Ferro make my API faster?

**Maybe.** Ferro helps most when:
- Processing large datasets
- Bulk operations
- Complex queries

Ferro helps less when:
- Network latency dominates
- Business logic is the bottleneck
- Database is slow (Ferro can't fix slow queries)

Profile your application to identify bottlenecks.

### How do I benchmark Ferro vs other ORMs?

```python
import time

# Test bulk insert
users = [User(username=f"user_{i}") for i in range(10000)]

start = time.time()
await User.bulk_create(users)
print(f"Ferro: {time.time() - start:.2f}s")

# Compare with other ORM using same data
```

## Features

### Does Ferro support migrations?

**Yes!** Ferro integrates with Alembic for production migrations:

```bash
pip install "ferro-orm[alembic]"
alembic init migrations
# Configure env.py
alembic revision --autogenerate -m "Initial"
alembic upgrade head
```

See [Schema Management](guide/migrations.md).

### Does Ferro support raw SQL?

Check your Ferro version's API for raw SQL support. Most versions provide an escape hatch for complex queries not supported by the query builder.

### Does Ferro support multiple databases?

Multi-database support varies by version. Check your version's documentation for `using()` or similar APIs.

See [How-To: Multiple Databases](howto/multiple-databases.md).

### Does Ferro support async?

**Yes!** Ferro is async-first. All database operations are asynchronous:

```python
users = await User.all()  # Async
user = await User.create(username="alice")  # Async
```

### Can I use Ferro with sync code?

Ferro requires async/await. For sync code, use `asyncio.run()`:

```python
import asyncio

def sync_function():
    users = asyncio.run(User.all())
    return users
```

## Troubleshooting

### Why is my query slow?

Common causes:
1. Missing indexes
2. N+1 queries
3. Large result sets without pagination
4. Slow database
5. Network latency

See [Performance](concepts/performance.md) for optimization tips.

### How do I debug SQL queries?

Enable SQL logging (check your version's API):

```python
import logging
logging.basicConfig(level=logging.DEBUG)
# SQL queries will be logged
```

### Why am I getting IntegrityError?

Common causes:
- Duplicate values in unique fields
- Missing required fields
- Foreign key violations
- Primary key conflicts

Check the error message for details.

### How do I reset the database?

```python
# Drop all tables
await ferro.drop_all_tables()

# Recreate
await ferro.create_tables()
```

Or use Alembic migrations:

```bash
alembic downgrade base
alembic upgrade head
```

## Development

### How do I contribute?

See [Contributing](contributing.md) for guidelines.

### Where do I report bugs?

[GitHub Issues](https://github.com/syn54x/ferro-orm/issues)

### Where do I ask questions?

[GitHub Discussions](https://github.com/syn54x/ferro-orm/discussions)

### Is there a Discord/Slack?

Not yet. Use GitHub Discussions for now.

## Migration

### How do I migrate from SQLAlchemy?

See [Migrating from SQLAlchemy](migration-sqlalchemy.md) for a detailed guide.

### How do I migrate from Django ORM?

Migration guide coming soon. Key differences:
- Replace Django models with Ferro models
- Use async/await
- Replace Django's migration system with Alembic

### How do I migrate from Tortoise ORM?

Ferro and Tortoise have similar APIs. Key changes:
- Replace Tortoise models with Ferro models
- Update relationship syntax
- Use Alembic instead of Aerich

## Still Have Questions?

Ask on [GitHub Discussions](https://github.com/syn54x/ferro-orm/discussions)!
