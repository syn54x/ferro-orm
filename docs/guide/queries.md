# Queries

Ferro provides a fluent, type-safe API for constructing and executing database queries. All queries are constructed in Python and executed by the high-performance Rust engine.

## Basic Filtering

Use standard Python comparison operators on model fields to create filter conditions:

```python
# Equality
users = await User.where(User.is_active == True).all()

# Comparison
adults = await User.where(User.age >= 18).all()
seniors = await User.where(User.age > 65).all()

# String matching
alice_users = await User.where(User.name.like("Alice%")).all()
```

### Supported Operators

| Operator | SQL Equivalent | Example |
|----------|----------------|---------|
| `==` | `=` | `User.status == "active"` |
| `!=` | `!=` or `<>` | `User.role != "admin"` |
| `>` | `>` | `User.age > 18` |
| `>=` | `>=` | `User.age >= 21` |
| `<` | `<` | `User.score < 100` |
| `<=` | `<=` | `User.score <= 50` |
| `.like()` | `LIKE` | `User.email.like("%@example.com")` |
| `.in_()` | `IN` | `User.status.in_(["active", "pending"])` |

## Logical Operators

Combine conditions with `&` (AND) and `|` (OR). **Always use parentheses** around each condition:

```python
# AND
query = User.where((User.age > 21) & (User.status == "active"))

# OR
query = User.where((User.role == "admin") | (User.role == "moderator"))

# Complex: (age > 21 AND status == 'active') OR role == 'admin'
query = User.where(
    ((User.age > 21) & (User.status == "active")) | (User.role == "admin")
)

# NOT with !=
inactive_users = await User.where(User.is_active != True).all()
```

## Chaining

Methods can be chained to build complex queries incrementally:

```python
results = await Product.select() \
    .where(Product.category == "Electronics") \
    .where(Product.price < 1000) \
    .order_by(Product.price, "desc") \
    .limit(10) \
    .offset(5) \
    .all()
```

Multiple `.where()` calls are combined with AND.

## Ordering

Sort results with `.order_by()`:

```python
# Single field, ascending (default)
users = await User.order_by(User.created_at).all()

# Single field, descending
users = await User.order_by(User.created_at, "desc").all()

# Multiple fields
products = await Product.order_by(Product.category) \
    .order_by(Product.price, "desc") \
    .all()
```

## Limiting and Offsetting

### Limit

Restrict the number of results:

```python
# Get first 10 users
users = await User.limit(10).all()

# Get top 5 highest-scoring players
top_players = await Player.order_by(Player.score, "desc").limit(5).all()
```

### Offset

Skip a number of results (useful for pagination):

```python
# Skip first 10, get next 10
users = await User.order_by(User.id).offset(10).limit(10).all()

# Page 3 (20 per page): skip 40, take 20
page_3 = await Product.offset(40).limit(20).all()
```

For better pagination patterns, see [How-To: Pagination](../howto/pagination.md).

## Terminal Operations

These methods execute the query and return results:

### `.all()`

Returns all matching records as a list:

```python
all_users = await User.where(User.is_active == True).all()
# Returns: list[User]
```

### `.first()`

Returns the first matching record or `None`:

```python
admin = await User.where(User.role == "admin").first()
# Returns: User | None

if admin:
    print(f"Admin: {admin.username}")
```

### `.count()`

Returns the total number of matching records:

```python
active_count = await User.where(User.is_active == True).count()
# Returns: int

print(f"Active users: {active_count}")
```

### `.exists()`

Returns `True` if at least one matching record exists:

```python
has_admin = await User.where(User.role == "admin").exists()
# Returns: bool

if not has_admin:
    print("Warning: No admin users found!")
```

!!! tip "Performance"
    Use `.exists()` instead of `.count() > 0`. It's more efficient because the database can stop after finding the first match.

## Aggregations

!!! note
    Aggregation support varies by Ferro version. Check your version's capabilities.

```python
# Count
total_users = await User.count()

# Group by (if supported)
# Check API documentation for your version
```

## Selecting Specific Fields

By default, Ferro loads all fields. To select specific fields:

```python
# Select specific fields (if supported in your version)
users = await User.select(User.id, User.username).all()
```

## Working with Relationships

### Forward Relations

Access foreign keys:

```python
post = await Post.where(Post.id == 1).first()
author = await post.author  # Fetches the related User
```

### Reverse Relations

Query the reverse side:

```python
author = await User.where(User.username == "alice").first()

# Get all posts by author
author_posts = await author.posts.all()

# Filter reverse relation
published_posts = await author.posts.where(Post.published == True).all()

# Count reverse relation
post_count = await author.posts.count()
```

### Eager Loading

!!! note
    Check your Ferro version for `.prefetch_related()` or similar eager-loading support to avoid N+1 queries.

## Advanced Filtering

### NULL Checks

```python
# Find records with NULL field
users_no_phone = await User.where(User.phone == None).all()

# Find records with non-NULL field
users_with_phone = await User.where(User.phone != None).all()
```

### IN Queries

```python
# Using .in_()
active_statuses = ["active", "pending", "verified"]
users = await User.where(User.status.in_(active_statuses)).all()

# NOT IN
banned_users = await User.where(User.status.not_in_(["banned", "suspended"])).all()
```

### LIKE Patterns

```python
# Starts with
gmail_users = await User.where(User.email.like("%.gmail.com")).all()

# Contains
smith_users = await User.where(User.name.like("%Smith%")).all()

# Case-insensitive (if supported)
users = await User.where(User.email.ilike("%EXAMPLE.COM")).all()
```

## Raw SQL

For complex queries not supported by the query builder:

```python
# Check your version's API for raw SQL support
# Example (API may vary):
from ferro import raw_query

results = await raw_query(
    "SELECT * FROM users WHERE age > $1 AND status = $2",
    18,
    "active"
)
```

!!! warning "SQL Injection"
    Always use parameterized queries (e.g., `$1`, `$2`). Never interpolate user input directly into SQL strings.

## Performance Tips

### Use `.exists()` for Checks

```python
# Bad (loads full count)
if await User.where(User.email == email).count() > 0:
    raise ValueError("Email already exists")

# Good (stops at first match)
if await User.where(User.email == email).exists():
    raise ValueError("Email already exists")
```

### Use Indexes

Add indexes to frequently filtered fields:

```python
from ferro import Field, FerroField

class User(Model):
    email: Annotated[str, FerroField(unique=True, index=True)]
    status: Annotated[str, FerroField(index=True)]
```

### Batch Operations

Use bulk methods instead of loops:

```python
# Bad (N queries)
for user in users:
    user.is_active = False
    await user.save()

# Good (1 query)
await User.where(User.id.in_([u.id for u in users])).update(is_active=False)
```

### Avoid N+1 Queries

```python
# Bad (N+1 queries)
posts = await Post.all()
for post in posts:
    author = await post.author  # Separate query for each post!

# Good (prefetch if supported)
# Check your version's API for eager loading patterns
```

## SQL Injection Protection

All values passed to the query API are automatically parameterized by the Rust engine. User input is never concatenated into SQL strings:

```python
# Safe - parameterized automatically
username = request.get("username")  # User input
user = await User.where(User.username == username).first()

# Generates: SELECT * FROM users WHERE username = $1
# With parameter: [username]
```

## See Also

- [Mutations](mutations.md) - Creating, updating, and deleting records
- [Relationships](relationships.md) - Working with foreign keys
- [How-To: Pagination](../howto/pagination.md) - Efficient pagination patterns
- [Performance](../concepts/performance.md) - Query optimization techniques
