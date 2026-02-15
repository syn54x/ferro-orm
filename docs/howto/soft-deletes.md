# How-To: Soft Deletes

Implement soft deletes to mark records as deleted without removing them from the database.

## Basic Implementation

```python
from datetime import datetime
from ferro import Model, Field

class SoftDeleteModel(Model):
    is_deleted: bool = False
    deleted_at: datetime | None = None

    async def soft_delete(self):
        """Mark as deleted instead of removing."""
        self.is_deleted = True
        self.deleted_at = datetime.now()
        await self.save()

    async def restore(self):
        """Restore a soft-deleted record."""
        self.is_deleted = False
        self.deleted_at = None
        await self.save()

class User(SoftDeleteModel):
    username: str
    email: str

# Usage
user = await User.create(username="alice", email="alice@example.com")

# Soft delete
await user.soft_delete()

# Restore
await user.restore()
```

## Query Only Active Records

```python
class User(SoftDeleteModel):
    username: str

    @classmethod
    def active(cls):
        """Query only non-deleted records."""
        return cls.where(cls.is_deleted == False)

# Usage
active_users = await User.active().all()
deleted_users = await User.where(User.is_deleted == True).all()
```

## Manager Pattern

```python
class SoftDeleteManager:
    def __init__(self, model):
        self.model = model

    def active(self):
        return self.model.where(self.model.is_deleted == False)

    def deleted(self):
        return self.model.where(self.model.is_deleted == True)

    def all_with_deleted(self):
        return self.model.select()

class User(SoftDeleteModel):
    username: str

    objects = SoftDeleteManager(lambda: User)

# Usage
active = await User.objects.active().all()
deleted = await User.objects.deleted().all()
```

## See Also

- [Mutations](../guide/mutations.md)
- [Queries](../guide/queries.md)
