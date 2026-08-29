---
title: Json-family factory defaults are absent from JSON Schema
type: pattern
tags: [convention, gotcha, schema, migrations, pydantic, python]
related_files:
  - src/ferro/columns.py
  - crates/ferro-ddl-lowering/src/lib.rs
  - crates/ferro-migrate/src/emit.rs
related_issues: [370, 373]
captured: 2026-08-29
---

## Problem

`migrate_updates` backfills a new NOT NULL column from `ColumnSpec.default`,
which used to be `model_json_schema()["properties"][name]["default"]`.
Pydantic puts `Field(default={})` in that key and **omits**
`Field(default_factory=dict)`. Opening the object/array arm in the lowering
helper would fix `default={}` and still refuse Pinch's actual declaration.

Evaluating every `default_factory` is the other trap: `uuid4` and
`datetime.now` JSON-serialize, and one frozen UUID/timestamp would land on
every existing row.

## Takeaway

Snapshot factories only on json-family fields (`dict` / `list` / nested
model), once, at column-spec compile. Store the JSON dump on the same
`default` fact `Field(default={})` already uses. Scalar factories are never
called. ADD COLUMN renders `'{}'::jsonb` / `'{}'::json` / SQLite `'{}'` from
resolved storage, then drops the default on Postgres (SQLite lingers).
CREATE TABLE still does not emit Field defaults as server defaults.

A factory that raises, needs arguments, or cannot JSON-dump leaves `default`
unset. The loud failure is ADD COLUMN's existing refusal (names the column),
not class-definition / import — a factory that is legal Python stays legal
Python.
