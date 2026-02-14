# Transactions

Ferro provides a simple and robust way to ensure data integrity through atomic transactions using an asynchronous context manager.

## Usage

To group multiple database operations into a single atomic unit, use the `ferro.transaction()` context manager.

```python
from ferro import transaction

async def process_order(user, product):
    async with transaction():
        # All operations inside this block are atomic
        order = await Order.create(user=user, product=product)
        await user.posts.add(order)

        # If any error occurs here, everything above is rolled back
        await product.refresh()
```

## Atomicity and Rollbacks

When you enter a transaction block:

1.  **Automatic Commit**: If the block finishes without an exception, Ferro automatically commits all changes to the database.
2.  **Automatic Rollback**: If an exception is raised within the block, Ferro immediately rolls back all operations performed during that transaction, ensuring the database remains in a consistent state.

```python
try:
    async with transaction():
        await User.create(username="alice")
        raise RuntimeError("Something went wrong")
except RuntimeError:
    # 'alice' was never persisted to the database
    pass
```

## Connection Affinity

Ferro's transaction engine uses **Connection Affinity** to guarantee correctness:

-   **Shared Connection**: All operations performed within a `transaction()` block are guaranteed to use the same underlying database connection.
-   **Task Safety**: Connection affinity is managed via `contextvars`, making it safe to use in highly concurrent asynchronous environments.

## Manual Control

While the context manager is the recommended way to handle transactions, you can also use the low-level API if you need finer control:

| Method | Description |
| :--- | :--- |
| `begin_transaction()` | Manually starts a new transaction and returns a unique `tx_id`. |
| `commit_transaction(tx_id)` | Commits all changes for the given transaction ID. |
| `rollback_transaction(tx_id)` | Rolls back all changes for the given transaction ID. |

!!! warning "Note on Nesting"
    Ferro currently supports single-level transactions. Nested `async with transaction():` calls will participate in the outermost transaction.
