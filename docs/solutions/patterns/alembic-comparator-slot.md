---
title: Ferro Alembic comparators declare FIRST or LAST, never default MEDIUM
type: pattern
tags: [convention, invariant, gotcha, migrations, alembic, schema]
related_files:
  - AGENTS.md
  - src/ferro/migrations/alembic.py
  - tests/test_table_check_reconcile.py
  - tests/test_row_security_alembic.py
related_issues: [423, 427, 429]
related_prs: [431]
captured: 2026-09-15
---

## Problem

A generated revision that adds a column and a table check over it can emit
`ADD CONSTRAINT` **before** `ADD COLUMN`. Postgres then rejects the upgrade
because the check names a column that does not exist yet (#423).

## Takeaway

Every Ferro schema comparator must declare `priority=FIRST` (before-tables) or
`priority=LAST` (after-tables). Default `MEDIUM` is an I-12 violation. Alembic's
own table comparator may stay in `MEDIUM` — that is how table ops land in the
middle.

## Why MEDIUM lies

Alembic runs comparators in three buckets (`FIRST` 50 → `MEDIUM` 25 → `LAST`
10). Omit `priority=` and you get `MEDIUM`.

Ferro registers comparators at **import time** on the global dispatcher.
When autogenerate actually runs, Alembic then `populate_with`s its plugin
comparators — including `_autogen_for_tables` — into the same `(schema,
MEDIUM)` bucket *after* that registration. Ferro's `MEDIUM` comparators
therefore run first and `extend` `upgrade_ops.ops` while the list still has
no `create_table` / `ModifyTableOps`.

A comment that says "we append after table ops" is not a slot. Checks shipped
with that comment and still ran first.

## Slots

| Family | Slot | How |
| --- | --- | --- |
| Enum label addition | Before-tables | `FIRST` and insert at `ops[:0]` |
| Check add / rebuild / drop | After-tables | `LAST` then append |
| Row security | After-tables | `LAST` then append |

Two `LAST` families keep registration order in the Alembic adapter: checks,
then row security.

## When to apply

Adding a Ferro autogenerate comparator: pick a slot on the decorator, and add
a same-revision pin if the family is after-tables (column+check, or
create-table+policy). Do not register into default `MEDIUM` and hope insert
or extend lands in the right place.

See AGENTS.md I-12. Architecture review: I-19 in #427. PRD #429.
