# Installation

## Requirements

- Python 3.10 or higher
- Supported platforms: macOS, Linux, Windows
- Database: SQLite, PostgreSQL, or MySQL

## Install Ferro

Ferro is distributed as pre-compiled wheels for all major platforms:

```bash
# UV
uv add ferro-orm

# Or pip
pip install ferro-orm
```

### With Migration Support

For production use with Alembic migrations:

```bash
uv add "ferro-orm[alembic]"
```

This installs Alembic and SQLAlchemy (used only for migration generation, not at runtime).

## Database Drivers

Ferro uses SQLx under the hood, which includes drivers for all supported databases. No additional database-specific packages are required.

### SQLite

No additional setup needed. SQLite is embedded in Ferro.

### PostgreSQL

No additional setup needed. PostgreSQL support is built into Ferro.

### MySQL

No additional setup needed. MySQL/MariaDB support is built into Ferro.


## Optional Dependencies

### Development Tools

For running tests and linting:

```bash
pip install "ferro-orm[dev]"
```

This includes pytest, ruff, mypy, and other development tools.

## Building from Source

!!! note
    Most users don't need to build from source. Pre-compiled wheels are available for all common platforms.

If you need to build from source (e.g., for an unsupported platform):

**Requirements:**
- Rust 1.70 or higher
- Python development headers

```bash
# Install Rust
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh

# Install maturin (Rust/Python build tool)
pip install maturin

# Clone and build
git clone https://github.com/syn54x/ferro-orm.git
cd ferro-orm
maturin develop --release
```

Build time is typically 2-5 minutes depending on your machine.

## Next Steps

Ready to build your first Ferro application?

[:octicons-arrow-right-24: Start the tutorial](tutorial.md){ .md-button .md-button--primary }
