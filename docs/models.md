# Models

Models are the central building blocks of Ferro. They define your data schema in Python and are automatically mapped to database tables by the Rust engine.

## Defining a Model

To create a model, inherit from `ferro.Model`. Models use standard Python type hints, leveraging Pydantic V2 for validation and serialization.

```python
from typing import Annotated
from ferro import Field, Model, FerroField

class User(Model):
    id: Annotated[int, FerroField(primary_key=True)]
    username: str
    is_active: bool = True
```

Ferro field metadata can also be declared with the wrapped `ferro.Field` API:

```python
from ferro import Field, Model

class User(Model):
    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(unique=True, min_length=3)
    is_active: bool = True
```

## Internal Mechanics

Ferro uses a custom `ModelMetaclass` to bridge the gap between Python and Rust:

1.  **Schema Capture**: When you define a class, the metaclass inspects its fields and constraints.
2.  **Rust Registration**: The schema is serialized to a JSON-AST and passed to the Rust core's `MODEL_REGISTRY`.
3.  **Table Generation**: When `auto_migrate=True` is used or `create_tables()` is called, the Rust engine generates the appropriate SQL `CREATE TABLE` statements.

## Model Configuration

Since Ferro models are Pydantic models, you can use the `model_config` attribute to control standard behaviors.

```python
from pydantic import ConfigDict
from ferro import Model

class Product(Model):
    model_config = ConfigDict(
        str_strip_whitespace=True,
        validate_assignment=True
    )

    sku: str
    name: str
```

## The Identity Map

Ferro implements an **Identity Map** pattern to ensure object consistency within a single application process.

-   **Consistency**: If you fetch the same record twice (e.g., once via `User.get(1)` and again via a query), Ferro returns the exact same Python object instance.
-   **Performance**: Returning existing instances from the identity map bypasses the hydration cost of creating new Python objects.
-   **In-Place Updates**: Changes made to an object are immediately visible to all other parts of your code holding a reference to that same object.

To manually remove an object from the identity map (forcing a fresh database fetch on the next request), use `ferro.evict_instance(model_name, pk)`.
