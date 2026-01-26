# Queries

Ferro provides a fluent, type-safe API for constructing and executing database queries. All queries are constructed in Python and executed by the high-performance Rust engine.

## Fetching Data

Queries are typically started using the `select()` or `where()` methods on a Model class.

### Basic Filtering
Use standard Python comparison operators on Model fields to create filter conditions.

```python
# Select all active users
users = await User.where(User.is_active == True).all()

# Select users with age >= 18
adults = await User.where(User.age >= 18).all()
```

### Chaining
Methods can be chained to build complex queries incrementally.

```python
results = await Product.select() \
    .where(Product.category == "Electronics") \
    .order_by(Product.price, "desc") \
    .limit(10) \
    .offset(5) \
    .all()
```

### Logical Operators
Use bitwise operators for complex logical conditions. Note that parentheses are required around each condition.

- **AND**: `&`
- **OR**: `|`

```python
# (age > 21 AND status == 'active') OR role == 'admin'
query = User.where(
    ((User.age > 21) & (User.status == "active")) | (User.role == "admin")
)
```

## Terminal Operations

These methods execute the query and return a result.

| Method | Return Type | Description |
| :--- | :--- | :--- |
| `.all()` | `list[Model]` | Executes the query and returns all matching records. |
| `.first()` | `Model \| None` | Returns the first matching record or `None`. |
| `.count()` | `int` | Returns the total number of matching records. |
| `.exists()` | `bool` | Returns `True` if at least one matching record exists. |

## Mutations

Ferro supports both instance-level and batch mutation operations.

### Creating Records
```python
# Single record
user = await User.create(username="alice", email="alice@example.com")

# Bulk creation (highly efficient)
users = [User(username=f"user_{i}") for i in range(100)]
await User.bulk_create(users)
```

### Updating Records
Batch updates can be performed directly on a query without loading instances into memory.

```python
# Update all products in a category
await Product.where(Product.category == "Old").update(status="archived")
```

### Deleting Records
```python
# Delete specific instance
await user.delete()

# Batch deletion
await User.where(User.is_active == False).delete()
```

## SQL Injection Protection

All values passed to the fluent API (via `.where()`, `.update()`, etc.) are automatically parameterized by the Rust engine. Raw user input is never concatenated into SQL strings, ensuring built-in protection against SQL injection attacks.
