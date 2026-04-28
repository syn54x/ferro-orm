---
title: index=True + unique=True is redundant
type: pattern
tags: [convention, schema, validation]
related_files:
  - src/ferro/base.py
  - src/ferro/schema_metadata.py
  - docs/guide/relationships.md
related_issues: [32]
related_prs: [36]
captured: 2026-04-28
---

## Problem

A user might write `ForeignKey(related_name="...", unique=True, index=True)` or
`FerroField(unique=True, index=True)` thinking they need to opt into both. But
`UNIQUE` constraints in SQL implicitly create an underlying B-tree index — so
emitting both produces two indexes on the same column, doubling write
amplification and disk usage.

## Takeaway

Ferro **silently strips `index=True` when `unique=True` is also set** and
issues a `UserWarning` so the user notices.

```python
# In ForeignKey.__init__ (mirror this pattern for any new flag combo):
self.unique = unique
self.index = index
if unique and index:
    warnings.warn(
        "ForeignKey(unique=True, index=True) is redundant; unique=True "
        "already implies an index. Ignoring index=True.",
        UserWarning,
        stacklevel=2,
    )
    self.index = False
```

`stacklevel=2` is critical — it points the warning at the user's model
declaration, not at Ferro's `__init__`.

## How to recognize

- Code review surfaces a constructor that accepts both `unique` and `index`
  flags without checking redundancy → flag it.
- A schema dump shows two indexes for the same column with names like
  `uq_table_col` and `idx_table_col` → the warning silencer was bypassed.

## Why a warning, not an error

The combination is harmless once stripped — refusing to construct the field
would break user code that sets the flags from data (e.g. driven by a config).
A warning preserves ergonomics while making the redundancy visible.
