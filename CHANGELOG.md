# Changelog

All notable changes to Ferro will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

### Changed

### Fixed

### Removed

### Performance

### Security

## [0.1.0] - 2026-01-27

### Added

- Initial release of Ferro ORM
- High-performance Rust-backed core engine using PyO3
- Asynchronous-first design with async/await support
- Pydantic V2 integration for schema definition and validation
- SQLx integration for database connectivity (SQLite, PostgreSQL)
- Sea-Query for dynamic SQL generation
- Identity Map for object consistency
- Zero-copy principles for data hydration
- Model definition with type annotations
- Basic CRUD operations (create, read, update, delete)
- Query building with chainable methods
- Connection management with `connect()` function
- Auto-migration support with `auto_migrate=True`
- Alembic integration for database migrations
- Pre-commit hooks for code quality (Ruff, rustfmt, clippy)
- Conventional commit enforcement with Commitizen
- Comprehensive test suite with pytest
- Documentation with MkDocs Material
- UV-based development workflow
- Cross-platform wheel support (macOS, Linux, Windows)

[Unreleased]: https://github.com/syn54x/ferro-orm/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/syn54x/ferro-orm/releases/tag/v0.1.0
