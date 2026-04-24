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

### Supabase (managed PostgreSQL)

[Supabase](https://supabase.com/) hosts PostgreSQL behind TLS. Ferro’s Rust driver stack connects with **Rustls** and the **webpki** CA bundle, so TLS to public Supabase endpoints works out of the box in published wheels and in normal `maturin` / `uv` builds from this repository.

**Connection string**

1. In the Supabase project dashboard, open **Project Settings → Database** and copy the URI (direct or pooler—use the string Supabase gives you for your client type).
2. Append TLS if it is not already present:

```python
import os

url = os.environ["DATABASE_URL"]
if "sslmode=" not in url:
    sep = "&" if "?" in url else "?"
    url = f"{url}{sep}sslmode=require"

await ferro.connect(url)
```

Supabase’s pooler hostname often looks like `*.pooler.supabase.com`; the database name is usually `postgres`, and the username may include the project ref (for example `postgres.<project_ref>`). Prefer the **exact** URI from the dashboard so host, port, and user stay correct when Supabase changes defaults.

**Secrets and shells**

- Load the URI from an environment variable or secret manager—never commit it to git.
- Passwords can contain characters that shells treat specially (for example `$`). In POSIX shells, wrap the value in **single quotes** when exporting, or put the URL in a `.env` file read by your app instead of the shell.

**Password characters in the URL**

If you assemble the URI yourself, percent-encode reserved characters in the password (for example `%24` for `$`, `%5E` for `^`) per [RFC 3986](https://datatracker.ietf.org/doc/html/rfc3986#section-2.1) userinfo rules. Many drivers accept unencoded passwords until one character breaks parsing; encoding avoids surprises.

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

!!! warning "Feature Not Implemented"
    Multi-database support is not yet available. Ferro currently supports only a single database connection per application. See [Coming Soon](../coming-soon.md#multiple-database-support) and [How-To: Multiple Databases](../howto/multiple-databases.md) for planned features.

## Health Checks

!!! warning "Feature Not Implemented"
    `check_connection()` is not yet available. See [Coming Soon](../coming-soon.md#check_connection) for workarounds.

**Workaround:**

```python
# Attempt a simple query to verify connectivity
try:
    await User.select().limit(1).all()
    is_connected = True
except Exception:
    is_connected = False
```

## Connection Context

!!! warning "Feature Not Implemented"
    `connection_context()` is not yet available. See [Coming Soon](../coming-soon.md#connection_context) for more information. Use `transaction()` for scoped database operations.

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
    # Graceful shutdown (manual cleanup if needed)
    print("Database connection will be cleaned up on process exit")

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

!!! note "disconnect() Not Available"
    The `disconnect()` function is not yet implemented. Connection cleanup happens automatically on process exit. See [Coming Soon](../coming-soon.md#disconnect) for more information.

### Use Connection Pooling

!!! note
    Advanced connection pool parameters may not be fully supported. See [Coming Soon](../coming-soon.md#connection-pool-configuration).

For web applications with basic connection support:

```python
# Basic connection for production
await ferro.connect("postgresql://localhost/proddb")
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
# Connection errors will raise exceptions
try:
    await ferro.connect("postgresql://localhost/dbname")
except Exception as e:
    logger.error(f"Failed to connect: {e}")
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

### TLS / SSL errors (PostgreSQL, Supabase)

```text
# Error: TLS upgrade required ... SQLx was built without TLS support
```

Ferro’s default build enables PostgreSQL TLS via SQLx (`tls-rustls-ring-webpki` in `Cargo.toml`). If you see the message above, you are using an extension built **without** that feature (for example a stripped-down local `cargo build`). Reinstall the published wheel or rebuild from this repo’s `Cargo.toml` without removing TLS features.

If the server requires TLS but the URL omits it, add `?sslmode=require` (or `&sslmode=require` after other query parameters) as shown in the Supabase subsection above.

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
