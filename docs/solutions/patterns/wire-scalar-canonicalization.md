---
title: Query and update literals share one scalar helper with save()
type: pattern
tags: [query, paging, pydantic, invariant]
related_files:
  - src/ferro/_bind_payload.py
  - src/ferro/query/nodes.py
  - AGENTS.md
related_issues: [428, 430]
captured: 2026-09-15
---

## Problem

`save()` writes UTC datetimes as pydantic JSON (`…Z`). Query literals used
to have a leftover `.isoformat()` arm (`…+00:00`). On SQLite those are
different TEXT bytes, so `after((the_datetime_I_saved, pk))` can skip the
cursor row.

## Takeaway

`canonicalize_wire_scalar` is the only cascade for `datetime` / `date` /
`time` / UUID / Decimal on query and `update()` literals. Its output is
pinned against `save_bind_payload`. `save()` stays `model_dump`. Anything
else with `.isoformat()` raises. Enums on the query wire stay enums (I-13).

## How to recognize

A new `isoformat()` / `str(uuid)` / `str(Decimal)` arm in the query or
update marshal path — even with a comment that it “matches save” — is an
I-13 violation. Add the type to the helper instead.
