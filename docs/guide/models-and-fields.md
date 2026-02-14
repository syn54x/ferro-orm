# Models & Fields

Models are the central building blocks of Ferro. They define your data schema in Python and are automatically mapped to database tables by the Rust engine.

## Defining a Model

To create a model, inherit from `ferro.Model`. Models use standard Python type hints, leveraging Pydantic V2 for validation and serialization.

### Basic model example

```python
from ferro import Model

class User(Model):
    id: int
    username: str
    is_active: bool = True
```

## Field Types

Ferro supports a wide range of Python types, automatically mapping them to the most efficient database types available in the Rust engine.

| Python Type | Database Type (General) | Notes |
| :--- | :--- | :--- |
| `int` | `INTEGER` | |
| `str` | `TEXT` / `VARCHAR` | |
| `bool` | `BOOLEAN` / `INTEGER` | Stored as 0/1 in SQLite. |
| `float` | `DOUBLE` / `FLOAT` | |
| `datetime` | `DATETIME` / `TIMESTAMP` | Use `datetime.datetime` with timezone awareness. |
| `date` | `DATE` | Use `datetime.date`. |
| `UUID` | `UUID` / `TEXT` | Stored as a 36-character string if native UUID is unavailable. |
| `Decimal` | `NUMERIC` / `DECIMAL` | Use `decimal.Decimal` for high-precision financial data. |
| `bytes` | `BLOB` / `BYTEA` | Stored as binary data. |
| `Enum` | `ENUM` / `TEXT` | Python `enum.Enum` (typically string-backed). |
| `dict` / `list` | `JSON` / `JSONB` | Stored as JSON strings in SQLite. |

## Field Constraints

Ferro provides two equivalent API styles for declaring database constraints like primary keys, unique constraints, and indexes. Choose one style and use it consistently throughout your codebase.

### Pydantic-style: `ferro.Field`

If you're already familiar with Pydantic's `Field()`, this style will feel natural. You get all of Pydantic's validation options plus Ferro's database constraints.

```python
from ferro import Field, Model

class Product(Model):
    sku: str = Field(primary_key=True)
    slug: str = Field(unique=True, index=True)
    name: str = Field(max_length=200, description="Display name")
    price: Decimal = Field(ge=0, decimal_places=2)
```

### Annotated-style: `FerroField`

This type-first approach keeps the field type explicit and separates Ferro-specific constraints from Pydantic metadata.

```python
from typing import Annotated
from decimal import Decimal
from ferro import Model, FerroField

class Product(Model):
    sku: Annotated[str, FerroField(primary_key=True)]
    slug: Annotated[str, FerroField(unique=True, index=True)]
    price: Annotated[Decimal, FerroField(index=True)]
```

### Constraint parameters

Both styles support the same database constraint parameters:

| Parameter | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `primary_key` | `bool` | `False` | Marks the field as the primary key for the table. |
| `autoincrement` | `bool \| None` | `None` | If `True`, the database generates values automatically. Defaults to `True` for integer primary keys. |
| `unique` | `bool` | `False` | Enforces a uniqueness constraint on the column. |
| `index` | `bool` | `False` | Creates a database index for this column to improve query performance. |

#### Examples

**Primary key:**

```python
# Pydantic-style
id: int = Field(primary_key=True)
sku: str = Field(primary_key=True)  # natural key

# Annotated-style
id: Annotated[int, FerroField(primary_key=True)]
```

**Autoincrement:**

```python
# Autoincrement is implied for integer primary keys
id: int = Field(primary_key=True)

# Explicit manual key (no autoincrement)
id: int = Field(primary_key=True, autoincrement=False)
```

**Unique constraints:**

```python
# Pydantic-style
email: str = Field(unique=True)
slug: str = Field(unique=True, index=True)

# Annotated-style
email: Annotated[str, FerroField(unique=True)]
```

**Indexes:**

```python
# Pydantic-style
created_at: datetime = Field(index=True)
status: str = Field(index=True)

# Annotated-style
created_at: Annotated[datetime, FerroField(index=True)]
```

## Pydantic Validation

When using the Pydantic-style API, you can combine Ferro's database constraints with Pydantic's validation options in a single `Field()` call:

```python
from ferro import Field, Model

class User(Model):
    username: str = Field(
        unique=True,           # Ferro: database constraint
        min_length=3,          # Pydantic: validation
        max_length=50,
        description="Public handle"
    )
    age: int = Field(ge=0, le=150)
    email: str = Field(
        unique=True,
        pattern=r'^[\w\.-]+@[\w\.-]+\.\w+$'
    )
```

All Pydantic `Field` parameters work as expected. See [Pydantic's Field documentation](https://docs.pydantic.dev/latest/api/fields/#pydantic.fields.Field) for the complete list.

## Model Configuration

Since Ferro models are Pydantic models, you can use the `model_config` attribute to control validation and serialization behaviors:

```python
from pydantic import ConfigDict
from ferro import Model

class Product(Model):
    model_config = ConfigDict(
        str_strip_whitespace=True,
        validate_assignment=True,
        extra='forbid'
    )

    sku: str
    name: str
```

## Internal Mechanics

Ferro uses a custom `ModelMetaclass` to bridge Python and Rust:

1. **Schema Capture**: When you define a class, the metaclass inspects its fields and constraints.
2. **Rust Registration**: The schema is serialized to a JSON-AST and passed to the Rust core's `MODEL_REGISTRY`.
3. **Table Generation**: When `auto_migrate=True` is used or `create_tables()` is called, the Rust engine generates the appropriate SQL `CREATE TABLE` statements.

This architecture allows Ferro to leverage Rust's performance for SQL generation and row hydration while maintaining a pure Python interface.

## See Also

- [Relationships](relationships.md) - Foreign keys, one-to-many, many-to-many
- [Queries](queries.md) - Fetching and filtering data
- [Mutations](mutations.md) - Creating, updating, and deleting records
- [Identity Map](../concepts/identity-map.md) - Understanding instance caching
