# Mutations

Ferro provides efficient methods for creating, updating, and deleting records. All mutation operations are executed by the Rust engine for maximum performance.

## Creating Records

### Single Record

Use `.create()` to insert a single record:

```python
# Basic creation
user = await User.create(
    username="alice",
    email="alice@example.com",
    is_active=True
)

# Returns the created instance with populated fields (including generated IDs)
print(f"Created user ID: {user.id}")
```

### With Relationships

Create records with foreign key relationships:

```python
# Create author first
author = await User.create(username="bob", email="bob@example.com")

# Create post with relationship
post = await Post.create(
    title="My First Post",
    content="Hello world!",
    author=author  # Pass the model instance
)

# Or use the foreign key ID directly
post2 = await Post.create(
    title="Second Post",
    content="More content",
    author_id=author.id  # Use the shadow field
)
```

### Bulk Creation

For inserting many records efficiently, use `.bulk_create()`:

```python
# Create list of model instances
users = [
    User(username=f"user_{i}", email=f"user{i}@example.com")
    for i in range(1000)
]

# Insert all at once (single transaction)
await User.bulk_create(users)
```

**Performance benefits:**

- Single round-trip to database
- Batched INSERT statements
- Significantly faster than looping with `.create()`

!!! tip
    For optimal performance with very large batches (>10K records), consider breaking into chunks of 1K-5K records each.

### Default Values

Fields with default values are handled automatically:

```python
class User(Model):
    username: str
    is_active: bool = True  # Default value
    created_at: datetime = Field(default_factory=datetime.now)

# Don't need to specify defaults
user = await User.create(username="charlie")
# user.is_active is True
# user.created_at is set to current time
```

## Updating Records

### Instance-Level Updates

Modify an instance and call `.save()`:

```python
# Fetch a user
user = await User.where(User.username == "alice").first()

# Modify fields
user.email = "alice.new@example.com"
user.is_active = False

# Save changes
await user.save()
```

This generates an `UPDATE` statement for the modified record.

### Batch Updates

Update multiple records without loading them into memory:

```python
# Update all inactive users
count = await User.where(User.is_active == False).update(
    status="archived"
)
print(f"Updated {count} users")

# Update with expressions (if supported)
await Product.where(Product.category == "Electronics").update(
    price=Product.price * 0.9  # 10% discount
)
```

**Performance benefits:**

- No model instantiation overhead
- Single UPDATE query
- Efficient for large batches

### Atomic Operations

!!! warning "Feature Not Implemented"
    Atomic field increment/decrement operations are not yet available. See [Coming Soon](../coming-soon.md#atomic-field-updates) for workarounds.

**Workaround:**
```python
# Load, modify, and save
post = await Post.where(Post.id == post_id).first()
if post:
    post.view_count += 1
    await post.save()
```

### Updating Relationships

Change foreign key relationships:

```python
post = await Post.where(Post.id == 1).first()

# Change the author
new_author = await User.where(User.username == "carol").first()
post.author = new_author
await post.save()

# Or set the foreign key ID directly
post.author_id = new_author.id
await post.save()
```

## Deleting Records

### Single Record

Delete an instance:

```python
user = await User.where(User.username == "alice").first()
await user.delete()
```

### Batch Delete

Delete multiple records matching a query:

```python
# Delete all inactive users
count = await User.where(User.is_active == False).delete()
print(f"Deleted {count} users")

# Delete with multiple conditions
await Post.where(
    (Post.published == False) & (Post.created_at < old_date)
).delete()
```

### Cascade Behavior

Foreign key cascade behavior determines what happens to related records:

```python
from ferro import ForeignKey

# CASCADE (default): Delete related records
class Post(Model):
    author: Annotated[User, ForeignKey(related_name="posts", on_delete="CASCADE")]

# SET NULL: Set foreign key to NULL
class Post(Model):
    author: Annotated[
        User | None,
        ForeignKey(related_name="posts", on_delete="SET NULL")
    ] = None

# RESTRICT: Prevent deletion if related records exist
class Post(Model):
    author: Annotated[User, ForeignKey(related_name="posts", on_delete="RESTRICT")]
```

Examples:

```python
# CASCADE: Deleting user deletes all their posts
await user.delete()  # Posts are deleted automatically

# SET NULL: Deleting user sets post.author_id to NULL
await user.delete()  # Posts remain, author_id becomes NULL

# RESTRICT: Deleting user fails if they have posts
try:
    await user.delete()
except Exception:  # Use specific exception type from your driver
    print("Cannot delete user with existing posts")
```

### Soft Deletes

For a "soft delete" pattern (marking as deleted instead of removing):

```python
class User(Model):
    username: str
    is_deleted: bool = False
    deleted_at: datetime | None = None

# Soft delete
user.is_deleted = True
user.deleted_at = datetime.now()
await user.save()

# Query only non-deleted
active_users = await User.where(User.is_deleted == False).all()
```

See [How-To: Soft Deletes](../howto/soft-deletes.md) for full implementation patterns.

## Many-to-Many Operations

Many-to-many relationships have specialized mutators:

### Adding Links

```python
student = await Student.where(Student.name == "Alice").first()
math_course = await Course.where(Course.title == "Mathematics").first()
physics_course = await Course.where(Course.title == "Physics").first()

# Add single relationship
await student.courses.add(math_course)

# Add multiple relationships
await student.courses.add(math_course, physics_course)
```

### Removing Links

```python
# Remove single relationship
await student.courses.remove(math_course)

# Remove multiple relationships
await student.courses.remove(math_course, physics_course)
```

### Clearing All Links

```python
# Remove all relationships for this student
await student.courses.clear()
```

## Transaction Safety

All mutations are transaction-safe when used within a transaction context:

```python
from ferro import transaction

async with transaction():
    # Create user
    user = await User.create(username="dave", email="dave@example.com")

    # Create posts
    for i in range(3):
        await Post.create(
            title=f"Post {i}",
            content=f"Content {i}",
            author=user
        )

    # If any operation fails, all changes are rolled back
```

See [Transactions](transactions.md) for details.

## Best Practices

### Use Bulk Operations

```python
# Bad (N queries)
for i in range(100):
    await User.create(username=f"user_{i}", email=f"user{i}@example.com")

# Good (1 query)
users = [
    User(username=f"user_{i}", email=f"user{i}@example.com")
    for i in range(100)
]
await User.bulk_create(users)
```

### Avoid Unnecessary Saves

```python
# Bad (2 database hits)
user = await User.create(username="alice", email="alice@example.com")
user.is_active = True
await user.save()

# Good (1 database hit)
user = await User.create(
    username="alice",
    email="alice@example.com",
    is_active=True
)
```

### Use Batch Updates for Multiple Records

```python
# Bad (N queries)
users = await User.where(User.status == "pending").all()
for user in users:
    user.status = "active"
    await user.save()

# Good (1 query)
count = await User.where(User.status == "pending").update(status="active")
```

### Check Cascade Behavior

Always consider what happens to related records:

```python
# Before deleting, check for related records
post_count = await author.posts.count()
if post_count > 0:
    print(f"Warning: Deleting author will affect {post_count} posts")

await author.delete()
```

### Validate Before Bulk Operations

```python
# Validate all instances before bulk insert
users = [
    User(username=f"user_{i}", email=f"user{i}@example.com")
    for i in range(100)
]

# Pydantic validation happens automatically on model creation
# If any instance is invalid, an exception is raised before the database hit

await User.bulk_create(users)
```

## Error Handling

!!! note "Exception Types"
    The documentation references exception types like `IntegrityError` and `ValidationError`. These exceptions come from the underlying database driver or Pydantic. Import paths may vary. Catch general `Exception` or check your specific database driver's exceptions.

### Unique Constraint Violations

```python
try:
    await User.create(username="alice", email="existing@example.com")
except Exception as e:  # Use specific exception type from your driver
    print(f"User with this email already exists: {e}")
```

### Foreign Key Violations

```python
try:
    await Post.create(
        title="Orphan Post",
        author_id=99999  # Non-existent user
    )
except Exception as e:  # Use specific exception type from your driver
    print(f"Invalid author ID: {e}")
```

### Not Null Violations

```python
from pydantic import ValidationError

try:
    await User.create(username="bob")  # Missing required 'email'
except ValidationError as e:
    print(f"Validation failed: {e}")
```

## Performance Considerations

### Bulk Operations are Fast

Ferro's Rust engine optimizes bulk operations:

- 1K inserts: ~10-50ms (vs 500-1000ms looping)
- 10K inserts: ~100-300ms (vs 5-10 seconds looping)

### Batch Updates are Efficient

Updating via query is much faster than loading instances:

```python
# Slow: Loads 10K users into memory, updates each
users = await User.where(User.status == "old").all()  # 10K users
for user in users:
    user.status = "new"
    await user.save()  # 10K UPDATE queries

# Fast: Single UPDATE query, no memory overhead
await User.where(User.status == "old").update(status="new")
```

### Identity Map Awareness

Modified instances in the identity map are automatically synchronized:

```python
# Fetch user (stored in identity map)
user = await User.where(User.id == 1).first()

# Batch update
await User.where(User.id == 1).update(email="newemail@example.com")

# The in-memory instance is NOT automatically updated
# Refresh if needed:
await user.refresh()
```

## See Also

- [Queries](queries.md) - Fetching and filtering data
- [Transactions](transactions.md) - Atomic operations
- [Relationships](relationships.md) - Working with related records
- [How-To: Testing](../howto/testing.md) - Testing mutation operations
