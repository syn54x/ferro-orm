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

## FerroField Metadata

To configure database-level constraints and behaviors, use the `FerroField` metadata container. The preferred way to apply this is via `typing.Annotated`.

```python
from typing import Annotated
from ferro import Model, FerroField

class Product(Model):
    sku: Annotated[str, FerroField(primary_key=True)]
    slug: Annotated[str, FerroField(unique=True, index=True)]
    price: Annotated[Decimal, FerroField(index=True)]
```

### Parameters

| Parameter | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `primary_key` | `bool` | `False` | Marks the field as the primary key for the table. |
| `autoincrement`| `bool \| None` | `None` | If `True`, the database generates values automatically. Defaults to `True` for integer primary keys. |
| `unique` | `bool` | `False` | Enforces a uniqueness constraint on the column. |
| `index` | `bool` | `False` | Creates a database index for this column to improve query performance. |

## Pydantic Integration

Since Ferro is built on Pydantic, all standard Pydantic validation and field configuration still apply.

```python
from pydantic import Field
from ferro import Model, FerroField

class User(Model):
    # Combine Ferro metadata with Pydantic validation
    username: Annotated[
        str,
        FerroField(unique=True),
        Field(min_length=3, max_length=50)
    ]
```
