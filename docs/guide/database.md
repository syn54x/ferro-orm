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

Ferro currently supports SQLite and PostgreSQL. The connection string format follows standard URL patterns:

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

# Development connection with auto-migrate
await ferro.connect(
    "postgresql://user:password@localhost:5432/dbname",
    auto_migrate=True,
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

## Connection Options

### Named Connections

Ferro can keep multiple active pools in one process. Unnamed `connect()` calls register and select the `"default"` connection. Named connections are explicit and only become the default when `default=True` is passed.

```python
import os
import ferro

await ferro.connect(
    os.environ["APP_DATABASE_URL"],
    name="app",
    default=True,
    pool=ferro.PoolConfig(max_connections=10, min_connections=1),
)
await ferro.connect(
    os.environ["SERVICE_DATABASE_URL"],
    name="service",
    pool=ferro.PoolConfig(max_connections=3),
)

# Default app role
users = await User.all()

# Explicit service role
job = await Job.using("service").create(kind="backfill")
await ferro.execute("select run_internal_job(?)", job.id, using="service")
```

Use constants or trusted server-side code to choose `using` values. Do not bind connection names directly from request parameters, headers, GraphQL arguments, or other untrusted input.

### Transaction Inheritance

Transactions are bound to one connection. Operations inside the block inherit that connection; trying to switch to another connection inside the transaction raises.

```python
async with ferro.transaction(using="service"):
    await Job.create(kind="backfill")  # runs on service
    await ferro.execute("select set_config('role_context', ?, true)", "pipeline")
```

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

    # Create all tables on the default connection
    await ferro.create_tables()
```

## Multiple Databases

Use named connections for multiple databases, roles, or pools:

```python
await ferro.connect(os.environ["APP_DATABASE_URL"], name="app", default=True)
await ferro.connect(os.environ["SERVICE_DATABASE_URL"], name="service")

await ferro.create_tables(using="service")
service_users = await User.using("service").all()
```

Ferro does not provide automatic router policies, read/write splitting, distributed transactions, or cross-connection joins in v1. Route each operation explicitly when it should not use the default connection.

### Supabase Role Guidance

For Supabase/Postgres deployments, keep elevated service credentials server-side. Prefer least-privileged custom roles where possible, and avoid making a service-role connection the default in user-facing runtimes.

Named connections isolate pools and roles, not per-request RLS/JWT/session context inside one shared pool. If you set Postgres session state, prefer transaction-local settings and keep the work inside `transaction(using=...)`.

Service-origin objects can contain data unavailable to the app role. Treat them as elevated data and filter them deliberately before returning user-facing responses.

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
        auto_migrate=os.getenv("ENV") != "production"
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

### Use Long-Lived Pools

For web applications, connect once at startup and reuse those pools:

```python
await ferro.connect("postgresql://localhost/proddb", name="app", default=True)
```

### Separate Dev/Prod Configs

```python
import os

if os.getenv("ENV") == "production":
    await ferro.connect("postgresql://prodhost/proddb")
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

### Unsupported connect() kwargs

```python
# Example of kwargs Ferro does not currently accept:
# await ferro.connect("postgresql://localhost/dbname", max_connections=100)
```

If you need custom pool sizing or timeout controls today, Ferro does not expose them yet through `connect()`.

## See Also

- [Schema Management](migrations.md) - Alembic migrations
- [Transactions](transactions.md) - Connection affinity
- [How-To: Multiple Databases](../howto/multiple-databases.md) - Multi-database patterns
- [How-To: Testing](../howto/testing.md) - Test database setup
