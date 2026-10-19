# Ferro: The Pydantic x Rust ORM

[![PyTest](https://img.shields.io/badge/Pytest-0A9EDC?style=for-the-badge&logo=pytest&logoColor=white)](https://docs.pytest.org/)
[![Ruff](https://img.shields.io/badge/Ruff-FFC107?style=for-the-badge&logo=python&logoColor=black)](https://docs.astral.sh/ruff/)
[![Zensical](https://img.shields.io/badge/Zensical-4051B5?style=for-the-badge&logo=markdown&logoColor=white)](https://zensical.org/)
[![UV](https://img.shields.io/badge/UV-2C2C2C?style=for-the-badge&logo=python&logoColor=white)](https://github.com/astral-sh/uv)
[![Rust](https://img.shields.io/badge/Rust-000000?style=for-the-badge&logo=rust&logoColor=white)](https://www.rust-lang.org/)
[![Python](https://img.shields.io/badge/Python-3.13%20|%203.14-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![PyPI](https://img.shields.io/pypi/v/ferro-orm?style=for-the-badge)](https://pypi.org/project/ferro-orm/)
[![License](https://img.shields.io/badge/License-Apache_2.0-D22128?style=for-the-badge&logo=apache&logoColor=white)](https://opensource.org/licenses/Apache-2.0)
[![Downloads](https://img.shields.io/pepy/dt/ferro-orm?style=for-the-badge)](https://pepy.tech/project/ferro-orm)
[![Codecov](https://img.shields.io/codecov/c/github/syn54x/ferro-orm?style=for-the-badge&logo=codecov&logoColor=white)](https://codecov.io/gh/syn54x/ferro-orm)
[![Issues](https://img.shields.io/github/issues/syn54x/ferro-orm?style=for-the-badge&logo=github&logoColor=white)](https://github.com/syn54x/ferro-orm/issues)

<!-- --8<-- [start:intro] -->
Ferro is an async ORM for Python: you declare Pydantic models, and a Rust engine builds the SQL, runs it and turns rows into model instances. Its runtime dependencies are Pydantic and pydantic-settings.
<!-- --8<-- [end:intro] -->

Here is a model, a query and the result:

```python
import asyncio

from ferro import Field, Model, connect, engines


class User(Model):
    id: int | None = Field(default=None, primary_key=True)
    username: str
    is_active: bool = True


async def main():
    await connect("sqlite::memory:", auto_migrate=True)

    async with engines.session():
        await User.create(username="alice")
        await User.create(username="bob", is_active=False)

        active_users = await User.where(lambda user: user.is_active == True).all()
        print(f"Found {len(active_users)} active users.")


asyncio.run(main())
```

Running it prints:

```text
Found 1 active users.
```

Every ORM call runs inside `async with engines.session():`, which routes it to the connection you opened. `auto_migrate=True` creates the table on connect; it suits development and tests.

## What Ferro does

- The Rust engine generates the SQL and parses rows outside the GIL, then fills each model's fields directly, without calling `__init__`.
- Everything is async, on `pyo3-async-runtimes` and SQLx.
- Models are Pydantic v2 models, so validation and IDE support work as they do for any Pydantic class.
- Within a session, one database row is one Python object (the identity map).
- It supports SQLite and PostgreSQL. Both are compiled into the engine, so no driver packages are needed.

## Installation

Ferro ships pre-compiled wheels for macOS, Linux and Windows:

```bash
pip install ferro-orm
# With the migrations CLI (ferro migrate)
pip install "ferro-orm[cli]"
# With the Alembic bridge
pip install "ferro-orm[alembic]"
```

## Schema changes

Auto-migrate keeps a development database current. For production, `ferro migrate new` writes reviewed migration files and `ferro migrate up` applies them; see [Schema management](https://ferro-orm.x54.sh/guide/schema/overview/). To use more than one database, register named connections with `connect(..., name="...")`; see [Connections](https://ferro-orm.x54.sh/guide/connections/).
