# How-To: Multiple Databases

!!! warning "Feature Not Implemented"
    **Multi-database support is not currently available in Ferro.** This documentation describes planned features. See [Coming Soon](../coming-soon.md#multiple-database-support) for more information.

    Ferro currently supports only a single database connection per application. The examples below show the planned API.

---

Connect to and query multiple databases in Ferro (planned feature).

## Basic Configuration (Planned)

The following shows the planned API for multi-database support:

```python
import ferro

async def setup():
    # Primary database
    await ferro.connect(
        "postgresql://localhost/main_db",
        name="primary"
    )

    # Read replica
    await ferro.connect(
        "postgresql://localhost/replica_db",
        name="replica",
        read_only=True
    )

    # Analytics database
    await ferro.connect(
        "postgresql://localhost/analytics_db",
        name="analytics"
    )
```

## Using Specific Databases

```python
# Default database (primary)
users = await User.all()

# Specific database
replica_users = await User.using("replica").all()
analytics_data = await Metric.using("analytics").all()
```

## Read/Write Splitting

```python
class User(Model):
    username: str

    @classmethod
    def read_query(cls):
        """Use replica for reads."""
        return cls.using("replica")

    @classmethod
    async def write(cls, **kwargs):
        """Use primary for writes."""
        return await cls.using("primary").create(**kwargs)

# Usage
users = await User.read_query().all()  # From replica
new_user = await User.write(username="alice")  # To primary
```

## See Also

- [Database Setup](../guide/database.md)
