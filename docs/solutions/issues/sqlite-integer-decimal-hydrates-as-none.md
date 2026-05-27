---
title: SQLite INTEGER-backed NUMERIC Decimal hydrates as None after reconnect
type: issue
tags: [gotcha, sqlite, hydration, bridge, rust, sqlalchemy, alembic, decimal, ffi]
related_files:
  - src/operations.rs
  - src/backend.rs
  - tests/test_sqlite_alembic_reconnect_hydration.py
related_issues: [58]
related_prs: [59]
captured: 2026-05-18
---

## Problem

Non-null `Decimal` fields can appear as Python `None` after `ferro.reset_engine()`,
a new CLI process, or any fetch on a fresh connection — even though `sqlite3`
shows the value in the row (often with `typeof(column) = 'integer'` for whole
numbers). Same-session `create()` still returns `Decimal('3')`.

## Takeaway

`engine_value_to_rust_value` in `src/operations.rs` must map `EngineValue::I64`
to `RustValue::Decimal` for decimal schema columns, not only `F64` and `String`.
SQLite stores whole-number NUMERIC values with INTEGER affinity; fractional
values use REAL and already worked.

## Explanation

**Causal chain**

1. Alembic/SQLAlchemy maps Python `Decimal` to `sa.Numeric()`; SQLite often
   stores `Decimal(3)` with INTEGER affinity and `Decimal("1.5")` with REAL.
2. `materialize_engine_row` (`src/backend.rs`) correctly decodes non-null
   INTEGER as `EngineValue::I64(3)` (after the #56 NULL-first fix).
3. `engine_value_to_rust_value` handled decimal columns only for `F64` and
   `String`; `I64` hit `_ => RustValue::None`.
4. Python hydration saw `None`, so `e.hours` formatting crashed downstream.

**Why same-session `create()` looked fine**

The identity map returns the in-memory instance from insert without re-reading
the row from SQLite (same masking pattern as issue #56).

**Fix (PR #59)**

```rust
if is_decimal {
    return match value {
        EngineValue::I64(v) => RustValue::Decimal(v.to_string()),
        EngineValue::F64(v) => RustValue::Decimal(v.to_string()),
        EngineValue::String(v) => RustValue::Decimal(v),
        _ => RustValue::None,
    };
}
```

**Tests**

- Rust: `engine_value_to_rust_value_tests` in `src/operations.rs` (decimal I64/F64/String).
- Python: `tests/test_sqlite_alembic_reconnect_hydration.py` — Alembic
  `create_all`, `auto_migrate=False`, `reset_engine()`, then fetch. Covers
  integer/real/zero/null decimal, plus datetime/date/bool/uuid/json regressions.
- Requires `aiosqlite` and `greenlet` in `ci-test` / `dev` so CI does not skip.

## How to recognize

- Bug only on **SQLite**, often with **Alembic-created** schema (`auto_migrate=False`).
- Raw SQL shows a non-null numeric value; Python attribute is `None` after reconnect.
- `typeof(column)` is `integer` for whole numbers; fractional values often work.
- Same process right after `create()` is correct; `reset_engine()` or new process fails.
- Distinct from #56: there the DB value is SQL `NULL` but Python saw `int(0)`.

## Prevention

- Any change to `engine_value_to_rust_value` decimal branch: add Rust tests for
  every `EngineValue` variant SQLite can emit (`I64`, `F64`, `String`, `Null`).
- Add Alembic + reconnect integration tests for typed columns — not only
  `auto_migrate=True` same-session CRUD (see `tests/test_sqlite_alembic_reconnect_hydration.py`).
- Do not paper over `None` in Python for `Decimal` fields; fix bridge mapping.

## Related

- GitHub issue: https://github.com/syn54x/ferro-orm/issues/58
- PR: https://github.com/syn54x/ferro-orm/pull/59
- `docs/solutions/issues/sqlite-null-hydrates-as-int-zero.md` — sibling SQLite fetch bug (#56)
- `docs/solutions/patterns/typed-null-binds.md` — NULL on the write path (distinct layer)
