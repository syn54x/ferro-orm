# Schema Management

Ferro integrates with **Alembic**, the industry-standard migration tool for Python, to provide robust and reliable schema management for production environments.

## Why Alembic?

Instead of reinventing migrations, Ferro leverages Alembic's battle-tested workflow. Ferro provides a bridge that translates your models into SQLAlchemy metadata, which Alembic uses to detect schema changes.

## Installation

Install Ferro with Alembic support:

```bash
pip install "ferro-orm[alembic]"
```

This installs Alembic and SQLAlchemy (used only for migration generation, not at runtime).

## Quick Start

### 1. Initialize Alembic

In your project root:

```bash
alembic init migrations
```

This creates:
```
your_project/
├── migrations/
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
└── alembic.ini
```

### 2. Configure env.py

Edit `migrations/env.py` to connect Ferro models to Alembic:

```python
# migrations/env.py
from ferro.migrations import get_metadata

# Import all models to register them
from myapp.models import User, Post, Comment

# Ferro generates SQLAlchemy metadata from registered models
target_metadata = get_metadata()

# Rest of env.py remains unchanged
```

### 3. Generate Your First Migration

```bash
alembic revision --autogenerate -m "Initial schema"
```

Alembic compares your models to the database and generates a migration script in `migrations/versions/`.

### 4. Review the Migration

Open the generated file in `migrations/versions/xxxx_initial_schema.py`:

```python
def upgrade():
    op.create_table('users',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('username', sa.String(), nullable=False),
        sa.Column('email', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('username'),
        sa.UniqueConstraint('email')
    )
    # ... more tables

def downgrade():
    op.drop_table('users')
    # ... reverse operations
```

**Always review generated migrations** for correctness.

### 5. Apply the Migration

```bash
alembic upgrade head
```

Your database now matches your models.

## Workflow

The typical development workflow:

1. **Modify models** in Python
2. **Generate migration**: `alembic revision --autogenerate -m "Description"`
3. **Review migration** in `migrations/versions/`
4. **Apply migration**: `alembic upgrade head`
5. **Commit migration** to version control

## Common Operations

### Check Current Version

```bash
alembic current
```

### View Migration History

```bash
alembic history --verbose
```

### Upgrade to Specific Version

```bash
alembic upgrade <revision>

# Examples
alembic upgrade +1        # Upgrade one version
alembic upgrade abc123    # Upgrade to specific revision
alembic upgrade head      # Upgrade to latest
```

### Downgrade (Rollback)

```bash
alembic downgrade -1      # Downgrade one version
alembic downgrade abc123  # Downgrade to specific revision
alembic downgrade base    # Downgrade to empty database
```

### Create Empty Migration

For custom SQL or data migrations:

```bash
alembic revision -m "Add admin user"
```

Edit the generated file:

```python
def upgrade():
    # Custom SQL
    op.execute("""
        INSERT INTO users (username, email, role)
        VALUES ('admin', 'admin@example.com', 'admin')
    """)

def downgrade():
    op.execute("DELETE FROM users WHERE username = 'admin'")
```

## Production Workflow

### Development

1. Develop features with models
2. Generate migrations
3. Test migrations locally
4. Commit migrations to git

### Staging

1. Pull latest code
2. Run `alembic upgrade head`
3. Test application

### Production

1. **Backup database** before migrations
2. Review migration scripts
3. Run migrations:
   ```bash
   alembic upgrade head
   ```
4. Monitor application

### Rollback Strategy

Keep rollback migrations tested:

```bash
# Test upgrade
alembic upgrade head

# Test downgrade
alembic downgrade -1

# Upgrade again
alembic upgrade head
```

## Precision Mapping

Ferro's migration bridge ensures high fidelity between your models and the database:

### Nullability

```python
# Required field
username: str
# → NOT NULL column

# Optional field
bio: str | None = None
# → NULL allowed
```

### Complex Types

```python
from decimal import Decimal
from datetime import datetime
from uuid import UUID
import enum

class UserRole(enum.Enum):
    USER = "user"
    ADMIN = "admin"

class User(Model):
    # Maps to DECIMAL/NUMERIC
    balance: Decimal

    # Maps to TIMESTAMP
    created_at: datetime

    # Maps to UUID (or TEXT in SQLite)
    id: UUID

    # Maps to ENUM (or TEXT in SQLite)
    role: UserRole

    # Maps to JSON/JSONB
    metadata: dict
```

### Constraints

```python
from ferro import Field, FerroField

class Product(Model):
    # PRIMARY KEY
    id: Annotated[int, FerroField(primary_key=True)]

    # UNIQUE constraint
    sku: Annotated[str, FerroField(unique=True)]

    # INDEX
    category: Annotated[str, FerroField(index=True)]
```

### Foreign Keys

```python
class Post(Model):
    author: Annotated[User, ForeignKey(related_name="posts")]
    # → FOREIGN KEY (author_id) REFERENCES users(id)

    # With cascade
    author: Annotated[User, ForeignKey(
        related_name="posts",
        on_delete="CASCADE"
    )]
    # → FOREIGN KEY ... ON DELETE CASCADE
```

### Many-to-Many

```python
class Student(Model):
    courses: Annotated[list["Course"], ManyToManyField(related_name="students")]

# Automatically generates join table:
# CREATE TABLE student_courses (
#     student_id INT REFERENCES students(id),
#     course_id INT REFERENCES courses(id),
#     PRIMARY KEY (student_id, course_id)
# )
```

## Data Migrations

For migrations that modify data (not just schema):

```bash
alembic revision -m "Migrate user roles"
```

```python
from alembic import op
import sqlalchemy as sa

def upgrade():
    # Schema change
    op.add_column('users', sa.Column('role', sa.String(), nullable=True))

    # Data migration
    connection = op.get_bind()
    connection.execute(
        "UPDATE users SET role = 'user' WHERE role IS NULL"
    )

    # Make non-nullable after populating
    op.alter_column('users', 'role', nullable=False)

def downgrade():
    op.drop_column('users', 'role')
```

## Zero-Downtime Migrations

For production systems that can't tolerate downtime:

### 1. Additive Changes First

```python
# Step 1: Add new column (nullable)
def upgrade():
    op.add_column('users', sa.Column('new_email', sa.String(), nullable=True))

# Deploy application that writes to both old and new columns
# Wait for all instances to deploy

# Step 2: Migrate data
def upgrade():
    connection = op.get_bind()
    connection.execute("UPDATE users SET new_email = email WHERE new_email IS NULL")

# Step 3: Make non-nullable, drop old column
def upgrade():
    op.alter_column('users', 'new_email', nullable=False)
    op.drop_column('users', 'email')
    op.alter_column('users', 'new_email', new_column_name='email')
```

### 2. Feature Flags

Use feature flags to control when code uses new schema:

```python
if feature_enabled("new_email_column"):
    user.new_email = email
else:
    user.email = email
```

## Troubleshooting

### Migration Not Detected

```python
# Ensure models are imported in env.py
from myapp.models import *  # Import all models

# Verify metadata generation
target_metadata = get_metadata()
print(target_metadata.tables)  # Should list your tables
```

### Conflicting Migrations

```bash
# Error: Multiple head revisions
# Solution: Merge migrations
alembic merge heads -m "Merge migrations"
```

### Manual Schema Changes

```bash
# If you manually modified the database, stamp it
alembic stamp head
```

### Reset Migrations

```bash
# Delete all migration files
rm migrations/versions/*.py

# Drop all tables
# Then regenerate from scratch
alembic revision --autogenerate -m "Initial schema"
alembic upgrade head
```

## Best Practices

1. **Always review** generated migrations
2. **Test migrations** locally before production
3. **Backup database** before running migrations
4. **Keep migrations small** and focused
5. **Don't edit** applied migrations (create new ones)
6. **Version control** all migration files
7. **Test rollback** (downgrade) functionality
8. **Use descriptive names** for migrations

## See Also

- [Database Setup](database.md) - Connection configuration
- [Models & Fields](models-and-fields.md) - Model definitions
- [How-To: Testing](../howto/testing.md) - Testing with migrations
