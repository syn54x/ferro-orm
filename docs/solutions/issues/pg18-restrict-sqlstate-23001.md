---
title: PG18 RESTRICT is SQLSTATE 23001, not 23503
type: issue
tags: [gotcha, rust, postgres, sqlite, ffi, sqlx]
related_files:
  - src/errors.rs
  - src/ferro/exceptions.py
  - tests/test_exception_mapping.py
related_issues: [306, 336, 337]
captured: 2026-08-15
---

# PG18 RESTRICT is SQLSTATE 23001, not 23503

**Problem:** Deleting a parent still referenced by `ForeignKey(on_delete="RESTRICT")` raised `ForeignKeyViolationError` on PostgreSQL 17 and `OperationalError` on PostgreSQL 18.

**Takeaway:** Ferro owns the integrity-code table. Do not ask sqlx `kind()` alone, and do not match driver message text. `23001` (PG18 RESTRICT) and SQLite `1811` (RESTRICT implemented as a trigger) are `ForeignKeyViolationError`. `sqlstate` stays the raw driver code.

## What happened

PostgreSQL 18 changed `ON DELETE` / `ON UPDATE RESTRICT` from `23503` (`foreign_key_violation`) to `23001` (`restrict_violation`). The message picked up "RESTRICT setting of" at the same time — that is the symptom, not the cause.

sqlx 0.8 and 0.9 still map only `23503` → `ErrorKind::ForeignKeyViolation`. `23001` is `Other`. Ferro's mapper used `kind()`, so the delete path fell through to `OperationalError`. Inserting a dangling FK stayed `23503` on both majors, which is why the existing INSERT test never caught this. CI is `postgres:17`.

SQLite has a parallel hole: RESTRICT parent-delete reports extended result code `1811` (`SQLITE_CONSTRAINT_TRIGGER`), not `787` (`SQLITE_CONSTRAINT_FOREIGNKEY`). sqlx `kind()` is `Other` there too. The driver message still says `FOREIGN KEY constraint failed`.

The table lives in `exception_name_for_database` (`src/errors.rs`). Known codes win; `kind()` is the fallback. `map_db_error` still copies `db.code()` onto `sqlstate` unchanged — do not rewrite `23001` to `23503`.

`23000` (generic integrity) and `23P01` (exclusion) stay `OperationalError` until they get their own type decision.

## How to recognize

A RESTRICT parent-delete raises `OperationalError` instead of `ForeignKeyViolationError`, especially when CI is PG17 and someone is on PG18. Check `exc.sqlstate`: `23001` on Postgres, `1811` on SQLite. If classification missed, `sqlstate` should still be set — `None` is a second bug (the code was dropped on the wrap).
