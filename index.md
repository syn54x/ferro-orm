# Overview

Ferro is a high-performance, asynchronous ORM for Python, built with a Rust-backed core engine. It combines the ergonomics of Pydantic models with the speed and safety of Rust's SQLx and Sea-Query.

## Key Features

- **High-Performance Core**: All SQL generation and row hydration are handled by a dedicated Rust engine, minimizing "Python Tax" on data-heavy operations.
- **Async First**: Built from the ground up for asynchronous applications, utilizing `pyo3-async-runtimes` for non-blocking I/O.
- **Pydantic Integration**: Leverages Pydantic V2 for schema definition and data validation, providing full IDE support and type safety.
- **Zero-Copy Intent**: Designed with zero-copy principles to maximize throughput during large-scale data retrieval.
- **Identity Map**: Ensures object consistency across your application by tracking active model instances in a thread-safe registry.

## Architecture

Ferro operates through a dual-layer architecture connected via a high-performance FFI (Foreign Function Interface) bridge:

1. **Python Layer**: Developers define models using standard Python classes; a metaclass registers them with the backend.
1. **Rust Engine**: Built on `SQLx` and `Sea-Query` for GIL-free row parsing and object instantiation.

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
