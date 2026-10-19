# Contributing to Ferro

<!-- --8<-- [start:contributing] -->

To develop Ferro locally you need Python 3.13 or later, a Rust toolchain and uv. This guide takes you from a clone to a pull request.

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

## Getting started

### 1. Clone the repository

```bash
git clone https://github.com/syn54x/ferro-orm.git
cd ferro-orm
```

### 2. Install dependencies

```bash
uv sync --group dev
```

This installs every development dependency, including:
- Testing tools (pytest, pytest-asyncio, pytest-cov)
- Linting and formatting tools (ruff, prek)
- Build tools (maturin)
- Documentation tools (zensical)
- Release tools (commitizen, python-semantic-release)

### 3. Install pre-commit hooks

```bash
# Install all hooks (file checks, linting, formatting)
uv run prek install

# Install commit message validation hook
uv run prek install --hook-type commit-msg
```

These hooks:
- Check for trailing whitespace
- Fix end-of-file issues
- Validate YAML, TOML, and JSON files
- Format Python code with Ruff
- Format Rust code with rustfmt
- Lint Rust code with clippy
- Validate conventional commit messages

### 4. Build the Rust extension

```bash
uv run maturin develop
```

This compiles the Rust core and installs it in development mode. You'll need to re-run this command after making changes to Rust code.

## Development workflow

### Running tests

```bash
# Run all tests with coverage
uv run pytest

# Run all tests in parallel, without coverage
uv run pytest -n 8 --no-cov

# Run specific test file
uv run pytest tests/test_models.py

# Run with verbose output
uv run pytest -v

# Run tests and generate coverage report
uv run pytest --cov=src --cov-report=html
```

### Running linters

```bash
# Run all pre-commit hooks
uv run prek run --all-files

# Run specific hooks
uv run ruff check .        # Python linting
uv run ruff format .       # Python formatting
cargo fmt                  # Rust formatting
cargo clippy --workspace --all-targets -- -D warnings   # Rust linting
```

### Building documentation

```bash
# Serve documentation locally (with live reload)
uv run zensical serve

# Build documentation
uv run zensical build

# The site is served at http://127.0.0.1:8000/
```

Before you open a documentation pull request, run the strict build. It fails on a broken link or a snippet that no longer exists:

```bash
uv run zensical build --clean --strict
```

### Testing your changes

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
   cargo test --no-default-features --features testing
   ```

4. **Code builds successfully:**
   ```bash
   uv run maturin develop
   ```

## Conventional commits

Ferro uses [Conventional Commits](https://www.conventionalcommits.org/) for automated version bumping and changelog generation. All commit messages **must** follow this format:

```
<type>[optional scope]: <description>

[optional body]

[optional footer(s)]
```

### Commit types

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

### Commit validation

The pre-commit hook will automatically validate your commit message format. Invalid commits will be rejected with an error message.

If you need to bypass the hook (not recommended), use:
```bash
git commit --no-verify -m "message"
```

## Pull request process

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

4. **Open a pull request** on GitHub. Put `Closes #N` in the body for each issue the pull request completes (see [Project rules](#project-rules)).

5. **Wait for CI checks to pass:**
   - All linters must pass
   - All tests must pass
   - Code must build on all platforms

6. **Address review feedback**, if any.

A maintainer merges the pull request once it is approved.

### PR requirements

- All CI checks pass
- Conventional commit format followed
- Tests added for new features
- Documentation updated
- No merge conflicts with `main`
- `Closes #N` in the body for every issue the pull request completes
- `CHANGELOG.md` untouched

## Project rules

[`AGENTS.md`](https://github.com/syn54x/ferro-orm/blob/main/AGENTS.md) lists the invariants every change must keep. They bind human contributors and AI agents alike. The ones a pull request meets most often:

- **Issues close with the pull request (I-9).** Put a GitHub keyword in the body for each completed issue (`Closes #89`, `Fixes #90`), so nobody has to triage it after the merge.
- **Never edit `CHANGELOG.md` (I-10).** The release workflow writes it from your conventional commit messages, so describe the change in the pull request title and body instead.
- **Docs show both field styles (I-7).** An example that declares fields with `Field()` appears twice, as an Assignment tab and an Annotated tab, each backed by a runnable script in `docs/examples/`.
- **Docs queries use lambda predicates (I-8).** Name the lambda parameter after the model in lowercase singular: `User.where(lambda user: user.age >= 18)`.
- **Every migration door produces the same DDL (I-1).** A schema change goes through the one planner, so `connect(auto_migrate=True)`, `ferro migrate new` and the Alembic bridge emit the same statements.
- **No `unwrap()` across the FFI boundary (I-3).** A PyO3 function returns a `PyResult` instead of panicking.

## Release process

Ferro uses automated releases. You don't need to manually bump versions or update the changelog.

### How releases work

1. **Commits are merged to main**
   - Changelog is automatically updated with unreleased changes

2. **Maintainer triggers the release workflow**

   The release process is fully automated. To trigger a new release:

   - **Via GitHub CLI (Recommended):**
     ```bash
     gh workflow run release.yml
     ```
   - **Via GitHub Web UI:** Go to the **Actions** tab, select the **Release** workflow, and click **Run workflow**.

   This will:
   - Automatically determine the next version from conventional commits.
   - Update `pyproject.toml` and `Cargo.toml`.
   - Finalize `CHANGELOG.md`.
   - Create and push the Git tag.
   - Trigger the **Build & Publish** workflow to upload wheels to PyPI.

3. **Package is automatically published to PyPI**
   - Cross-platform wheels are built
   - Package is uploaded using trusted publishing

### Version bumping

Version bumps are determined by commit types:

- **Major** (1.0.0 → 2.0.0): Commits with `BREAKING CHANGE:` in body or `!` after type
- **Minor** (1.0.0 → 1.1.0): Commits with `feat:` type
- **Patch** (1.0.0 → 1.0.1): Commits with `fix:` or `perf:` type

## Code style

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

### Python tests

Tests that cross the Python and Rust bridge, or exercise Pydantic models, live in `tests/`. A test connects with the `db_url` fixture, opens a session and asserts on what comes back:

```python
import pytest

from ferro import Field, Model, connect, engines


class Author(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str


@pytest.mark.asyncio
async def test_create_author(db_url):
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        author = await Author.create(name="Ada Lovelace")
        assert author.id is not None
```

The `db_url` fixture gives each test its own SQLite file. Mark a test `backend_matrix` to run it against Postgres too when a server is available.

### Rust tests

Pure SQL and schema generation logic is tested in Rust, in a `#[cfg(test)]` module next to the code it covers. Run all of them with:

```bash
cargo test --no-default-features --features testing
```

The flags skip the extension-module linking so the test binary can resolve Python symbols, and start the interpreter for tests that call into Python. A feature is not done until both the Rust and the Python tests pass.

## Project structure

```
ferro/
├── src/
│   ├── ferro/           # Python package (models, query/, relations/, migrations/, cli/)
│   └── *.rs             # Rust core, exposed to Python through PyO3
├── crates/
│   ├── ferro-ddl-lowering/   # column types, names and rendering rules
│   ├── ferro-migrate/        # the one schema planner and its renderer
│   └── ferro-schema-ir/      # the schema intermediate representation
├── tests/               # Python tests
├── docs/
│   ├── pages/           # the documentation site
│   ├── examples/        # runnable scripts the pages embed
│   ├── adr/             # architecture decision records
│   └── solutions/       # patterns and known footguns
├── .github/
│   └── workflows/       # CI/CD workflows
├── AGENTS.md            # project invariants
├── CONTEXT.md           # domain vocabulary
├── Cargo.toml           # Rust workspace
├── pyproject.toml       # Python package
└── README.md
```

## Getting help

- **Issues**: [GitHub Issues](https://github.com/syn54x/ferro-orm/issues)
- **Documentation**: [https://ferro-orm.x54.sh/](https://ferro-orm.x54.sh/)

## License

By contributing to Ferro, you agree that your contributions will be licensed under the project's license, [Apache 2.0](https://github.com/syn54x/ferro-orm/blob/main/LICENSE).

<!-- --8<-- [end:contributing] -->
