# Ferro: The Pydantic x Rust ORM

<!-- --8<-- [start:main] -->

[![PyTest](https://img.shields.io/badge/Pytest-0A9EDC?style=for-the-badge&logo=pytest&logoColor=white)](https://docs.pytest.org/)
[![Ruff](https://img.shields.io/badge/Ruff-FFC107?style=for-the-badge&logo=python&logoColor=black)](https://docs.astral.sh/ruff/)
[![MkDocs](https://img.shields.io/badge/MkDocs-000000?style=for-the-badge&logo=markdown&logoColor=white)](https://www.mkdocs.org/)
[![UV](https://img.shields.io/badge/UV-2C2C2C?style=for-the-badge&logo=python&logoColor=white)](https://github.com/astral-sh/uv)
[![Rust](https://img.shields.io/badge/Rust-000000?style=for-the-badge&logo=rust&logoColor=white)](https://www.rust-lang.org/)
[![Python](https://img.shields.io/badge/Python-3.13%20|%203.14-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![PyPI](https://img.shields.io/pypi/v/ferro-orm?style=for-the-badge)](https://pypi.org/project/ferro-orm/)
[![License](https://img.shields.io/badge/License-Apache_2.0-D22128?style=for-the-badge&logo=apache&logoColor=white)](https://opensource.org/licenses/Apache-2.0)
[![Downloads](https://img.shields.io/pepy/dt/ferro-orm?style=for-the-badge)](https://pepy.tech/project/ferro-orm)
[![Codecov](https://img.shields.io/codecov/c/github/syn54x/ferro-orm?style=for-the-badge&logo=codecov&logoColor=white)](https://codecov.io/gh/syn54x/ferro-orm)
[![Issues](https://img.shields.io/github/issues/syn54x/ferro-orm?style=for-the-badge&logo=github&logoColor=white)](https://github.com/syn54x/ferro-orm/issues)

Ferro is a high-performance, asynchronous ORM for Python, powered by a core engine written in Rust. Designed for simplicity and it only has one dependency: Pydantic.  Delivering ergonomics familiar to the modern Pythonista and the speed and safety of Rust's SQLx and Sea-Query.

## Key Features

- **High-Performance Core**: All SQL generation and row hydration are handled by a dedicated Rust engine, minimizing "Python Tax" on data-heavy operations.
- **Async First**: Built from the ground up for asynchronous applications, utilizing `pyo3-async-runtimes` for non-blocking I/O.
- **Pydantic Integration**: Leverages Pydantic V2 for schema definition and data validation, providing full IDE support and type safety.
- **Zero-Copy Intent**: Designed with zero-copy principles to maximize throughput during large-scale data retrieval.
- **Identity Map**: Ensures object consistency across your application by tracking active model instances in a thread-safe registry.

## Architecture

Ferro operates through a dual-layer architecture connected via a high-performance FFI (Foreign Function Interface) bridge:

1.  **Python Layer**: Developers define models using standard Python classes; a metaclass registers them with the backend.
2.  **Rust Engine**: Built on `SQLx` and `Sea-Query` for GIL-free row parsing and object instantiation.

## Installation

Ferro is distributed as pre-compiled wheels for macOS, Linux, and Windows.

```bash
pip install ferro-orm
# Or with migration support
pip install "ferro-orm[alembic]"
```

## Quick Start

```python
import asyncio
from typing import Annotated
from ferro import Model, FerroField, connect

class User(Model):
    id: Annotated[int, FerroField(primary_key=True)]
    username: str
    is_active: bool = True

async def main():
    await connect("sqlite:example.db?mode=rwc", auto_migrate=True)

    # Create
    alice = await User.create(username="alice")

    # Query
    active_users = await User.where(User.is_active == True).all()
    print(f"Found {len(active_users)} active users.")

if __name__ == "__main__":
    asyncio.run(main())
```

<!-- --8<-- [end:main] -->
