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

Use **`ferro.Field`** for database constraints (primary key, unique, index, and Pydantic validation). Pydantic merges `Field()` the same way whether you attach it on the right-hand side of `=` or inside `typing.Annotated[...]`; Ferro reads the resulting `FieldInfo` and does not require you to know internal details.

**Recommended patterns (pick one and stay consistent in a project):**

### Assignment pattern

Put defaults and Ferro options on the **assignment** side (classic Pydantic model field):

```python
from decimal import Decimal
from ferro import Field, Model

class Product(Model):
    sku: str = Field(primary_key=True)
    slug: str = Field(unique=True, index=True)
    name: str = Field(max_length=200, description="Display name")
    price: Decimal = Field(ge=0, decimal_places=2)
```

### Annotation pattern

Keep the **plain type** on the left and pass **`Field(...)`** as `Annotated` metadata (all defaults and DB flags live inside `Field`):

```python
from typing import Annotated
from decimal import Decimal
from ferro import Field, Model

class Product(Model):
    sku: Annotated[str, Field(primary_key=True)]
    slug: Annotated[str, Field(unique=True, index=True)]
    name: Annotated[str, Field(max_length=200, description="Display name")]
    price: Annotated[Decimal, Field(ge=0, decimal_places=2)]
```

### Advanced: `FerroField` in `Annotated`

For a type-first style without going through `Field()`, you can attach **`FerroField(...)`** as metadata. This is equivalent for Ferro’s database flags; you lose the single-call surface that combines Pydantic validation kwargs on `Field`.

```python
from typing import Annotated
from decimal import Decimal
from ferro import FerroField, Model

class Product(Model):
    sku: Annotated[str, FerroField(primary_key=True)]
    slug: Annotated[str, FerroField(unique=True, index=True)]
    price: Annotated[Decimal, FerroField(index=True)]
```

### Constraint parameters

All of the above support the same database constraint parameters on `Field` / `FerroField`:

| Parameter | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `primary_key` | `bool` | `False` | Marks the field as the primary key for the table. |
| `autoincrement` | `bool \| None` | `None` | If `True`, the database generates values automatically. Defaults to `True` for integer primary keys. |
| `unique` | `bool` | `False` | Enforces a **single-column** uniqueness constraint on that column only. For uniqueness on a combination of columns, see [Composite unique constraints](#composite-unique-constraints) below. |
| `index` | `bool` | `False` | Creates a database index for this column to improve query performance. |
| `nullable` | `"infer" \| bool` | `"infer"` | Controls Alembic `Column.nullable` emitted by `get_metadata()`. `"infer"` follows whether the Python annotation allows `None`; `True` / `False` force NULL / NOT NULL. |

#### Examples

**Primary key:**

```python
# Pydantic-style (preferred in docs)
id: int = Field(primary_key=True)
sku: str = Field(primary_key=True)  # natural key
```

For the **`Annotated[..., Field(...)]`** form, see the [Annotation pattern](#annotation-pattern) above.

**Autoincrement:**

```python
# Autoincrement is implied for integer primary keys
id: int = Field(primary_key=True)

# Explicit manual key (no autoincrement)
id: int = Field(primary_key=True, autoincrement=False)
```

**Unique constraints:**

```python
# Pydantic-style (preferred in docs)
email: str = Field(unique=True)
slug: str = Field(unique=True, index=True)
```

### Composite unique constraints

Sometimes a row should be unique **across several columns together** (for example one membership per `(user_id, org_id)` pair). That is a *composite* unique: in SQL this is typically expressed as `UNIQUE (user_id, org_id)` on the table, or an equivalent unique index on those columns.

Ferro does **not** use per-column `Field(unique=True)` (assignment or `Annotated[..., Field(unique=True)]`) or `FerroField(unique=True)` for that case—`unique=True` is only for a **single** column. Instead, set the **`typing.ClassVar`** `__ferro_composite_uniques__` on your model (the base `Model` defines it as `()` so IDEs and type checkers know the hook exists; subclasses override when needed):

```python
from typing import ClassVar
import uuid

from ferro import Field, Model

class OrgMembership(Model):
    __ferro_composite_uniques__: ClassVar[tuple[tuple[str, ...], ...]] = (
        ("user_id", "org_id"),
    )

    id: uuid.UUID | None = Field(default=None, primary_key=True)
    user_id: uuid.UUID = Field()
    org_id: uuid.UUID = Field()
```

- Each inner tuple lists **database column names** as they appear in the generated schema (the same names as your Pydantic fields for scalar columns; for `ForeignKey("user")` use `user_id` in the tuple).
- You can list several groups for multiple composite uniques on one model.
- Invalid or unknown column names raise when the model is registered.

**Null semantics (SQLite):** With the default local SQLite engine, `UNIQUE` treats `NULL` as distinct from other `NULL` values for multi-column constraints unless columns are `NOT NULL`. Ferro maps nullability from your types and defaults like other fields; optional composite columns can therefore allow multiple rows that differ only by `NULL` in a nullable column. Prefer `NOT NULL` on composite members when you need strict “at most one row per pair” semantics. Other databases can differ; consult your backend’s documentation when you target PostgreSQL, MySQL, and so on.

**Wire format:** Declarations use nested tuples in Python; the schema JSON sent to the Rust engine uses nested lists (`ferro_composite_uniques`) because JSON has no tuple type.

**Many-to-many join tables:** When you use `ManyToManyField` without a custom `through` table, Ferro creates a default join table with two foreign-key columns. That table automatically gets a composite unique on those two columns so the same link cannot be stored twice. If you already have duplicate rows in such a table, adding this constraint in a migration may require a data cleanup step first.

See also [Schema management / migrations](migrations.md) for how composite uniques appear in Alembic metadata.

**Indexes:**

```python
# Pydantic-style (preferred in docs)
created_at: datetime = Field(index=True)
status: str = Field(index=True)
```

## Pydantic Validation

With the **assignment** or **`Annotated[..., Field(...)]`** pattern, you can combine Ferro's database constraints with Pydantic's validation options in a single `Field()` call:

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
2. **Rust Registration**: The schema is serialized to a JSON-AST (including Ferro-specific keys such as `ferro_composite_uniques` when declared) and passed to the Rust core's `MODEL_REGISTRY`.
3. **Table Generation**: When `auto_migrate=True` is used or `create_tables()` is called, the Rust engine generates the appropriate SQL `CREATE TABLE` statements.

This architecture allows Ferro to leverage Rust's performance for SQL generation and row hydration while maintaining a pure Python interface.

## See Also

- [Relationships](relationships.md) - Foreign keys, one-to-many, many-to-many
- [Queries](queries.md) - Fetching and filtering data
- [Mutations](mutations.md) - Creating, updating, and deleting records
- [Identity Map](../concepts/identity-map.md) - Understanding instance caching
