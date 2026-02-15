# Type Safety

Ferro provides comprehensive type safety through deep integration with Pydantic V2 and Python's type system.

## Pydantic Integration

Ferro models ARE Pydantic models:

```python
from ferro import Model
from pydantic import BaseModel

class User(Model):
    username: str
    age: int

# User inherits from BaseModel
assert issubclass(User, BaseModel)  # True

# All Pydantic features work
user = User(username="alice", age=30)
print(user.model_dump())  # {"username": "alice", "age": 30}
print(user.model_dump_json())  # '{"username":"alice","age":30}'
```

## Runtime Validation

Pydantic validates all data at runtime:

```python
# Valid
user = User(username="alice", age=30)

# Invalid: type error
try:
    user = User(username="alice", age="thirty")
except ValidationError as e:
    print(e)
    # age: Input should be a valid integer

# Invalid: missing required field
try:
    user = User(username="alice")
except ValidationError as e:
    print(e)
    # age: Field required
```

## Static Type Checking

Ferro provides full type hints for static analyzers (mypy, pyright, pylance):

```python
from ferro import Model

class User(Model):
    username: str
    age: int

# Type checker knows return type
user: User = await User.get(1)

# Autocomplete works
user.username  # ✓ Known attribute
user.invalid   # ✗ Type error

# Query results are typed
users: list[User] = await User.all()
first: User | None = await User.first()
```

## IDE Autocomplete

Full IDE support with intelligent completions:

```python
user = await User.get(1)

# IDE suggests: username, age, save, delete, refresh, etc.
user.  # <autocomplete>

# Query builder is typed
User.where(
    User.  # <autocomplete: username, age, id>
)
```

## Field Type Validation

Ferro validates field types match database types:

```python
from datetime import datetime
from decimal import Decimal
from uuid import UUID

class Order(Model):
    id: UUID  # Validated as UUID
    amount: Decimal  # Validated as Decimal
    created_at: datetime  # Validated as datetime
```

## Custom Validators

Use Pydantic validators:

```python
from pydantic import field_validator

class User(Model):
    username: str
    email: str

    @field_validator('email')
    @classmethod
    def validate_email(cls, v: str) -> str:
        if '@' not in v:
            raise ValueError('Invalid email')
        return v.lower()

# Validation runs automatically
user = await User.create(
    username="alice",
    email="ALICE@EXAMPLE.COM"  # Normalized to lowercase
)
```

## Type Coercion

Pydantic coerces compatible types:

```python
class User(Model):
    age: int
    score: float

# Strings are coerced
user = User(age="30", score="95.5")
assert user.age == 30  # int
assert user.score == 95.5  # float
```

## Generic Types

Ferro supports complex generic types:

```python
from typing import Dict, List, Optional

class User(Model):
    tags: List[str]  # List of strings
    metadata: Dict[str, Any]  # Dictionary
    bio: Optional[str] = None  # Nullable
```

## See Also

- [Models & Fields](../guide/models-and-fields.md)
- [Architecture](architecture.md)
