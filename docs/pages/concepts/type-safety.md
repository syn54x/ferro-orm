# Type Safety

Ferro is built on Pydantic V2 and Python's type system. Models validate at runtime, queries return precisely typed results, and the Rust boundary ships with type stubs — so both your IDE and your type checker understand Ferro code.

## Pydantic at the Core

Ferro models *are* Pydantic models, not Pydantic-flavored lookalikes:

```python
from pydantic import BaseModel

from ferro import Model


class User(Model):
    username: str
    age: int


assert issubclass(User, BaseModel)

user = User(username="alice", age=30)
print(user.model_dump())       # {'username': 'alice', 'age': 30}
print(user.model_dump_json())  # '{"username":"alice","age":30}'
```

Everything Pydantic gives you — `model_dump`, `model_validate`, JSON schema generation, serialization config — works unchanged. And validation runs on the write paths: `Model(...)` construction, `Model.create(...)`, and `instance.save()` all go through Pydantic before any data reaches the database.

```python
from pydantic import ValidationError

try:
    User(username="alice", age="not a number")
except ValidationError as e:
    print(e)  # age: Input should be a valid integer
```

## Static Typing

Ferro's public API carries full type hints, so static checkers (mypy, Pyright, Pylance) and your IDE know exactly what each call returns:

```python
# get() returns the model — and raises ModelDoesNotExist if missing
user: User = await User.get(1)

# get_or_none() makes absence explicit
maybe_user: User | None = await User.get_or_none(999)

# Query terminals are typed
users: list[User] = await User.all()
first: User | None = await User.where(lambda t: t.age >= 18).first()
n: int = await User.where(lambda t: t.age >= 18).count()
present: bool = await User.where(lambda t: t.username == "alice").exists()
```

Because results are real model instances with annotated fields, downstream code is checked too: `user.username` is a `str`, `user.age` is an `int`, and `user.nonexistent` is a type error.

## IDE Support

The same annotations drive autocomplete:

- Model instances complete their fields and methods (`save`, `delete`, `refresh`, ...).
- `Model.where(...)`, `.order_by(...)`, `.limit(...)` chains preserve the model type, so `await User.where(...).first()` completes `User` attributes on the result.
- Field names complete when you type `User.` inside a query expression.

No plugins are required — Ferro deliberately stays plugin-free and relies on standard typing constructs (`Self`, generics, overloads).

## The Rust Boundary

The compiled extension module can't be introspected by type checkers, so Ferro ships a stub file (`src/ferro/_core.pyi`) describing every FFI function — `connect`, `create_tables`, `migrate`, the fetch/save primitives, transaction control, and raw SQL entry points. The package is also marked with `py.typed`, so type checkers pick all of this up automatically when you depend on `ferro-orm`.

In practice you rarely touch `ferro._core` directly; the typed Python layer (`Model`, `Query`, `connect`, `transaction`) is the public API, and the stub exists so that even the boundary itself is checkable.

## Validators and Coercion

Because models are Pydantic models, the full validator toolbox applies:

```python
from pydantic import field_validator

from ferro import Model


class Account(Model):
    username: str
    email: str

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        if "@" not in v:
            raise ValueError("Invalid email")
        return v.lower()
```

Validators run on construction and on `create`/`save`, so invalid data is rejected before it is written. Pydantic's coercion rules also apply — `Account(username="a", email="A@B.COM")` stores `"a@b.com"`, and numeric strings coerce to `int`/`float` fields under Pydantic's standard (non-strict) mode.

Rich field types validate end to end: `datetime`, `date`, `Decimal`, `UUID`, enums, and JSON-shaped `dict`/`list` fields all round-trip through the database back into their proper Python types. See [Backends](backends.md) for how each maps to column types.

## Limits

**Query predicates** are one place static typing needs a small assist: the lambda parameter passed to `where()` is typed as a `QueryProxy`, whose attributes resolve to `FieldProxy[Any]` rather than each field's declared type. Wiring per-field types through the proxy needs `@dataclass_transform`-style plumbing that Ferro hasn't taken on; the predicate's overall *return* type still checks cleanly as `QueryNode`.

This is a deep enough topic to get its own page: see [Typed Query Predicates](query-typing.md).

## See Also

- [Typed Query Predicates](query-typing.md) — how lambda predicates type-check
- [Models & Fields](../guide/models-and-fields.md)
- [Architecture](architecture.md)
