---
title: SQLite NULL columns hydrate as int 0 after reconnect (Alembic DATETIME)
type: issue
tags: [gotcha, sqlite, hydration, bridge, rust, sqlalchemy, alembic, datetime, ffi]
related_files:
  - src/backend.rs
  - src/operations.rs
  - tests/test_sqlite_null_hydration.py
related_issues: [56]
related_prs: [57]
captured: 2026-05-18
---

## Problem

Optional fields that are `NULL` in a SQLite database (for example
`archived_at: datetime | None`) sometimes appear as Python `int(0)` after a
**new connection** — `ferro.reset_engine()`, a new CLI process, or any fetch
that does not hit the identity map. The same row shows `None` on the connection
that just inserted it, and `sqlite3` confirms the column is `NULL`.

## Takeaway

`materialize_engine_row` in `src/backend.rs` must call `ValueRef::is_null()` on
the raw column **before** typed `try_get` decoding. On SQLite, `try_get::<i64>`
on SQL `NULL` often succeeds with `0` for INTEGER/NUMERIC-affinity columns
(including Alembic `DateTime()` → `DATETIME`). That is not limited to datetimes:
`INTEGER`, `REAL`, `TEXT`, and `NUMERIC` NULLs were all misread as `0` before
the fix.

## Explanation

**Causal chain**

1. Alembic/SQLAlchemy creates nullable `archived_at` as `DATETIME` (numeric
   affinity on SQLite).
2. Insert leaves the column SQL `NULL`.
3. Fetch uses `materialize_engine_row`, which tried `try_get::<i64>` first.
4. sqlx returns `Ok(0)` for NULL on those affinities instead of an error.
5. `engine_value_to_rust_value` maps `EngineValue::I64` to `RustValue::BigInt`
   (date-time `format` only applies to `EngineValue::String`).
6. Python sees `0`, so `archived_at is None` filters fail.

**Why same-session `create()` looked fine**

The identity map returns the in-memory instance from insert without re-reading
the row from SQLite.

**Fix (PR #57)**

```rust
let value = match row.try_get_raw(ordinal) {
    Ok(raw) if raw.is_null() => EngineValue::Null,
    Ok(_) | Err(_) => decode_non_null_engine_value(row, ordinal),
};
```

Legitimate integer `0` is unchanged: SQL `NULL` is detected via `is_null()`, not
by treating `0` as missing.

**Tests**

- Rust: `engine_handle_fetches_sqlite_null_columns_as_null_not_zero`,
  `engine_handle_fetches_sqlite_non_null_zero_integer` in `src/backend.rs`.
- Python: `tests/test_sqlite_null_hydration.py` (Alembic `create_all` + reconnect).
  Requires `aiosqlite` and `greenlet` in `ci-test` / `dev` so CI does not skip.

## How to recognize

- Bug only on **SQLite**, often with **Alembic-created** schema (`auto_migrate=False`).
- `field is None` checks fail while raw SQL shows `NULL`.
- Same process right after `create()` is correct; new connection or `reset_engine()` is wrong.
- Affected value is `0` (int), not a datetime string or `AttributeError`.
- Raw `ferro.raw.fetch_all` shows the same `0` — failure is in row materialization, not Pydantic coercion alone.

## Prevention

- Any change to `materialize_engine_row` or `EngineValue` decoding: add a Rust
  test that inserts `DEFAULT VALUES` into a nullable column and asserts
  `EngineValue::Null`.
- Do not “fix” optional datetimes in Python by treating `0` as unset — fix NULL
  detection at the bridge.
- Related but distinct: typed **bind** NULLs for Postgres are documented in
  `docs/solutions/patterns/typed-null-binds.md` (`NullKind`, insert/update paths).
  Fetch-time NULL handling is separate; both layers need correct typing.

## Related

- GitHub issue: https://github.com/syn54x/ferro-orm/issues/56
- PR: https://github.com/syn54x/ferro-orm/pull/57
- `docs/solutions/patterns/typed-null-binds.md` — NULL on the **write** path
- `docs/solutions/issues/pydantic-slots-missing-after-ferro-hydration.md` — other hydration footguns
