# Migrations

Ferro integrates with **Alembic**, the industry-standard migration tool for Python, to provide robust and reliable schema management.

## Integration Overview

Instead of reinventing a migration system, Ferro utilizes a SQLAlchemy bridge. This bridge translates Ferro's internal model registry into an in-memory SQLAlchemy `MetaData` object, which Alembic then uses to detect changes.

### Installation
Ensure you have installed the migration dependencies:

```bash
pip install "ferro-orm[alembic]"
```

## Using `get_metadata()`

To connect Ferro to Alembic, you must update your `env.py` file (typically found in the `migrations/` directory created by `alembic init`).

The `get_metadata()` function automatically discovers all registered Ferro models and returns a SQLAlchemy `MetaData` object.

```python
# migrations/env.py
from ferro.migrations import get_metadata
from my_app.models import User, Post  # Ensure models are imported to register them

# Pass the Ferro-generated metadata to Alembic
target_metadata = get_metadata()
```

## Workflow

1.  **Initialize Alembic**: Run `alembic init migrations` if you haven't already.
2.  **Define Models**: Create your Ferro models as usual.
3.  **Generate Migration**: Run the autogenerate command:
    ```bash
    alembic revision --autogenerate -m "Initial schema"
    ```
4.  **Apply Migration**: Update your database:
    ```bash
    alembic upgrade head
    ```

## Precision Mapping

Ferro's migration bridge ensures high fidelity between your code and the database:

-   **Nullability**: Automatically detects whether a field is required or optional (e.g., `str` vs `str | None`).
-   **Complex Types**: Correctly maps Enums, Decimals, UUIDs, and JSON fields to the appropriate database-native types.
-   **Constraints**: Translates `primary_key`, `unique`, and `index` metadata directly into the migration script.
-   **Foreign Keys**: Automatically generates `FOREIGN KEY` constraints, including custom `on_delete` behaviors like `CASCADE` or `SET NULL`.
-   **Join Tables**: Automatically discovers and includes hidden join tables for Many-to-Many relationships.
