# Fields

Ferro supports a wide range of Python types, automatically mapping them to the most efficient database types available in the Rust engine.

## Supported Types

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

## Field Metadata

Ferro supports two equivalent ways to configure database-level constraints:

1. `typing.Annotated[..., FerroField(...)]`
2. `ferro.Field(..., primary_key=..., unique=..., index=...)`

Use whichever style matches your codebase best.
Do not declare both styles on the same field.

### Option 1: `Annotated` + `FerroField`

```python
from typing import Annotated
from ferro import Model, FerroField

class Product(Model):
    sku: Annotated[str, FerroField(primary_key=True)]
    slug: Annotated[str, FerroField(unique=True, index=True)]
    price: Annotated[Decimal, FerroField(index=True)]
```

### Option 2: Wrapped `ferro.Field`

```python
from decimal import Decimal
from ferro import Field, Model

class Product(Model):
    sku: str = Field(primary_key=True)
    slug: str = Field(unique=True, index=True)
    price: Decimal = Field(index=True)
```

### Parameters

| Parameter | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `primary_key` | `bool` | `False` | Marks the field as the primary key for the table. |
| `autoincrement`| `bool \| None` | `None` | If `True`, the database generates values automatically. Defaults to `True` for integer primary keys. |
| `unique` | `bool` | `False` | Enforces a uniqueness constraint on the column. |
| `index` | `bool` | `False` | Creates a database index for this column to improve query performance. |

## Pydantic Integration

`ferro.Field` wraps Pydantic's `Field`, so all standard Pydantic validation and schema kwargs still apply:

```python
from ferro import Field, Model

class User(Model):
    username: str = Field(
        unique=True,
        min_length=3,
        max_length=50,
        description="Public handle"
    )
```

If you prefer `Annotated`, you can also compose `FerroField` with `pydantic.Field(...)` metadata in the same annotation.
