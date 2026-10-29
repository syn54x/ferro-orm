---
description: "Install Ferro with pip or uv, add the optional extras and check the installed version."
---

# Installation

Install Ferro with `pip` or `uv`, then check the installed version. Ferro ships pre-compiled wheels, so a normal install needs no Rust toolchain:

=== "uv"

    ```bash
    uv add ferro-orm
    ```

=== "pip"

    ```bash
    pip install ferro-orm
    ```

The install adds Ferro and its two runtime dependencies, Pydantic and pydantic-settings.

## Requirements

- Python 3.13 or newer. Ferro is tested on 3.13 and 3.14.
- macOS, Linux or Windows. Wheels are published for all three.
- PostgreSQL 17 or newer, if you use Postgres. Ferro is tested on 17 and 18, and `connect()` refuses an older server with an `InterfaceError` that names its version. SQLite has no version floor.

## Extras

Ferro has two optional extras. The `cli` extra installs the `ferro` command, which runs reviewed schema migrations (`ferro migrate init`, `new`, `up`). The `alembic` extra adds Alembic and SQLAlchemy so a project that already runs Alembic can generate revisions from Ferro models; Ferro never uses SQLAlchemy at runtime.

Add the extra you need:

=== "uv"

    ```bash
    uv add "ferro-orm[cli]"
    uv add "ferro-orm[alembic]"
    ```

=== "pip"

    ```bash
    pip install "ferro-orm[cli]"
    pip install "ferro-orm[alembic]"
    ```

[Schema management](../guide/schema/overview.md) explains how migrations and auto-migrate fit together, [Migrations](../guide/schema/migrations.md) covers the workflow, and [Alembic](../guide/schema/alembic.md) covers the bridge.

## Database drivers

Ferro's Rust engine bundles SQLite and PostgreSQL support through SQLx, so there are no driver packages to install or configure. `ferro-orm` alone covers both backends.

## Build from source

Most users never need this, because wheels cover the common platforms. To build on an unsupported platform, install a Rust toolchain and [uv](https://docs.astral.sh/uv/), then build into a project environment:

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
git clone https://github.com/syn54x/ferro-orm.git
cd ferro-orm
uv sync
uv run maturin develop --release
```

The first compile takes a few minutes, and the build installs Ferro into the project's `.venv`.

## Installed version

Print the version to confirm the install:

```python {test=compile reason="prints the version, which changes every release"}
import ferro

print(ferro.version())
```

The number is the installed Ferro version:

<!-- shows: an example version number; yours is the version installed -->
```text
0.25.0
```

## Next steps

Build a small app in the [Quickstart](quickstart.md).
