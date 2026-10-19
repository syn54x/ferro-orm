# Type safety

Ferro models are Pydantic models and Ferro's API is fully annotated, so building an instance validates its fields at runtime and a type checker knows what each query returns.

The examples on this page use this `User` model, with four users saved (ada, grace, alan and edsger):

=== "Assignment"

    ```python
    --8<-- "docs/examples/concepts.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/concepts_annotated.py:models"
    ```

A query returns `User` instances, and each one is a Pydantic model:

```python {test=run prelude=concepts}
from ferro import engines

async with engines.session():
    ada = await User.where(lambda user: user.username == "ada").first()
    assert ada is not None
    assert ada.model_dump() == {
        "id": 1,
        "username": "ada",
        "email": "ada@example.com",
        "age": 36,
        "active": True,
    }
```

`first()` returns `None` when no row matches, so the first `assert` rules that out before `ada` is used. `model_dump()` is Pydantic's own method, and it returns ada's row as a dict.

## Pydantic models

`Model` is a subclass of Pydantic's `BaseModel`, so `model_dump`, `model_validate`, JSON schema generation and serialization settings all work unchanged. Validation runs whenever an instance is built, by constructing a model or by `Model.create(...)`, before any data reaches the database. Constructing a user with an age that is not a number fails:

```python {test=run prelude=concepts}
from pydantic import ValidationError

try:
    User(username="linus", email="linus@example.com", age="twenty")
except ValidationError as error:
    print(error)
else:
    raise AssertionError("an age that is not a number must fail validation")
```

It prints:

```text
1 validation error for User
age
  Input should be a valid integer, unable to parse string as an integer [type=int_parsing, input_value='twenty', input_type=str]
    For further information visit https://errors.pydantic.dev/2.12/v/int_parsing
```

The error names the field and the rule it broke, and nothing was written. An assignment to a field of an existing instance is not validated unless the model sets `validate_assignment=True` in its `model_config`, and `save()` writes the values the instance holds. Pydantic's coercion rules apply too: in its default, non-strict mode, `age="36"` becomes the integer `36`. Field validators, model validators and `model_config` settings behave as they do in Pydantic; [Models and fields › Validation](../guide/models-and-fields.md#validation) shows one.

Rich field types validate from end to end: `datetime`, `date`, `time`, `Decimal`, `UUID`, enums, and `dict` and `list` fields stored as JSON all come back from the database as the annotated Python type. [Database backends](backends.md#type-mapping) shows the column type each one gets.

## Typed results

Ferro's public API carries type hints, so a type checker (ty, mypy, Pyright) and your editor know what each call returns. This block runs inside a [session](../guide/connections.md#sessions):

```python {test=run prelude=concepts}
from typing import assert_type

assert_type(await User.get(1), User)
assert_type(await User.get_or_none(99), User | None)
assert_type(await User.all(), list[User])
assert_type(await User.where(lambda user: user.age >= 18).count(), int)
assert_type(await User.where(lambda user: user.username == "ada").exists(), bool)
```

Each `assert_type` passes `ty check`, and at runtime it does nothing. `first()` is annotated the same way, returning the model or `None`. `get()` returns `User` because it raises `ModelDoesNotExist` when no row has the key; `get_or_none()` returns `User | None` instead. A result is a model instance, so code that uses it is checked too: `ada.age` is an `int`, and `ada.nickname` is a type error.

## Editor completion

The same annotations drive completion in your editor:

- A model instance completes its fields and its methods (`save`, `delete`, `refresh`, ...).
- `get()`, `get_or_none()` and `all()` return `User`, so their results complete `User` attributes.
- Inside a lambda predicate, attribute names are not completed: the lambda's parameter is a query proxy whose attributes are typed `FieldProxy[Any]`. A misspelled name raises `AttributeError` when the query is built, before anything reaches the database; see [Typed query predicates › Proxy validation](query-typing.md#proxy-validation).

Ferro needs no type checker plugin. It uses standard typing constructs (`Self`, generics, overloads) that every checker reads.

## Rust boundary

A type checker cannot read a compiled extension module, so Ferro ships a stub file, `ferro/_core.pyi`, that describes every function the Rust core exposes. The package is marked with `py.typed`, so checkers use the stub and Ferro's own annotations when you depend on `ferro-orm`.

You rarely call `ferro._core` directly: `Model`, `Query`, `connect` and `transaction` are the public API. The stub exists so the boundary itself is checked too.

## Where checking stops

A type checker catches a predicate that returns the wrong kind of value, but not a comparison with the wrong type: `lambda user: user.age >= "eighteen"` type-checks, because `user.age` is `FieldProxy[Any]`. [Typed query predicates › Where checking stops](query-typing.md#where-checking-stops) explains why, and what would close the gap.

## See also

- [Typed query predicates](query-typing.md): how lambda predicates are checked
- [Models and fields](../guide/models-and-fields.md): declaring fields, types and validation
- [Architecture](architecture.md): where Python ends and Rust begins
