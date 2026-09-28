---
title: A three-or-more-term OR/AND table check reads as drifted forever
type: issue
tags: [rust, schema, migrations, alembic, gotcha, postgres]
related_files:
  - crates/ferro-ddl-lowering/src/lib.rs
  - tests/test_table_check_rebuild.py
  - docs/adr/0015-table-check-body-drift.md
related_issues: [437]
captured: 2026-09-28
---

# A three-or-more-term OR/AND table check reads as drifted forever

**Problem.** A table check built from three or more `|` (or `&`) terms was rebuilt on
every `connect(migrate_updates=True)` and showed up as a DROP + ADD in every
`alembic revision --autogenerate`, against a database ferro had built a second earlier.

**Takeaway.** Postgres stores a left-nested chain of one boolean connective as one
n-ary node and prints it flat. The drift normalizer must compare associative grouping
as noise, the same way it already treats wrapping parens and identifier quotes.

## What the two sides looked like

ferro renders the lambda as a binary tree, folded left the way Python evaluates it:

```sql
((("a" IS NOT NULL) OR ("b" IS NOT NULL)) OR ("c" IS NOT NULL)) OR ("d" IS NOT NULL)
```

`pg_get_constraintdef` for the constraint that statement created:

```sql
CHECK (((a IS NOT NULL) OR (b IS NOT NULL) OR (c IS NOT NULL) OR (d IS NOT NULL)))
```

Strip the quotes, whitespace and outer parens and the two strings still differ by
the inner grouping parens, so `drifted_check_names` reported a rebuild.

The two-term pin (`catalog_shaped_transfer_check_normalizes_equal_to_rendered_body`)
never reached this: with two operands there is nothing to fold.

## The fix

`normalize_check_definition` now ends with `flatten_associative_chains`: parse the
tokens into a parenthesis tree, and at each level splice a parenthesized operand into
the chain around it when both are chains of the same connective. An operand is a
group at the start of the sequence or right after the connective, and at the end or
right before it. A group after `NOT`, after `IN`, after a function name, or after a
comparison operator is that construct's argument and keeps its parentheses. `AND`
nested in `OR` (and the reverse) also keeps them: precedence grouping is the predicate.

Postgres only folds the *left* spine (`a OR (b OR c)` stays nested in the catalog).
The normalizer flattens both sides, so a right-nested lambda compares equal to its
catalog form too.

## How to recognize

- A rebuild or an autogenerate DROP + ADD whose ADD body equals the live body
  except for parentheses around the first n-1 terms of an `OR`/`AND` chain.
- `pg_constraint.oid` for a `ck_*` constraint changes across a `migrate_updates`
  boot on an unchanged model.
- When adding a new shape to the check renderer, capture real
  `pg_get_constraintdef` output for it and pin the normalizer against that string.
