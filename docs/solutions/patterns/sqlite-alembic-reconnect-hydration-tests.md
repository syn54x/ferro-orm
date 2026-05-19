---
title: Alembic SQLite schema + reconnect integration tests for hydration
type: pattern
tags: [convention, sqlite, hydration, pytest, alembic, sqlalchemy, testing]
related_files:
  - tests/test_sqlite_alembic_reconnect_hydration.py
  - src/backend.rs
  - src/operations.rs
related_issues: [56, 58]
related_prs: [57, 59]
captured: 2026-05-18
---

## Problem

Most Ferro integration tests use `auto_migrate=True` (Rust DDL) and often read
rows on the same connection that inserted them. SQLite hydration bugs tied to
Alembic column affinities and the identity map only appear after a fresh
`ferro.connect()`.

## Takeaway

For SQLite fetch/hydration regressions, add tests that use Alembic
`get_metadata().create_all`, `auto_migrate=False`, `reset_engine()`, then
re-fetch — see `tests/test_sqlite_alembic_reconnect_hydration.py`.

## Explanation

**Failure mode the harness catches**

1. Schema from SQLAlchemy/Alembic (`sa.Numeric()`, `sa.DateTime()`, etc.) —
   SQLite storage affinity differs from Rust `auto_migrate` DDL.
2. Same-session `create()` returns the identity-map instance (no DB re-read).
3. `reset_engine()` + reconnect forces `materialize_engine_row` and
   `engine_value_to_rust_value` to run on stored affinities.

**Harness shape**

- `pytest.mark.sqlite_only` so CI always runs SQLite even when Postgres is off.
- `aiosqlite` + `greenlet` in `ci-test` / `dev` — `importorskip` otherwise hides regressions.
- Assert both Python attribute and raw `sqlite3` `typeof()` when affinity matters.

**When to extend the matrix**

Add a case when a typed column has a narrow `match` in `engine_value_to_rust_value`
or only handles `EngineValue::String` while SQLite may emit `I64` / `F64` / `Bytes`.

## When to apply

- New bridge mapping in `engine_value_to_rust_value` or `materialize_engine_row`.
- User reports "works after insert, wrong after new process / CLI command".
- Bug repro mentions Alembic, `auto_migrate=False`, or `typeof(...) = 'integer'`.

## Related

- `docs/solutions/issues/sqlite-null-hydrates-as-int-zero.md` (#56)
- `docs/solutions/issues/sqlite-integer-decimal-hydrates-as-none.md` (#58)
- `docs/solutions/patterns/typed-null-binds.md` — write-path NULL typing (separate layer)
