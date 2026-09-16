---
title: Query and update literals share one scalar helper with save()
type: pattern
tags: [query, paging, pydantic, invariant]
related_files:
  - src/ferro/_bind_payload.py
  - src/ferro/query/nodes.py
  - src/ferro/query/wire.py
  - AGENTS.md
related_issues: [428, 430]
captured: 2026-09-15
---

## Problem

SQLite stores datetime columns as TEXT, so `after((the_datetime_I_saved, pk))`
misses the cursor row if the query sends `…+00:00` while `save()` wrote `…Z`.

## Takeaway

`canonicalize_wire_scalar` is the only cascade for `datetime` / `date` /
`time` / UUID / Decimal on query and `update()` literals; pin it against
`save_bind_payload` (I-13).

## Where it lives

- Helper: `canonicalize_wire_scalar` in `src/ferro/_bind_payload.py` —
  pydantic `to_json` then JSON-decode for those five types; containers
  recurse; bytes / `Enum` / int / str / bool / `None` pass through;
  any other `.isoformat()` object raises.
- Query leaves and position slots: `QueryNode.to_ir_dict` in
  `src/ferro/query/nodes.py` and `_after_query_values` /
  `to_wire_json` in `src/ferro/query/wire.py`.
- `update()`: `update_bind_payload` runs the helper, then dict-level
  `to_json` (enums still stringify on that door).
- `save()`: `save_bind_payload` stays `model_dump(mode="json")` plus a
  raw bytes overlay — do not route save through the per-value helper.

## How to recognize

A new `isoformat()` / `str(uuid)` / `str(Decimal)` arm in the query or
update marshal path — even with a comment that it “matches save” — is an
I-13 violation. Add the type to the helper instead. A wrapper that only
forwards to the helper is a second name for the same cascade; call
`canonicalize_wire_scalar` directly.
