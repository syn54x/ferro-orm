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

Schema migrations use Alembic. Install the optional extra to get it:

=== "uv"

    ```bash
    uv add "ferro-orm[alembic]"
    ```

=== "pip"

    ```bash
    pip install "ferro-orm[alembic]"
    ```

This pulls in Alembic (and SQLAlchemy, which Alembic uses for migration generation only — Ferro never uses it at runtime). See [Migrations](../guide/migrations.md) for the workflow.

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
