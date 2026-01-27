# Contributing to Ferro

<!-- --8<-- [start:contributing] -->

We welcome contributions to Ferro! This guide will help you get started with developing Ferro locally.

## Prerequisites

Before starting, ensure you have:

- **Python 3.13+**: Ferro requires Python 3.13 or later
- **Rust toolchain**: Required for building the Rust core
  ```bash
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
  ```
- **UV**: Fast Python package manager
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

## Getting Started

### 1. Clone the Repository

```bash
git clone https://github.com/syn54x/ferro-orm.git
cd ferro-orm
```

### 2. Install Dependencies

```bash
uv sync --group dev
```

This will install all development dependencies including:
- Testing tools (pytest, pytest-asyncio, pytest-cov)
- Linting and formatting tools (ruff, prek)
- Build tools (maturin)
- Documentation tools (mkdocs-material)
- Release tools (commitizen, python-semantic-release)

### 3. Install Pre-commit Hooks

```bash
# Install all hooks (file checks, linting, formatting)
uv run prek install

# Install commit message validation hook
uv run prek install --hook-type commit-msg
```

These hooks will automatically:
- Check for trailing whitespace
- Fix end-of-file issues
- Validate YAML, TOML, and JSON files
- Format Python code with Ruff
- Format Rust code with rustfmt
- Lint Rust code with clippy
- Validate conventional commit messages

### 4. Build the Rust Extension

```bash
uv run maturin develop
```

This compiles the Rust core and installs it in development mode. You'll need to re-run this command after making changes to Rust code.

## Development Workflow

### Running Tests

```bash
# Run all tests with coverage
uv run pytest

# Run specific test file
uv run pytest tests/test_models.py

# Run with verbose output
uv run pytest -v

# Run tests and generate coverage report
uv run pytest --cov=src --cov-report=html
```

### Running Linters

```bash
# Run all pre-commit hooks
uv run prek run --all-files

# Run specific hooks
uv run ruff check .        # Python linting
uv run ruff format .       # Python formatting
cargo fmt                  # Rust formatting
cargo clippy               # Rust linting
```

### Building Documentation

```bash
# Serve documentation locally (with live reload)
uv run mkdocs serve

# Build documentation
uv run mkdocs build

# Documentation will be available at http://127.0.0.1:8000/
```

### Testing Your Changes

Before submitting a PR, ensure:

1. **All tests pass:**
   ```bash
   uv run pytest
   ```

2. **All linters pass:**
   ```bash
   uv run prek run --all-files
   ```

3. **Rust tests pass:**
   ```bash
   cargo test
   ```

4. **Code builds successfully:**
   ```bash
   uv run maturin develop
   ```

## Conventional Commits

Ferro uses [Conventional Commits](https://www.conventionalcommits.org/) for automated version bumping and changelog generation. All commit messages **must** follow this format:

```
<type>[optional scope]: <description>

[optional body]

[optional footer(s)]
```

### Commit Types

- **feat**: New feature (triggers minor version bump)
- **fix**: Bug fix (triggers patch version bump)
- **docs**: Documentation changes only
- **refactor**: Code refactoring (no functional changes)
- **test**: Adding or updating tests
- **perf**: Performance improvements (triggers patch version bump)
- **build**: Build system changes
- **ci**: CI/CD configuration changes
- **chore**: Other changes that don't modify src or test files

### Examples

```bash
# Feature commits
git commit -m "feat: add support for many-to-many relations"
git commit -m "feat(queries): implement OR operator for filters"

# Bug fix commits
git commit -m "fix: resolve connection pool deadlock"
git commit -m "fix(migrations): handle nullable foreign keys correctly"

# Documentation commits
git commit -m "docs: update installation instructions"
git commit -m "docs(api): add examples for transaction usage"

# Breaking changes (triggers major version bump)
git commit -m "feat!: change Model.create() to require explicit save()"
# OR
git commit -m "feat: redesign query API

BREAKING CHANGE: Query.filter() now requires Q objects instead of kwargs"
```

### Commit Validation

The pre-commit hook will automatically validate your commit message format. Invalid commits will be rejected with an error message.

If you need to bypass the hook (not recommended), use:
```bash
git commit --no-verify -m "message"
```

## Pull Request Process

1. **Create a feature branch:**
   ```bash
   git checkout -b feat/my-new-feature
   ```

2. **Make your changes and commit:**
   ```bash
   git add .
   git commit -m "feat: add my new feature"
   ```

3. **Push to your fork:**
   ```bash
   git push origin feat/my-new-feature
   ```

4. **Open a Pull Request** on GitHub

5. **Wait for CI checks to pass:**
   - All linters must pass
   - All tests must pass
   - Code must build on all platforms

6. **Address review feedback** if any

7. **Merge** once approved!

### PR Requirements

- ✅ All CI checks pass
- ✅ Conventional commit format followed
- ✅ Tests added for new features
- ✅ Documentation updated
- ✅ No merge conflicts with main

## Release Process

Ferro uses automated releases. You don't need to manually bump versions or update the changelog.

### How Releases Work

1. **Commits are merged to main**
   - Changelog is automatically updated with unreleased changes

2. **Maintainer creates a GitHub release**
   - Version is automatically determined from conventional commits
   - Both `pyproject.toml` and `Cargo.toml` are updated
   - Git tag is created
   - CHANGELOG.md is finalized

3. **Package is automatically published to PyPI**
   - Cross-platform wheels are built
   - Package is uploaded using trusted publishing

### Version Bumping

Version bumps are determined by commit types:

- **Major** (1.0.0 → 2.0.0): Commits with `BREAKING CHANGE:` in body or `!` after type
- **Minor** (1.0.0 → 1.1.0): Commits with `feat:` type
- **Patch** (1.0.0 → 1.0.1): Commits with `fix:` or `perf:` type

## Code Style

### Python

- Follow PEP 8 style guide
- Use type hints for all functions
- Maximum line length: 100 characters (enforced by Ruff)
- Use Pydantic for data validation
- Write docstrings for all public APIs

### Rust

- Follow Rust style guidelines (enforced by rustfmt)
- Use `cargo clippy` warnings as errors
- Write documentation for public APIs
- Use descriptive variable names
- Prefer explicit types over inference in function signatures

## Testing

### Python Tests

Located in `tests/` directory. Use pytest with async support:

```python
import pytest
from ferro import Model, FerroField, connect

@pytest.mark.asyncio
async def test_create_model():
    await connect("sqlite::memory:")
    user = await User.create(name="Alice")
    assert user.name == "Alice"
```

### Rust Tests

Located alongside Rust code with `#[cfg(test)]`:

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_sql_generation() {
        let query = generate_select_query("users");
        assert_eq!(query, "SELECT * FROM users");
    }
}
```

## Project Structure

```
ferro/
├── src/
│   ├── ferro/           # Python package
│   │   ├── __init__.py
│   │   ├── models.py
│   │   ├── queries.py
│   │   └── ...
│   └── lib.rs           # Rust core
├── tests/               # Python tests
├── docs/                # Documentation
├── .github/
│   └── workflows/       # CI/CD workflows
├── Cargo.toml           # Rust dependencies
├── pyproject.toml       # Python dependencies
└── README.md
```

## Getting Help

- **Issues**: [GitHub Issues](https://github.com/syn54x/ferro-orm/issues)
- **Discussions**: [GitHub Discussions](https://github.com/syn54x/ferro-orm/discussions)
- **Documentation**: [https://ferro.readthedocs.io](https://ferro.readthedocs.io)

## License

By contributing to Ferro, you agree that your contributions will be licensed under the same license as the project (Apache 2.0 OR MIT).

<!-- --8<-- [end:contributing] -->
