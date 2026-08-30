---
title: Exclusive stepwise compare is the after() expansion
type: pattern
tags: [query, paging, ir]
related_files:
  - src/query.rs
  - src/ferro/query/builder.py
  - src/ferro/query/wire.py
related_issues: [393, 394, 395]
captured: 2026-08-30
---

## Problem

`after(position)` must render as an exclusive keyset bound — `(a > :a) OR (a = :a AND b > :b)` with DESC flipping `>` to `<` — without turning paging into a `where()` predicate and without copying that tree into every SELECT walker. #394 will add NULL-bucket expansion to the same bound.

## Takeaway

One function owns the compare tree: `exclusive_stepwise_compare` in `src/query.rs`. The SELECT walker qualifies columns, binds typed values, and ANDs the result onto WHERE. Do not sprinkle inequalities in `operations.rs`. #394 extends this function; it does not add a second expander.

Python validates the wedge at `after()` / `position_of()` (root columns, PK included, non-nullable) and `compile_query` is the only assembler that puts `after` on the fetch payload as typed `kind`/`value` nodes. Count omits the key; mutations reject it.

Datetime slots go through `_serialize_query_value` → pydantic JSON mode (`…Z` for UTC), the same bytes `save()` writes. `datetime.isoformat()` emits `…+00:00`; on SQLite that is a different TEXT value, so the prefix-equality arm of the stepwise compare never matches.
