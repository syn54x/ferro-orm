---
title: index=True + unique=True is redundant
type: pattern
tags: [convention, schema, validation]
related_files:
  - src/ferro/base.py
  - docs/pages/guide/relationships.md
related_issues: [32]
related_prs: [36]
captured: 2026-04-28
---

## Problem

A user might write `Field(foreign_key=Team, unique=True, index=True)` or
`Field(unique=True, index=True)` thinking they need to opt into both. But
`UNIQUE` constraints in SQL implicitly create an underlying B-tree index — so
emitting both produces two indexes on the same column, doubling write
amplification and disk usage.

## Takeaway

Declare `unique=True` alone: the unique constraint is the index.

```python
class Profile(Model):
    id: int | None = Field(default=None, primary_key=True)
    player_id: int = Field(foreign_key=Player, unique=True)
    player: Relation[Player] = Forward("player_id")
```

> 2026-11-09: until #874 Explicit foreign-key columns, with relations as
> typed views, the `ForeignKey` annotation warned on `unique=True` with
> `index=True` and dropped the index (`stacklevel=2`, so the warning pointed at
> the model declaration). That annotation was removed by #874, and the warning
> with it: `Field(unique=True, index=True)` emits both `uq_<table>_<col>` and
> `idx_<table>_<col>`, on a foreign-key column as on any other.

## How to recognize

- A schema dump shows two indexes for the same column with names like
  `uq_table_col` and `idx_table_col` → `index=True` sits beside `unique=True`
  on that field; remove it.
- Code review surfaces a declaration that sets both flags → flag it.
