# Coming Soon

This page lists features that are documented but not yet fully implemented in Ferro. These features are planned for future releases.

!!! info "Work in Progress"
    The features listed below are referenced in the documentation but are not currently available. Check back for updates or follow the [GitHub repository](https://github.com/syn54x/ferro-orm) for progress.

## Query Features

### Case-Insensitive LIKE (ilike)

**Status:** Not Implemented

**Documentation References:**
- `docs/guide/queries.md` (lines 248-249)

**Description:**
Case-insensitive pattern matching with `ilike()` method.

**Example (Not Working):**
```python
# This does not work yet
users = await User.where(User.email.ilike("%EXAMPLE.COM")).all()
```

**Workaround:**
Use standard `like()` with lowercase conversion or database-specific functions.

---

### NOT IN Operator (not_in)

**Status:** Not Implemented

**Documentation References:**
- `docs/guide/queries.md` (lines 235-236)

**Description:**
A `not_in_()` method for excluding values from a list.

**Example (Not Working):**
```python
# This does not work yet
banned_users = await User.where(User.status.not_in_(["banned", "suspended"])).all()
```

**Workaround:**
Use negation with `&` and `!=` operators:
```python
banned_users = await User.where(
    (User.status != "banned") & (User.status != "suspended")
).all()
```

---

### Raw SQL Queries

**Status:** Not Implemented

**Documentation References:**
- `docs/guide/queries.md` (lines 252-266)

**Description:**
Direct raw SQL query execution with parameterization.

**Example (Not Working):**
```python
# This does not work yet
from ferro import raw_query

results = await raw_query(
    "SELECT * FROM users WHERE age > $1 AND status = $2",
    18,
    "active"
)
```

**Workaround:**
Use the query builder API for all queries.

---

### Eager Loading / Prefetch Related

**Status:** Not Implemented

**Documentation References:**
- `docs/guide/queries.md` (lines 211-214, 318-321)

**Description:**
Eager loading of relationships to avoid N+1 queries.

**Example (Not Working):**
```python
# This does not work yet
posts = await Post.select().prefetch_related("author").all()
```

**Workaround:**
Manually load relationships as needed. Be mindful of N+1 query patterns.

---

### Select Specific Fields

**Status:** Not Implemented

**Documentation References:**
- `docs/guide/queries.md` (lines 174-181)

**Description:**
Loading only specific fields instead of all model fields.

**Example (Not Working):**
```python
# This does not work yet
users = await User.select(User.id, User.username).all()
```

**Workaround:**
Load full models and access only the fields you need.

---

### Aggregation Functions

**Status:** Partially Implemented

**Documentation References:**
- `docs/guide/queries.md` (lines 166-172)

**Description:**
Only `.count()` is implemented. Other aggregations like `sum()`, `avg()`, `min()`, `max()` are not available.

**Example (Partially Working):**
```python
# Works
total_users = await User.count()

# Does NOT work yet
total_sales = await Order.sum(Order.amount)
avg_price = await Product.avg(Product.price)
```

**Workaround:**
Use `.count()` or load all records and compute aggregations in Python.

---

### Atomic Field Updates

**Status:** Not Implemented

**Documentation References:**
- `docs/guide/mutations.md` (lines 129-139)

**Description:**
Database-level atomic increment/decrement operations.

**Example (Not Working):**
```python
# This does not work yet
await Post.where(Post.id == post_id).update(
    view_count=Post.view_count + 1
)
```

**Workaround:**
Load the instance, modify it, and save:
```python
post = await Post.where(Post.id == post_id).first()
if post:
    post.view_count += 1
    await post.save()
```

---

## Database Connection Features

### disconnect()

**Status:** Not Implemented

**Documentation References:**
- `docs/guide/database.md` (lines 217-232)

**Description:**
Graceful database disconnection for shutdown hooks.

**Example (Not Working):**
```python
# This does not work yet
await ferro.disconnect()
```

**Workaround:**
Connection cleanup is handled automatically on process exit.

---

### check_connection()

**Status:** Not Implemented

**Documentation References:**
- `docs/guide/database.md` (lines 151-164)

**Description:**
Health check function to verify database connectivity.

**Example (Not Working):**
```python
# This does not work yet
from ferro import check_connection

is_connected = await check_connection()
```

**Workaround:**
Attempt a simple query to verify connectivity:
```python
try:
    await User.select().limit(1).all()
    is_connected = True
except Exception:
    is_connected = False
```

---

### connection_context()

**Status:** Not Implemented

**Documentation References:**
- `docs/guide/database.md` (lines 166-179)

**Description:**
Request-scoped connection context manager.

**Example (Not Working):**
```python
# This does not work yet
from ferro import connection_context

async def handle_request():
    async with connection_context():
        user = await User.create(username="alice")
        await Post.create(title="Hello", author=user)
```

**Workaround:**
Use `transaction()` context manager for scoped database operations.

---

### Connection Pool Configuration

**Status:** Partially Implemented

**Documentation References:**
- `docs/guide/database.md` (lines 76-104)

**Description:**
Advanced connection pool parameters like `max_connections`, `min_connections`, and `connect_timeout`.

**Example (Partially Working):**
```python
# Support for these parameters is not confirmed
await ferro.connect(
    "postgresql://user:password@localhost/dbname",
    max_connections=20,      # May not work
    min_connections=5,       # May not work
    connect_timeout=30       # May not work
)
```

**Workaround:**
Use basic connection string without advanced pool parameters.

---

### Multiple Database Support

**Status:** Not Implemented

**Documentation References:**
- `docs/guide/database.md` (lines 123-149)
- `docs/howto/multiple-databases.md` (entire file)

**Description:**
Connecting to and querying multiple databases with named connections.

**Example (Not Working):**
```python
# This does not work yet
await ferro.connect("postgresql://localhost/main_db", name="primary")
await ferro.connect("postgresql://localhost/replica_db", name="replica")

# Query specific database
users = await User.using("replica").all()
```

**Workaround:**
Ferro currently supports only a single database connection per application.

---

## Transaction Features

### Nested Transactions / Savepoints

**Status:** Not Implemented

**Documentation References:**
- `docs/guide/transactions.md` (lines 91-106)

**Description:**
True nested transaction support with savepoints.

**Current Behavior:**
Nested `transaction()` blocks participate in the outermost transaction.

**Example (Not Working as Described):**
```python
async with transaction():  # Outer transaction
    await User.create(username="alice")

    async with transaction():  # Should be a savepoint, but isn't
        await Post.create(title="Hello")
    # Partial rollback not supported
```

**Workaround:**
Avoid nesting transactions. Structure code to use a single transaction scope.

---

## Migration Features

### Alembic Integration Details

**Status:** Partially Documented

**Documentation References:**
- `docs/guide/migrations.md` (entire file)

**Description:**
The migration guide references `ferro.migrations.get_metadata()` and assumes full Alembic integration.

**Verification Needed:**
```python
# Check if this import works
from ferro.migrations import get_metadata

target_metadata = get_metadata()
```

**Note:** Verify that `ferro-orm[alembic]` installation provides the necessary migration bridge.

---

## Model Features

### Model.count() Class Method

**Status:** Implemented, but Usage Unclear

**Documentation References:**
- `docs/getting-started/tutorial.md` (line 135)

**Description:**
The tutorial shows `await Post.count()` being called directly on the model class.

**Verification:**
```python
# This should work
total_posts = await Post.select().count()

# Check if this shorthand works
total_posts = await Post.count()
```

---

## Error Handling

### Specific Exception Types

**Status:** Not Confirmed

**Documentation References:**
- `docs/guide/mutations.md` (lines 380-408)
- `docs/guide/database.md` (lines 266-276)

**Description:**
Documentation references `IntegrityError`, `ValidationError`, and `ConnectionError` without imports.

**Example (Import Path Unknown):**
```python
# Import path not documented
try:
    await User.create(username="alice", email="duplicate@example.com")
except IntegrityError as e:  # Where does this come from?
    print(f"Integrity error: {e}")
```

**Clarification Needed:**
Document the exception hierarchy and import paths:
- Are these from `ferro` package?
- Re-exported from Pydantic?
- Database-driver specific?

---

## Relationship Features

### Many-to-Many Join Table Creation

**Status:** Partially Implemented

**Documentation References:**
- `docs/guide/relationships.md` (lines 176-289)

**Description:**
Many-to-many relationships are defined with `ManyToManyField`, but the join tables are not automatically created during `auto_migrate=True`.

**Example (Partially Working):**
```python
class Post(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    tags: Annotated[list["Tag"], ManyToManyField(related_name="posts")] = None

class Tag(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    posts: BackRef[list["Post"]] = None

# Models created, but join table 'post_tags' is NOT auto-created
# This causes errors when trying to use M2M methods:
await post.tags.add(tag)  # RuntimeError: no such table: post_tags
```

**Workaround:**
Manual join table creation may be required, or use Alembic migrations. Further investigation needed.

**Test Status:** 4 tests skipped in `tests/test_documentation_features.py`

---

### One-to-One Automatic Behavior

**Status:** Documented but Verify

**Documentation References:**
- `docs/guide/relationships.md` (lines 154-162)

**Description:**
Documentation states that one-to-one reverse relations automatically return a single object instead of a Query.

**Example (Verify Behavior):**
```python
class User(Model):
    id: int
    profile: BackRef["Profile"] = None

class Profile(Model):
    id: int
    user: Annotated[User, ForeignKey(related_name="profile", unique=True)]

user = await User.where(User.id == 1).first()

# Does this return Profile | None directly?
# Or does it return Query[Profile]?
profile = await user.profile
```

**Action:** Verify with tests that unique ForeignKey creates this behavior.

---

## Summary

### Definitely Not Implemented
1. `ilike()` - case-insensitive LIKE
2. `not_in_()` - NOT IN operator
3. Raw SQL queries (`raw_query`)
4. Eager loading (`prefetch_related`)
5. Select specific fields (partial model loading)
6. Aggregation functions (sum, avg, min, max)
7. Atomic field updates (e.g., `view_count + 1`)
8. `disconnect()` function
9. `check_connection()` function
10. `connection_context()` context manager
11. Connection pool advanced parameters
12. Multiple database support (`.using()`)
13. Nested transactions / savepoints

### Needs Verification
1. `Model.count()` class method shorthand
2. Exception types and import paths
3. One-to-one automatic single object return
4. Alembic integration (ferro.migrations module)

### Next Steps

If you encounter any issues with documented features:

1. **Check GitHub Issues**: [ferro-orm/issues](https://github.com/syn54x/ferro-orm/issues)
2. **Report Missing Features**: Open an issue if a documented feature doesn't work
3. **Use Workarounds**: See the workarounds provided above for each feature

**Want to contribute?** Check the [Contributing Guide](contributing.md) to help implement these features.
