---
title: Exclusive stepwise compare is the after() expansion
type: pattern
tags: [query, paging, ir]
related_files:
  - src/query.rs
  - src/ferro/query/builder.py
  - src/ferro/query/wire.py
related_issues: [393, 394, 395, 396]
captured: 2026-08-30
---

## Problem

`after(position)` / `before(position)` must render as an exclusive keyset bound — `(a > :a) OR (a = :a AND b > :b)` with DESC flipping `>` to `<` — without turning paging into a `where()` predicate and without copying that tree into every SELECT walker. #394 folds each key's direction, null placement, and whether the bound is NULL into the same tree so a cursor can cross the NULL bucket in one query. #395's `before()` inverts each order key (`asc`↔`desc`, `first`↔`last`, `"native"` stays `"native"`), runs that same tree, fetches, then reverses the hydrated rows so the caller sees the declared order.

## Takeaway

One function owns the compare tree: `exclusive_stepwise_compare` in `src/query.rs`. The SELECT walker qualifies columns, binds typed values, and ANDs the result onto WHERE. Do not sprinkle inequalities in `operations.rs`. #394 extends this function; it does not add a second expander. #395 does not add a second expander either — `before_condition` inverts keys then calls the same function. `"native"` resolves inside that function from `Dialect` (Postgres: NULL is larger; SQLite: NULL is smaller).

Python validates the wedge at `after()` / `before()` / `position_of()` (root or traversed columns, PK included; `None` legal in every non-PK slot) and `compile_query` is the only assembler that puts `after` / `before` on the fetch payload as typed `kind`/`value` nodes, including `kind: "null"`. Count omits the keys; mutations reject them. Column nullability is not consulted — a `left_join`'d NOT NULL related column may still be NULL when the relation is missing. Do not invent a second expander: path-carrying `order_by` terms already go through `qualify_column_with_joins`.

Datetime slots go through `_serialize_query_value` → pydantic JSON mode (`…Z` for UTC), the same bytes `save()` writes. `datetime.isoformat()` emits `…+00:00`; on SQLite that is a different TEXT value, so the prefix-equality arm of the stepwise compare never matches.
