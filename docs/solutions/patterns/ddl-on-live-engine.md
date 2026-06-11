---
title: DDL on a live engine — pool refresh, unprepared SQL, identity-map eviction
type: pattern
tags: [invariant, rust, sqlx, pool, statement-cache, auto-migrate, identity-map]
related_files:
  - src/backend.rs
  - src/migrate.rs
  - src/introspect.rs
  - tests/test_auto_migrate.py
related_issues: [67, 68]
captured: 2026-06-11
---

## Problem

Running `ALTER TABLE` against a pool that has already served queries leaves
three kinds of stale state, each producing a different failure:

1. **sqlx statement caches (SQLite):** a cached prepared statement carries the
   pre-DDL column count. Re-executing it after the table widens panics the
   sqlx worker thread (`row.rs index out of bounds`) and the query **silently
   returns zero rows** — no exception reaches Python (issue #67).
2. **Prepared statements (Postgres):** the server raises
   `cached plan must not change result type`.
3. **Ferro's identity map:** instances hydrated before the DDL lack fields the
   migration added. A later load returns the same cached instance, so the new
   field falls through to the class-level `FieldProxy` instead of its value.

## Pattern

Auto-migrate (`src/migrate.rs::internal_migrate`) treats DDL as a
schema-epoch event with three guarantees, in order:

1. **Migration SQL never populates caches.** All introspection and DDL go
   through `EngineHandle::execute_sql_unprepared` /
   `fetch_all_sql_unprepared*` (`sqlx ... .persistent(false)`), so the
   migration cannot poison the connection it borrowed.
2. **`EngineHandle::refresh_pool()` after any ALTER/DROP.** The pool lives
   behind `Arc<RwLock<BackendPool>>` with an owned `PoolSpec` (URL,
   search_path, sizing), so the engine atomically swaps in a freshly built
   pool and gracefully closes the old one. No connection — idle or checked
   out — can serve a statement prepared against the pre-DDL schema afterward.
   * In-memory SQLite databases live inside their connections; swapping the
     pool would destroy the database. There, refresh acquires **every**
     connection (waiting for outstanding work to drain) and calls
     `clear_cached_statements()` on each — same guarantee, data intact.
   * Handles wrapped around externally built pools (`new_sqlite` /
     `new_postgres`, test-only) have no `PoolSpec` and fail loudly.
3. **`IDENTITY_MAP.clear()` after any ALTER/DROP.** The schema lives in the
   database, which any number of named connections may share, so the whole
   map is invalidated. Eviction is always safe — instances re-hydrate on the
   next load.

The connect path gets guarantee zero for free: migration runs immediately
after pool creation, before the connection can serve ORM queries.

## Gotchas

- `refresh_pool` is the primitive for any future DDL surface (e.g. detecting
  DDL in `ferro.raw.execute` for the rest of issue #67). Do not reinvent
  partial cache flushes — draining "idle" connections only is exactly the
  stop-gap this design replaced.
- The regression tests are
  `tests/test_auto_migrate.py::test_migrate_updates_adds_missing_columns_and_hydrates`
  (#67 repro shape: narrow table → connect with `migrate_updates=True` → rows
  hydrate) and
  `::test_manual_migrate_on_live_pool_refreshes_cached_statements`
  (statement cached pre-migrate, correct rows post-migrate).
