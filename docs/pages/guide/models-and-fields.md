# Models & Fields

Models are the central building blocks of Ferro. They define your schema in plain Python type hints, validate data with Pydantic, and are mapped to database tables by the Rust engine.

## Defining a Model

Inherit from `ferro.Model` and declare fields with standard type annotations:

=== "Assignment"

    ```python
    --8<-- "docs/examples/quickstart.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/quickstart_annotated.py:models"
    ```

Every Ferro model is a full [Pydantic](https://docs.pydantic.dev/latest/) `BaseModel`, so validation, serialization (`model_dump()`, `model_dump_json()`), and `model_config` all work as you'd expect.

## Declaration Styles

Ferro's `Field()` merges database options (`primary_key`, `unique`, `index`, ...) with Pydantic's validation options in a single call. You can attach it in two equivalent ways:

=== "Assignment with Field()"

    Put defaults and options on the assignment side — the classic Pydantic style:

    ```python
    from decimal import Decimal

    from ferro import Field, Model


    class Product(Model):
        id: int | None = Field(default=None, primary_key=True)
        slug: str = Field(unique=True, index=True)
        name: str = Field(max_length=200, description="Display name")
        price: Decimal = Field(ge=0, decimal_places=2)
    ```

=== "Annotated metadata"

    Keep the plain type on the left and pass `Field(...)` inside `typing.Annotated`:

    ```python
    from decimal import Decimal
    from typing import Annotated

    from ferro import Field, Model


    class Product(Model):
        id: Annotated[int | None, Field(default=None, primary_key=True)]
        slug: Annotated[str, Field(unique=True, index=True)]
        name: Annotated[str, Field(max_length=200, description="Display name")]
        price: Annotated[Decimal, Field(ge=0, decimal_places=2)]
    ```

Both produce identical schemas — every example in these docs shows both styles in tabs; pick one and stay consistent within a project. The `Annotated` style keeps the bare type visible at a glance and is also how forward relationships are declared (`Annotated[Author, ForeignKey(...)]` — see [Relationships](relationships.md)).

!!! note "Advanced: `FerroField`"
    For a type-first style that carries only database flags (no Pydantic validation kwargs), you can attach `ferro.FerroField(...)` as `Annotated` metadata instead: `id: Annotated[int, FerroField(primary_key=True)]`. It accepts the same database options as `Field()`.

## Field Types

Ferro maps Python annotations to backend-appropriate column types:

| Python type | SQLite | PostgreSQL | Notes |
| :--- | :--- | :--- | :--- |
| `int` | `INTEGER` | `INTEGER` / `BIGINT` | |
| `str` | `TEXT` | `TEXT` | Override with `db_type=varchar(n)` |
| `bool` | `BOOLEAN` (0/1) | `BOOLEAN` | |
| `float` | `DOUBLE` | `DOUBLE PRECISION` | |
| `datetime.datetime` | `TEXT` (ISO 8601) | `TIMESTAMP` / `TIMESTAMPTZ` | |
| `datetime.date` | `TEXT` (ISO 8601) | `DATE` | |
| `datetime.time` | `TEXT` (ISO 8601) | `TIME` | |
| `uuid.UUID` | `TEXT` (36 chars) | `UUID` | |
| `decimal.Decimal` | `NUMERIC` | `NUMERIC` | For money and other exact values |
| `bytes` | `BLOB` | `BYTEA` | |
| `enum.Enum` | `TEXT` | native `ENUM` | See note below |
| `dict` / `list` | `TEXT` (JSON) | `JSON` / `JSONB` | |

### Overriding the column type with `db_type`

When the inferred type isn't what you want — for example `varchar(255)` instead of unbounded `TEXT` — pass a `db_type` token:

=== "Assignment"

    ```python
    from ferro import Field, Model, varchar


    class Document(Model):
        id: int | None = Field(default=None, primary_key=True)
        title: str = Field(db_type=varchar(255))
        body: str = Field(db_type="text")
    ```

=== "Annotated"

    ```python
    from typing import Annotated

    from ferro import Field, Model, varchar


    class Document(Model):
        id: Annotated[int | None, Field(default=None, primary_key=True)]
        title: Annotated[str, Field(db_type=varchar(255))]
        body: Annotated[str, Field(db_type="text")]
    ```

Valid values are the `DbTypeToken` literals — `"text"`, `"smallint"`, `"int"`, `"bigint"`, `"uuid"`, `"timestamp"`, `"timestamptz"`, `"date"`, `"time"` — plus `varchar(n)` built with `ferro.varchar`. Prefer `varchar(255)` over the raw string `"varchar(255)"` so type checkers see a deliberate vocabulary choice. The override is validated against the Python annotation at class-definition time, so an incompatible combination fails immediately.

!!! note "Enum storage"
    `enum.Enum` fields are stored as text on SQLite and as named native `ENUM` types on PostgreSQL (via the [Alembic bridge](migrations.md)). For closed-domain string columns with a DB-side `CHECK` constraint, combine `db_type` with `db_check=True`.

## Field Options

Database options accepted by `Field()` (and `FerroField()`):

| Option | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `primary_key` | `bool` | `False` | Mark this column as the table's primary key. |
| `autoincrement` | `bool \| None` | `None` | Database-generated values. Inferred `True` for integer primary keys; pass `False` for manual integer keys. |
| `unique` | `bool` | `False` | Single-column uniqueness constraint. For multi-column uniqueness see [Composite Constraints](#composite-constraints). |
| `index` | `bool` | `False` | Create a non-unique index on this column. |
| `nullable` | `"infer" \| bool` | `"infer"` | Column nullability. `"infer"` follows whether the annotation allows `None`; `True`/`False` force it (useful when the Python type diverges from the column on purpose). |
| `default` | any | — | Pydantic default value (also used to backfill when [`migrate_updates`](migrations.md) adds a NOT NULL column). |
| `default_factory` | callable | — | Pydantic default factory, e.g. `default_factory=datetime.now`. |
| `db_type` | `DbType \| None` | `None` | Column-type override (see above). |
| `db_check` | `bool` | `False` | Emit a DB-side `CHECK` constraint for closed-domain types; only valid with `db_type`. |

On top of these, every Pydantic validation option works in the same call: `min_length`, `max_length`, `pattern`, `gt`, `ge`, `lt`, `le`, `multiple_of`, `decimal_places`, `description`, and the rest — see [Pydantic's Field docs](https://docs.pydantic.dev/latest/api/fields/#pydantic.fields.Field).

## Primary Keys

### Auto-increment

Integer primary keys auto-increment by default. Declare them as `int | None` with `default=None` so unsaved instances can exist before the database assigns an ID:

=== "Assignment"

    ```python
    from ferro import Field, Model


    class User(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str
    ```

=== "Annotated"

    ```python
    from typing import Annotated

    from ferro import Field, Model


    class User(Model):
        id: Annotated[int | None, Field(default=None, primary_key=True)]
        name: str
    ```

After `await user.save()` (or `User.create(...)`), the generated ID is written back onto the instance.

### Manual

For natural keys, or integer keys you assign yourself, disable auto-increment:

=== "Assignment"

    ```python
    from ferro import Field, Model


    class Country(Model):
        code: str = Field(primary_key=True)  # natural key, e.g. "US"
        name: str


    class LegacyRecord(Model):
        id: int = Field(primary_key=True, autoincrement=False)
        payload: str
    ```

=== "Annotated"

    ```python
    from typing import Annotated

    from ferro import Field, Model


    class Country(Model):
        code: Annotated[str, Field(primary_key=True)]  # natural key, e.g. "US"
        name: str


    class LegacyRecord(Model):
        id: Annotated[int, Field(primary_key=True, autoincrement=False)]
        payload: str
    ```

### UUID primary keys

Generate UUIDs client-side with `default_factory`:

=== "Assignment"

    ```python
    import uuid

    from ferro import Field, Model


    class Order(Model):
        id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
        total_cents: int = 0
    ```

=== "Annotated"

    ```python
    import uuid
    from typing import Annotated

    from ferro import Field, Model


    class Order(Model):
        id: Annotated[uuid.UUID, Field(default_factory=uuid.uuid4, primary_key=True)]
        total_cents: int = 0
    ```

On PostgreSQL this is a native `UUID` column; on SQLite it is stored as a 36-character string.

## Composite Constraints

### Composite uniques

When a row must be unique across several columns *together* (e.g. one membership per `(user_id, org_id)` pair), per-column `unique=True` is not what you want. Declare the `ClassVar` `__ferro_composite_uniques__` instead:

=== "Assignment"

    ```python
    import uuid
    from typing import ClassVar

    from ferro import Field, Model


    class OrgMembership(Model):
        __ferro_composite_uniques__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("user_id", "org_id"),
        )

        id: int | None = Field(default=None, primary_key=True)
        user_id: uuid.UUID
        org_id: uuid.UUID
    ```

=== "Annotated"

    ```python
    import uuid
    from typing import Annotated, ClassVar

    from ferro import Field, Model


    class OrgMembership(Model):
        __ferro_composite_uniques__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("user_id", "org_id"),
        )

        id: Annotated[int | None, Field(default=None, primary_key=True)]
        user_id: uuid.UUID
        org_id: uuid.UUID
    ```

- Each inner tuple lists **database column names** (for a `ForeignKey` field named `user`, use the shadow column `user_id`).
- Declare several tuples for several independent composite uniques.
- Unknown column names raise at model registration time.

!!! note "NULL semantics"
    SQL `UNIQUE` treats `NULL` values as distinct from each other, so nullable members of a composite unique can admit multiple rows that differ only by `NULL`. Prefer `NOT NULL` columns in composite uniques when you need strict "at most one row per pair" semantics.

### Composite indexes

For non-unique multi-column indexes — read-path optimization on common filter combinations like `(user_id, created_at)` — declare `__ferro_composite_indexes__` with the same shape:

=== "Assignment"

    ```python
    from datetime import datetime
    from typing import ClassVar

    from ferro import Field, Model


    class Comment(Model):
        __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("user_id", "created_at"),
        )

        id: int | None = Field(default=None, primary_key=True)
        user_id: int
        created_at: datetime
        body: str
    ```

=== "Annotated"

    ```python
    from datetime import datetime
    from typing import Annotated, ClassVar

    from ferro import Field, Model


    class Comment(Model):
        __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("user_id", "created_at"),
        )

        id: Annotated[int | None, Field(default=None, primary_key=True)]
        user_id: int
        created_at: datetime
        body: str
    ```

Validation mirrors composite uniques: at least two columns per tuple, columns must exist, and **order is preserved** (it matters for leftmost-prefix optimization). For single-column indexes use `Field(index=True)`. Declaring the same ordered tuple in both `__ferro_composite_uniques__` and `__ferro_composite_indexes__` emits a `UserWarning` and drops the redundant index.

Both ClassVars flow through to [Alembic autogenerate](migrations.md) as matching `UniqueConstraint` / `Index` objects.

## Pydantic Validation

Ferro models *are* Pydantic models, so validation runs whenever an instance is constructed — including inside `Model.create(...)` and before `bulk_create(...)` hits the database:

=== "Assignment"

    ```python
    from ferro import Field, Model


    class Account(Model):
        id: int | None = Field(default=None, primary_key=True)
        username: str = Field(unique=True, min_length=3, max_length=50)
        email: str = Field(unique=True, pattern=r"^[\w\.-]+@[\w\.-]+\.\w+$")
        age: int = Field(ge=0, le=150)
    ```

=== "Annotated"

    ```python
    from typing import Annotated

    from ferro import Field, Model


    class Account(Model):
        id: Annotated[int | None, Field(default=None, primary_key=True)]
        username: Annotated[str, Field(unique=True, min_length=3, max_length=50)]
        email: Annotated[str, Field(unique=True, pattern=r"^[\w\.-]+@[\w\.-]+\.\w+$")]
        age: Annotated[int, Field(ge=0, le=150)]
    ```

```python
from pydantic import ValidationError

try:
    await Account.create(username="ab", email="not-an-email", age=-1)
except ValidationError as exc:
    print(exc.error_count(), "validation errors — nothing was written")
```

Custom `@field_validator` / `@model_validator` methods and `model_config` settings (e.g. `validate_assignment=True`, `str_strip_whitespace=True`) work too, since they are plain Pydantic features.

## Reusing Fields Across Models

!!! warning "Model subclasses cannot inherit fields"
    You **cannot** declare fields on a `Model` base class and inherit them in subclasses. `Model`'s metaclass replaces field names with query proxies at the class level, so a "base model with shared columns" silently produces broken subclass defaults. Don't do this:

    ```python
    class TimestampedModel(Model):  # WRONG — do not inherit fields from a Model
        created_at: datetime = Field(default_factory=datetime.now)


    class Article(TimestampedModel):  # broken defaults
        title: str
    ```

The supported pattern is a **plain mixin** (not a `Model` subclass) for shared *behavior*, with the fields declared on each concrete model:

=== "Assignment"

    ```python
    from datetime import UTC, datetime

    from ferro import Field, Model


    def utcnow() -> datetime:
        return datetime.now(UTC)


    class TimestampMixin:
        """Touch ``updated_at`` on every save. A plain class, not a Model."""

        async def save(self, **kwargs) -> None:
            self.updated_at = utcnow()
            await super().save(**kwargs)


    class Note(TimestampMixin, Model):
        id: int | None = Field(default=None, primary_key=True)
        text: str
        created_at: datetime = Field(default_factory=utcnow)
        updated_at: datetime = Field(default_factory=utcnow)


    class Task(TimestampMixin, Model):
        id: int | None = Field(default=None, primary_key=True)
        title: str
        created_at: datetime = Field(default_factory=utcnow)
        updated_at: datetime = Field(default_factory=utcnow)
    ```

=== "Annotated"

    ```python
    from datetime import UTC, datetime
    from typing import Annotated

    from ferro import Field, Model


    def utcnow() -> datetime:
        return datetime.now(UTC)


    class TimestampMixin:
        """Touch ``updated_at`` on every save. A plain class, not a Model."""

        async def save(self, **kwargs) -> None:
            self.updated_at = utcnow()
            await super().save(**kwargs)


    class Note(TimestampMixin, Model):
        id: Annotated[int | None, Field(default=None, primary_key=True)]
        text: str
        created_at: Annotated[datetime, Field(default_factory=utcnow)]
        updated_at: Annotated[datetime, Field(default_factory=utcnow)]


    class Task(TimestampMixin, Model):
        id: Annotated[int | None, Field(default=None, primary_key=True)]
        title: str
        created_at: Annotated[datetime, Field(default_factory=utcnow)]
        updated_at: Annotated[datetime, Field(default_factory=utcnow)]
    ```

The few repeated field lines are deliberate: each concrete model owns its full schema, and the mixin contributes behavior only. For complete worked examples, see [Timestamps](../howto/timestamps.md) and [Soft Deletes](../howto/soft-deletes.md).

## See Also

- [Relationships](relationships.md) — foreign keys, one-to-many, many-to-many
- [Queries](queries.md) — fetching and filtering data
- [Mutations](mutations.md) — creating, updating, and deleting records
- [Schema Migrations](migrations.md) — how fields become DDL
- [Identity Map](../concepts/identity-map.md) — instance caching semantics
