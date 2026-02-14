# Database Setup

Ferro requires an explicit connection to a database before any operations can be performed. Connectivity is managed by the high-performance Rust core using SQLx.

## Establishing a Connection

Use the `ferro.connect()` function to initialize the database engine. This is an asynchronous operation and must be awaited:

```python
import ferro

async def main():
    await ferro.connect("sqlite:example.db?mode=rwc")
```

## Connection Strings

Ferro supports SQLite, PostgreSQL, and MySQL. The connection string format follows standard URL patterns:

### SQLite

```python
# File database
await ferro.connect("sqlite:path/to/database.db")

# With create mode (recommended)
await ferro.connect("sqlite:example.db?mode=rwc")

# In-memory database
await ferro.connect("sqlite::memory:")
```

**Modes:**
- `rwc` - Read/Write/Create (creates database if it doesn't exist)
- `rw` - Read/Write (database must exist)
- `ro` - Read-only

### PostgreSQL

```python
# Basic connection
await ferro.connect("postgresql://user:password@localhost:5432/dbname")

# With SSL
await ferro.connect("postgresql://user:password@localhost:5432/dbname?sslmode=require")

# Connection pooling (custom pool size)
await ferro.connect(
    "postgresql://user:password@localhost:5432/dbname",
    max_connections=20
)
```

### MySQL

```python
# Basic connection
await ferro.connect("mysql://user:password@localhost:3306/dbname")

# With charset
await ferro.connect("mysql://user:password@localhost:3306/dbname?charset=utf8mb4")
```

## Connection Options

### Auto-Migration (Development)

During development, automatically align the database schema with your models:

```python
await ferro.connect("sqlite:dev.db?mode=rwc", auto_migrate=True)
```

!!! danger "Production Warning"
    `auto_migrate=True` is intended for development only. For production, use [Alembic migrations](migrations.md).

### Connection Pooling

Control the connection pool size:

```python
await ferro.connect(
    "postgresql://user:password@localhost/dbname",
    max_connections=20,      # Maximum pool size
    min_connections=5,       # Minimum idle connections
    connect_timeout=30       # Connection timeout in seconds
)
```

Default pool sizes:
- SQLite: 1 (single connection)
- PostgreSQL/MySQL: 10

### Connection Timeout

Set a timeout for establishing connections:

```python
await ferro.connect(
    "postgresql://localhost/dbname",
    connect_timeout=10  # seconds
)
```

## Manual Table Creation

Create tables manually without `auto_migrate`:

```python
import ferro

async def main():
    # Connect without auto-migrate
    await ferro.connect("sqlite::memory:")

    # Import models to register them
    from myapp.models import User, Post, Comment

    # Create all tables
    await ferro.create_tables()
```

## Multiple Databases

!!! note
    Multi-database support varies by Ferro version. Check your version's documentation.

Basic pattern for multiple databases:

```python
# Primary database
await ferro.connect(
    "postgresql://localhost/main_db",
    name="primary"
)

# Analytics database (read-only)
await ferro.connect(
    "postgresql://localhost/analytics_db",
    name="analytics",
    read_only=True
)

# Use specific database
users = await User.using("primary").all()
metrics = await Metric.using("analytics").all()
```

See [How-To: Multiple Databases](../howto/multiple-databases.md) for details.

## Health Checks

Check database connectivity:

```python
from ferro import check_connection

# Returns True if connected
is_connected = await check_connection()

if not is_connected:
    # Reconnect logic
    await ferro.connect("postgresql://localhost/dbname")
```

## Connection Context

For request-scoped connections (e.g., in web apps):

```python
from ferro import connection_context

async def handle_request():
    async with connection_context():
        # All queries in this context use the same connection
        user = await User.create(username="alice")
        await Post.create(title="Hello", author=user)
    # Connection released
```

## Environment Variables

Common pattern for configuration:

```python
import os
from ferro import connect

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:dev.db?mode=rwc"  # Default for development
)

async def init_db():
    await connect(
        DATABASE_URL,
        max_connections=int(os.getenv("DB_POOL_SIZE", "10")),
        connect_timeout=int(os.getenv("DB_TIMEOUT", "30"))
    )
```

## Best Practices

### Single Connection at Startup

Connect once when your application starts:

```python
# main.py
import ferro
from myapp.models import *  # Import all models

async def startup():
    await ferro.connect(DATABASE_URL)
    print("Database connected")

async def shutdown():
    await ferro.disconnect()
    print("Database disconnected")

# FastAPI example
from fastapi import FastAPI

app = FastAPI()

@app.on_event("startup")
async def on_startup():
    await startup()

@app.on_event("shutdown")
async def on_shutdown():
    await shutdown()
```

### Use Connection Pooling

For web applications, connection pooling is essential:

```python
# Production config
await ferro.connect(
    "postgresql://localhost/proddb",
    max_connections=50,  # Tune based on load
    min_connections=10
)
```

### Separate Dev/Prod Configs

```python
import os

if os.getenv("ENV") == "production":
    await ferro.connect(
        "postgresql://prodhost/proddb",
        max_connections=50
    )
else:
    await ferro.connect(
        "sqlite:dev.db?mode=rwc",
        auto_migrate=True
    )
```

### Handle Connection Errors

```python
from ferro import ConnectionError

try:
    await ferro.connect("postgresql://localhost/dbname")
except ConnectionError as e:
    logger.error(f"Failed to connect: {e}")
    # Fallback or exit
    sys.exit(1)
```

## Troubleshooting

### Connection Refused

```python
# Error: Connection refused at localhost:5432
# Solution: Check database is running
# PostgreSQL: sudo service postgresql start
# MySQL: sudo service mysql start
```

### Authentication Failed

```python
# Error: password authentication failed
# Solution: Check username/password in connection string
await ferro.connect("postgresql://correct_user:correct_pass@localhost/dbname")
```

### Database Does Not Exist

```python
# Error: database "dbname" does not exist
# Solution: Create database first
# PostgreSQL: createdb dbname
# Or use SQLite which auto-creates
```

### Pool Exhaustion

```python
# Error: Too many connections
# Solution: Increase max_connections or fix connection leaks
await ferro.connect(
    "postgresql://localhost/dbname",
    max_connections=100  # Increase pool size
)

# Also ensure connections are released:
# - Use context managers (async with)
# - Close connections after use
# - Fix stuck transactions
```

## See Also

- [Schema Management](migrations.md) - Alembic migrations
- [Transactions](transactions.md) - Connection affinity
- [How-To: Multiple Databases](../howto/multiple-databases.md) - Multi-database patterns
- [How-To: Testing](../howto/testing.md) - Test database setup
