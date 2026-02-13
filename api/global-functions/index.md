# Global Functions

## `connect(url, auto_migrate=False)`

Establish a connection to the database.

Parameters:

| Name           | Type   | Description                                                          | Default    |
| -------------- | ------ | -------------------------------------------------------------------- | ---------- |
| `url`          | `str`  | The database connection string (e.g., "sqlite:example.db?mode=rwc"). | *required* |
| `auto_migrate` | `bool` | If True, automatically create tables for all registered models.      | `False`    |

Source code in `src/ferro/__init__.py`

```python
async def connect(url: str, auto_migrate: bool = False) -> None:
    """
    Establish a connection to the database.

    Args:
        url: The database connection string (e.g., "sqlite:example.db?mode=rwc").
        auto_migrate: If True, automatically create tables for all registered models.
    """
    from .relations import resolve_relationships

    resolve_relationships()

    await _core_connect(url)
    if auto_migrate:
        await create_tables()
```

## `transaction()`

Run database operations inside a transaction context

Yields control to the caller within an open transaction.

Examples:

```pycon
>>> async with transaction():
...     user = await User.create(name="Taylor")
...     await user.save()
```

Source code in `src/ferro/models.py`

```python
@asynccontextmanager
async def transaction():
    """Run database operations inside a transaction context

    Yields control to the caller within an open transaction.

    Examples:
        >>> async with transaction():
        ...     user = await User.create(name="Taylor")
        ...     await user.save()
    """
    tx_id = await begin_transaction()
    token = _CURRENT_TRANSACTION.set(tx_id)
    try:
        yield
        await commit_transaction(tx_id)
    except Exception:
        await rollback_transaction(tx_id)
        raise
    finally:
        _CURRENT_TRANSACTION.reset(token)
```

## `create_tables()`

Manually triggers table creation for all registered models.

Returns an awaitable object (Python coroutine).

### Errors

Returns a `PyErr` if the engine is not initialized or if SQL execution fails.

## `reset_engine()`

Shuts down the global engine and clears the Identity Map.

This is useful for testing environments to ensure isolation between test runs.

### Errors

Returns a `PyErr` if the engine lock cannot be acquired.

## `evict_instance(name, pk)`

Evicts a specific model instance from the global Identity Map.
