# Changelog

All notable changes to Ferro ORM will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Breaking

- `Model.get` and `Model.using(...).get` now return the concrete model type and raise `ModelDoesNotExist` when no row exists (previously they returned `T | None`). Use the new `get_or_none` for the old optional behavior.

### Added

- `Model.get_or_none` and `Model.using(...).get_or_none` for primary-key lookup without raising.
- `ModelDoesNotExist` (`LookupError` subclass with `.model` and `.pk`), exported from `ferro`. Documented under [Exceptions](api/exceptions.md).
- Typed query predicates: `col()` wrapper and lambda predicate API on `Query.where`, `Relation.where`, and `Model.where` for static-typing-clean predicates without model annotation changes ([#48](https://github.com/syn54x/ferro-orm/pull/48)). See [Typed Query Predicates](concepts/query-typing.md).
- `FieldProxy` is now generic (`FieldProxy[T]`); operator overloads are typed `T | FieldProxy[T] -> QueryNode`, `.like()` is gated to `FieldProxy[str]`.
- New public symbols re-exported from `ferro.query`: `col`, `QueryProxy`, `Predicate`.
- Comprehensive documentation restructure
- Tutorial for new users
- How-to guides for common patterns
- Concept pages explaining architecture

## Release History

For the complete release history, see [GitHub Releases](https://github.com/syn54x/ferro-orm/releases).

### Version Format

- **Major** (X.0.0): Breaking changes
- **Minor** (0.X.0): New features, backwards compatible
- **Patch** (0.0.X): Bug fixes

### Upgrade Guide

When upgrading between major versions, see the migration guide in the release notes.

## Reporting Issues

Found a bug? [Report it on GitHub](https://github.com/syn54x/ferro-orm/issues).

## Contributing

See [Contributing Guide](contributing.md) for how to contribute to Ferro.
