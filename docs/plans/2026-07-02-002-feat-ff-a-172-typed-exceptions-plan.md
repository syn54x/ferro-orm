---
title: "feat: Typed exception hierarchy mapped from sqlx (#172)"
date: 2026-07-02
issue: 172
epic: 170
branch: feat/ff-a-172-typed-exceptions
origin: docs/plans/2026-07-02-001-fable-fixes-roadmap.md (Epic FF-A, sub-task A2)
---

# Typed exception hierarchy Implementation Plan

**Goal:** Every database failure raised by the Rust core is catchable by type from Python. `ferro.exceptions` grows a DBAPI-shaped tree; a new `src/errors.rs` maps `sqlx::Error` (via `DatabaseError::kind()`) onto it, preserving the original driver message plus structured `sqlstate`/`constraint` attributes. No user-facing string matching required, ever.

**Architecture:** Exceptions are defined in pure Python (`src/ferro/exceptions.py`) so `__module__`, pickling, and mkdocstrings behave; Rust raises them through a `GILOnceCell`-cached lookup of `ferro.exceptions` at first raise (cold path — no import cycle, `_core` never imports `ferro` at init). One conversion function, `map_db_error(context, sqlx::Error) -> PyErr`, with a pure `exception_name_for(&sqlx::Error) -> &'static str` classifier that cargo unit tests pin. All engine-execution `map_err` sites in `operations.rs`, the DDL-execution sites in `migrate.rs`, and connect/routing failures in `connection.rs` convert; internal invariants (lock poisoning, registry misses) stay `RuntimeError`.

**Tree:** `FerroError` → `InterfaceError`, `OperationalError`, `DataError`, `IntegrityError` (→ `UniqueViolationError`, `ForeignKeyViolationError`, `NotNullViolationError`, `CheckViolationError`). `ModelDoesNotExist` re-parents to `(FerroError, LookupError)` — existing `except LookupError` callers keep working.

**Mapping:** `Database(e).kind()` Unique/ForeignKey/NotNull/CheckViolation → the four violations; `Database` Other → `OperationalError` (sqlstate kept); `Io`/`Tls`/`PoolTimedOut`/`PoolClosed`/`WorkerCrashed`/`RowNotFound`/catch-all → `OperationalError`; `Configuration`/`Protocol`/`ColumnNotFound`/`ColumnIndexOutOfBounds` → `InterfaceError`; `Decode`/`ColumnDecode`/`TypeNotFound` → `DataError`. Message: `"{context}: {driver message}"`.

**Closes:** [#172](https://github.com/syn54x/ferro-orm/issues/172)
**Base branch:** `main`

## Constraints

- AGENTS.md I-3 (PyResult, never panic across FFI), I-6 (no partial conversion — every *database* failure typed), I-10 (no CHANGELOG edits), conventional commits, no AI attribution.
- Exit-gate rule (epic #170): tests assert exception **types**, zero driver-message string matching.

## Tasks

- [ ] RED: `tests/test_exceptions.py` — hierarchy `issubclass` chains, `ModelDoesNotExist` still `LookupError`, structured attr defaults, pickle round-trip.
- [ ] GREEN: `src/ferro/exceptions.py` tree + `src/ferro/__init__.py` exports.
- [ ] RED: `tests/test_exception_mapping.py` (`backend_matrix`) — unique violation → `UniqueViolationError` (+ `sqlstate == "23505"` postgres-only), not-null via `Query.update(col=None)` → `NotNullViolationError`, FK violation → `ForeignKeyViolationError`, bad connect → `OperationalError`, unknown `using` → `InterfaceError`; all currently `RuntimeError`.
- [ ] Verify SQLite FK enforcement (`PRAGMA foreign_keys`) before writing the FK test; if sqlx defaults it off, enabling it is a deliberate scoped change in this PR.
- [ ] GREEN: new `src/errors.rs` (`map_db_error`, `exception_name_for`, cached class lookup) + `mod errors;` in `lib.rs`; swap engine-execution `map_err` sites in `operations.rs`, `migrate.rs`, `connection.rs`; routing misses → `InterfaceError`.
- [ ] Rust unit tests for `exception_name_for` over constructed sqlx variants.
- [ ] Docs: `docs/pages/api/exceptions.md` `:::` blocks; migration-guide row under new `### Fable Fixes — FF-A` section (minor; broad `except RuntimeError` → `except ferro.FerroError`).
- [ ] Full matrix green; `cargo test` green; PR with `Closes #172`.
