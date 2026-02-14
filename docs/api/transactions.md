# Transactions API

Complete reference for transaction management.

## transaction()

Context manager for atomic database transactions.

```python
from ferro import transaction

async with transaction():
    # All operations are atomic
    user = await User.create(username="alice")
    await Post.create(title="Hello", author=user)
    # Auto-commits on success, auto-rolls back on exception
```

See the [Transactions Guide](../guide/transactions.md) for comprehensive usage patterns and examples.

## Manual Control

For advanced use cases requiring fine-grained control, Ferro provides low-level transaction management functions. Check your Ferro version's API documentation for availability:

- `begin_transaction()` - Manually start a new transaction
- `commit_transaction(tx_id)` - Commit a transaction by ID
- `rollback_transaction(tx_id)` - Roll back a transaction by ID

!!! warning
    Manual transaction control is advanced usage. The `transaction()` context manager is recommended for most use cases.

## See Also

- [Transactions Guide](../guide/transactions.md) - Complete usage guide with patterns
- [Mutations Guide](../guide/mutations.md) - Creating, updating, deleting records
- [Database Setup](../guide/database.md) - Connection management
