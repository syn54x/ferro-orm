---
title: Query and update literals share one scalar helper with save()
type: pattern
tags: [query, paging, pydantic, invariant]
related_files:
  - src/ferro/_bind_payload.py
  - src/ferro/query/nodes.py
  - src/ferro/query/wire.py
  - src/ferro/query/builder.py
  - src/ferro/raw.py
  - AGENTS.md
related_issues: [428, 430, 938, 976, 981]
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
  `to_json` (enums still stringify on that path).
- Many-to-many keys: for `add`, `remove` and `clear`,
  `Query._m2m_owner_key` runs the helper on the owner's key and
  `Query._m2m_keys` on each related key in `src/ferro/query/builder.py`;
  the read of the relation carries the owner's key in the payload
  `to_wire_json` canonicalizes. Every statement sends the value `save()`
  wrote, and the core binds it by the join column's storage
  (`join_key_expr`), with no cascade of its own.
- Raw SQL: `_marshal` in `src/ferro/raw.py` calls the helper for those
  five types, so a raw bind on SQLite matches the text `save()` stored
  (#976).
- Offset refusal: `refuse_offset_seconds` in `src/ferro/_bind_payload.py`
  raises for a `datetime` / `time` whose UTC offset is not whole minutes
  (the stored text spells it to the minute). Its two callers cover every
  path: `canonicalize_wire_scalar` (literals, `update()`, raw binds), once
  on the whole value, and `save_bind_payload`, once on each field's value
  as the instance holds it (#981). It searches the value as pydantic's
  python-mode dump gives it (models and dataclasses become dicts, datetimes
  stay objects), then every mapping key and value and every collection
  item, so a shape pydantic can serialize is covered without a container
  list of Ferro's own. Do not add per-type arms to it.
- `save()`: `save_bind_payload` stays `model_dump(mode="json")` plus a
  raw bytes overlay — do not route save through the per-value helper.

## How to recognize

A new `isoformat()` / `str(uuid)` / `str(Decimal)` arm in the query or
update marshal path — even with a comment that it “matches save” — is an
I-13 violation. Add the type to the helper instead. A wrapper that only
forwards to the helper is a second name for the same cascade; call
`canonicalize_wire_scalar` directly.
