---
title: Postgres drift on a SQL body is a printed pair, never a normalizer
type: pattern
tags: [rust, schema, migrations, postgres, sqlite, invariant, convention]
related_files:
  - src/introspect.rs
  - src/live_ir.rs
  - crates/ferro-migrate/src/plan.rs
  - crates/ferro-ddl-lowering/src/lib.rs
  - tests/test_generated_column_drift.py
  - docs/adr/0060-postgres-prints-both-sides-of-a-sql-body-ferro-compares.md
related_issues: [563, 789, 790, 795, 796]
captured: 2026-10-28
---

# Postgres drift on a SQL body is a printed pair, never a normalizer

**Problem.** Postgres stores a check body or a generated expression in its own
printing, with casts that depend on its catalog, so Ferro's rendering and the
catalog's text differ on a body nobody touched:

```
declared:  CAST(sign(CAST("amount_minor" AS bigint)) AS bigint)
catalog:   (sign(((amount_minor)::bigint)::double precision))::bigint
```

**Takeaway.** Do not teach a normalizer the casts. Ask Postgres to print both
sides through the same analysis and compare the two prints: equal prints are
no drift.

## What is compared

For each live table holding something to compare, the live read
(`printed_pairs` in `src/introspect.rs`) sends one statement:

```sql
EXPLAIN (VERBOSE, FORMAT JSON)
SELECT <declared rendering>, <catalog reprint>, ... FROM "transaction"
```

Each pair is a `PrintedPair { declared, catalog }` in the live facts
(`crates/ferro-migrate/src/plan.rs`). Both sides are the plan's output
expressions, so Postgres resolves functions and adds implicit casts to both
the same way. `sign(bigint)` resolving to `sign(double precision)` shows on
both sides or on neither. A cast in the reprint is therefore not drift. A
hand edit (`ALTER TABLE ... SET EXPRESSION AS (sign(amount_minor) * 2)`)
prints differently and reads as `transaction.direction generated expression
differs`.

Two families use it: ferro-owned checks (`printed_pair_checks`) and generated
columns (`printed_pair_column`). A live `VIRTUAL` column (Postgres 18), or a
plain column where the model declares a generated one, differs without a
pair. Views will reuse the same comparison.

## The read-only rule

The statement runs inside `BEGIN READ ONLY` on a pinned connection and is
rolled back. It creates nothing, so `ferro migrate drift` works under a role
with only `SELECT` and `default_transaction_read_only=on`, and against a
replica. A temp table would fail there (`cannot execute CREATE TABLE in a
read-only transaction`), which is why that design was rejected. A role
without `SELECT` on the table is refused with sqlstate `42501`, naming the
fix: `GRANT SELECT ON "<table>" TO <role>`.

## The missing-pair rule

A pair is read against the declaration the plan is made toward. If a live
generated column or ferro-owned check that the rule gives a pair arrives
without one, planning fails (`PlanError::MissingPrintedPair`,
`MissingCheckPrintedPair`) instead of guessing; the message says to read the
facts again with `_core._live_schema_ir(using, declared_json)`. A column or
check reading a column that is not live yet gets no pair, by the same rule
on both sides: the plan adds it or changes it instead.

`ferro migrate new` compares two declared snapshots and never needs a pair,
since no catalog is involved.

## What SQLite does instead

SQLite stores the body as written, so there is nothing to print. A check
is compared with `normalize_check_definition` against the stored text, and
a generated column's definition, its `_ferro_range_<col>` guard included,
is compared with Ferro's rendering, whitespace folded. Row-policy drift is
Postgres-only and keeps its own normalizer, `normalize_row_policy_expr`.

## When to apply

- You add a new SQL body Ferro owns and must detect drift on (a view, an
  index expression): add it to the printed pair on Postgres. Do not write a
  cast table.
- A drift report shows a body that differs only in casts or parentheses:
  check that the comparison is reading printed pairs and not the raw catalog
  text.
- Superseded: the cast-folding rules of #563 and the n-ary flattening in
  [`n-ary-check-chain-reads-as-drift.md`](../issues/n-ary-check-chain-reads-as-drift.md)
  no longer decide Postgres drift.
