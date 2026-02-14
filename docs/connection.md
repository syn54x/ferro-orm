# Connection

Ferro requires an explicit connection to a database before any operations can be performed. Connectivity is managed by the high-performance Rust core using `SQLx`.

## Establishing a Connection

Use the `ferro.connect()` function to initialize the database engine. This is an asynchronous operation and must be awaited.

```python
import ferro

async def main():
    await ferro.connect("sqlite:example.db?mode=rwc")
```

## Connection Strings

Ferro supports SQLite, Postgres, and MySQL. The connection string format follows standard URL patterns:

| Database | Connection String Example |
| :--- | :--- |
| **SQLite** | `sqlite:path/to/database.db` or `sqlite::memory:` |
| **Postgres**| `postgres://user:password@localhost:5432/dbname` |
| **MySQL** | `mysql://user:password@localhost:3306/dbname` |

### SQLite Notes
For SQLite, it is recommended to include `?mode=rwc` (Read/Write/Create) to ensure the database file is created if it does not exist.

## Automatic Migrations (Dev Mode)

During development, you can use the `auto_migrate=True` flag to automatically align the database schema with your Python models upon connection.

```python
await ferro.connect("sqlite:example.db?mode=rwc", auto_migrate=True)
```

!!! danger "Production Warning"
    `auto_migrate=True` is intended for development only. For production environments, you should use **Alembic** for explicit schema versioning and migrations. See the [Migrations](migrations.md) section for details.

## Manual Table Creation

If you prefer to trigger table creation manually (without using `auto_migrate` during connect), you can use the `create_tables()` function:

```python
await ferro.connect("sqlite::memory:")
# ... define or import models ...
await ferro.create_tables()
```
