# Utilities API

Utility functions and helpers.

## Connection Management

### connect()

Establish a connection to the database.

```python
from ferro import connect

# SQLite
await connect("sqlite:example.db?mode=rwc")

# PostgreSQL
await connect("postgresql://user:password@localhost/dbname")

# With options
await connect(
    "postgresql://localhost/dbname",
    max_connections=20,
    auto_migrate=True  # Development only
)
```

See [Database Setup Guide](../guide/database.md) for complete connection options.

### disconnect()

Close the database connection.

```python
from ferro import disconnect

await disconnect()
```

### create_tables()

Manually create all registered model tables.

```python
from ferro import create_tables

await create_tables()
```

!!! note
    With `auto_migrate=True`, tables are created automatically on connect.

## Identity Map Management

### evict_instance()

Remove an instance from the identity map, forcing a fresh database fetch on next access.

```python
from ferro import evict_instance

# Evict user with ID=1
evict_instance("User", 1)

# Next fetch will hit database
user = await User.get(1)
```

See [Identity Map Concept](../concepts/identity-map.md) for when and why to evict instances.

## See Also

- [Database Setup Guide](../guide/database.md) - Connection configuration
- [Identity Map Concept](../concepts/identity-map.md) - Instance caching details
- [Schema Management](../guide/migrations.md) - Production migrations
