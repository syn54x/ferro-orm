# How-To: Timestamps

Add automatic timestamp tracking to your models.

## Basic Pattern

```python
from datetime import datetime
from ferro import Model, Field

class TimestampedModel(Model):
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

class User(TimestampedModel):
    username: str
    email: str

# Usage
user = await User.create(username="alice", email="alice@example.com")
print(f"Created at: {user.created_at}")
```

## Auto-Updating updated_at

```python
class TimestampedModel(Model):
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    async def save(self):
        """Override save to update timestamp."""
        self.updated_at = datetime.now()
        await super().save()

# Usage
user = await User.where(User.id == 1).first()
user.username = "new_name"
await user.save()  # updated_at automatically set
```

## Timezone-Aware Timestamps

```python
from datetime import datetime, timezone

def utc_now():
    return datetime.now(timezone.utc)

class Model(Model):
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
```

## See Also

- [Models & Fields](../guide/models-and-fields.md)
