# Installation

## Requirements

- **Python 3.13 or newer**
- macOS, Linux, or Windows — pre-compiled wheels are published for all three, so no Rust toolchain is needed for a normal install

## Install

=== "uv"

    ```bash
    uv add ferro-orm
    ```

=== "pip"

    ```bash
    pip install ferro-orm
    ```

### Migration support

Reviewed schema migrations (`ferro migrate init`, `new`, `up`) need the `cli` extra, which installs the `ferro` command line:

=== "uv"

    ```bash
    uv add "ferro-orm[cli]"
    ```

=== "pip"

    ```bash
    pip install "ferro-orm[cli]"
    ```

See [Schema Management](../guide/schema/overview.md) for how migrations and auto-migrate fit together, and [Migrations](../guide/schema/migrations.md) for the workflow.

A project that already runs Alembic can keep it with the `alembic` extra instead (`pip install "ferro-orm[alembic]"`), which adds Alembic and SQLAlchemy for generating revisions only; Ferro never uses SQLAlchemy at runtime. See [Alembic](../guide/schema/alembic.md).

## Database Drivers

You don't need any. Ferro's Rust engine bundles SQLite and PostgreSQL support via SQLx, so there are no driver packages to install or configure — `pip install ferro-orm` is enough for both backends.

## Building from Source

!!! note
    Most users never need this. Pre-compiled wheels cover all common platforms.

Building from source (for example, on an unsupported platform) requires a recent Rust toolchain and [maturin](https://www.maturin.rs/):

```bash
# Install Rust
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh

# Clone and build
git clone https://github.com/syn54x/ferro-orm.git
cd ferro-orm
pip install maturin
maturin develop --release
```

Expect the first compile to take a few minutes.

## Verify Your Installation

```python
import ferro

print(ferro.version())
```

If this prints a version number, you're ready to go.

## Next Steps

Build your first app with the [Quickstart Tutorial](quickstart.md).
