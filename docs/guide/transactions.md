# Transactions

Ferro provides a simple and robust way to ensure data integrity through atomic transactions using an asynchronous context manager.

## Basic Usage

To group multiple database operations into a single atomic unit, use the `ferro.transaction()` context manager:

```python
from ferro import transaction

async def transfer_funds(from_user, to_user, amount):
    async with transaction():
        # Deduct from source
        from_user.balance -= amount
        await from_user.save()

        # Add to destination
        to_user.balance += amount
        await to_user.save()

        # Record transfer
        await Transfer.create(
            from_user=from_user,
            to_user=to_user,
            amount=amount
        )

    # If we reach here, all operations succeeded and were committed
```

## Atomicity and Rollbacks

When you enter a transaction block:

1. **Automatic Commit**: If the block finishes without an exception, Ferro automatically commits all changes to the database.
2. **Automatic Rollback**: If an exception is raised within the block, Ferro immediately rolls back all operations performed during that transaction, ensuring the database remains in a consistent state.

```python
try:
    async with transaction():
        user = await User.create(username="alice", email="alice@example.com")

        # This raises an exception
        raise ValueError("Something went wrong")

        # This line never executes
        await Post.create(title="Hello", author=user)

except ValueError:
    # The user creation was rolled back
    # Database is unchanged
    print("Transaction rolled back")

# Verify rollback
user = await User.where(User.username == "alice").first()
assert user is None  # User was not created
```

## Connection Affinity

Ferro's transaction engine uses **Connection Affinity** to guarantee correctness:

- **Shared Connection**: All operations performed within a `transaction()` block are guaranteed to use the same underlying database connection.
- **Task Safety**: Connection affinity is managed via `contextvars`, making it safe to use in highly concurrent asynchronous environments.

This ensures that:

1. All queries see the same transaction state
2. Rollbacks affect only operations within the transaction
3. Concurrent tasks use separate transactions

```python
import asyncio

async def task_a():
    async with transaction():
        await User.create(username="task_a_user")
        await asyncio.sleep(1)
        # Still in the same transaction

async def task_b():
    async with transaction():
        await User.create(username="task_b_user")
        # Separate transaction from task_a

# These run concurrently with separate transactions
await asyncio.gather(task_a(), task_b())
```

## Nested Transactions

!!! warning "Feature Not Implemented"
    Ferro currently supports single-level transactions only. Nested `transaction()` calls participate in the outermost transaction. True nested transactions with savepoints are not yet available. See [Coming Soon](../coming-soon.md#nested-transactions--savepoints) for more information.

```python
async with transaction():  # Outer transaction
    await User.create(username="alice")

    async with transaction():  # Participates in outer transaction (no savepoint)
        await Post.create(title="Hello")

    # If an exception occurs here, both User and Post are rolled back
```

## Error Handling Patterns

### Catch and Handle

```python
async with transaction():
    try:
        user = await User.create(username="alice", email="existing@example.com")
    except IntegrityError:
        # Handle duplicate email
        user = await User.where(User.email == "existing@example.com").first()

    # Continue with transaction
    await Post.create(title="Welcome", author=user)
```

### Conditional Rollback

```python
async with transaction():
    user = await User.create(username="bob")

    if not is_valid_email(user.email):
        # Explicitly raise to trigger rollback
        raise ValueError("Invalid email")

    await send_welcome_email(user.email)
```

### Cleanup After Rollback

```python
try:
    async with transaction():
        file_path = await save_file(uploaded_file)
        user = await User.create(username="alice", avatar=file_path)

        # This might fail
        await send_confirmation_email(user.email)

except EmailError:
    # Transaction rolled back, but file still exists
    if file_path:
        await delete_file(file_path)  # Clean up
```

## Performance Implications

### Transactions Have Overhead

Transactions involve database locks and logging. For read-only operations, transactions are unnecessary:

```python
# Don't wrap read-only operations
user = await User.where(User.id == 1).first()  # No transaction needed

# Do wrap writes
async with transaction():
    user.email = "new@example.com"
    await user.save()
```

### Keep Transactions Short

Long-running transactions can block other operations:

```python
# Bad: Long transaction holds locks
async with transaction():
    users = await User.all()  # Fetch data

    for user in users:
        # Slow external API call
        await send_email(user.email)  # Blocks other transactions!
        await user.save()

# Good: Minimize transaction scope
users = await User.all()  # Outside transaction

for user in users:
    await send_email(user.email)  # No locks held

    async with transaction():  # Short, focused transaction
        await user.save()
```

### Batch Operations in Transactions

Bulk operations are efficient within transactions:

```python
async with transaction():
    # These are batched and fast
    users = [User(username=f"user_{i}") for i in range(1000)]
    await User.bulk_create(users)
```

## Testing with Transactions

A common pattern for test isolation is to wrap each test in a transaction and roll it back:

```python
import pytest

@pytest.fixture
async def db_transaction():
    """Wraps each test in a transaction that rolls back after test."""
    from ferro import transaction, rollback_transaction, begin_transaction

    tx_id = await begin_transaction()
    try:
        yield
    finally:
        await rollback_transaction(tx_id)

async def test_user_creation(db_transaction):
    # Create user (will be rolled back after test)
    user = await User.create(username="test_user")
    assert user.id is not None

    # After test: rollback happens automatically
```

See [How-To: Testing](../howto/testing.md) for more patterns.

## Manual Transaction Control

While the context manager is recommended, you can use the low-level API for finer control:

### begin_transaction()

Manually start a new transaction:

```python
from ferro import begin_transaction, commit_transaction, rollback_transaction

tx_id = await begin_transaction()
```

Returns a unique transaction ID.

### commit_transaction(tx_id)

Commit changes for the given transaction:

```python
try:
    await User.create(username="alice")
    await commit_transaction(tx_id)
except Exception:
    await rollback_transaction(tx_id)
```

### rollback_transaction(tx_id)

Roll back changes for the given transaction:

```python
await rollback_transaction(tx_id)
```

### Example

```python
tx_id = await begin_transaction()

try:
    user = await User.create(username="alice")
    post = await Post.create(title="Hello", author=user)

    if not validate(post):
        raise ValidationError("Invalid post")

    await commit_transaction(tx_id)

except Exception as e:
    await rollback_transaction(tx_id)
    print(f"Transaction rolled back: {e}")
```

!!! warning
    Always ensure rollback happens in a `finally` block or exception handler. Unreleased transactions can cause connection leaks.

## Common Patterns

### Idempotent Operations

```python
async def create_or_update_user(username, email):
    async with transaction():
        user = await User.where(User.username == username).first()

        if user:
            user.email = email
            await user.save()
        else:
            user = await User.create(username=username, email=email)

        return user
```

### Multi-Step Processing

```python
async def process_order(order_id):
    async with transaction():
        order = await Order.where(Order.id == order_id).first()

        if order.status != "pending":
            raise ValueError("Order already processed")

        # Update inventory
        for item in await order.items.all():
            product = await item.product
            product.stock -= item.quantity
            await product.save()

        # Update order status
        order.status = "completed"
        await order.save()

        # Create invoice
        await Invoice.create(order=order, amount=order.total)
```

### Batch with Validation

```python
async def import_users(user_data_list):
    async with transaction():
        created = []

        for data in user_data_list:
            # Validate each record
            if not is_valid_email(data["email"]):
                # Rollback entire batch
                raise ValueError(f"Invalid email: {data['email']}")

            user = await User.create(**data)
            created.append(user)

        return created

    # If any validation fails, no users are created
```

## See Also

- [Mutations](mutations.md) - Creating, updating, and deleting records
- [Queries](queries.md) - Fetching data
- [How-To: Testing](../howto/testing.md) - Test isolation with transactions
- [Database Setup](database.md) - Connection management
