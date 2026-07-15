---
title: Self-referential FK evicted its whole component from CREATE TABLE order
type: issue
tags: [rust, schema, migrations, gotcha, postgres]
related_files:
  - crates/ferro-migrate/src/order.rs
  - crates/ferro-migrate/src/emit.rs
  - src/schema.rs
related_issues: [302]
captured: 2026-07-15
---

# Self-referential FK evicted its whole component from CREATE TABLE order

**Problem:** On a fresh Postgres schema, `auto_migrate` created a referrer
table before its FK target whenever the target carried a self-referential FK —
the inline `REFERENCES` failed with `relation "..." does not exist`.

**Takeaway:** In a dependency sort, an edge to your own table is already
satisfied by your own `CREATE TABLE`; treating it as a pending dependency makes
the table permanently ineligible, stalls everything downstream of it, and drops
the whole component into the cycle fallback (input/alphabetical order).

## What happened

Both ordering loops (`order_models_for_create` in `ferro-migrate` and
`order_models_for_migration` in `src/schema.rs`) only scheduled a table once
every `fk.to_table` was in the `created` set. A self-FK's target can never be
in `created` before the table itself, so the self-FK table never became
eligible, its dependents stalled behind it, and the no-progress fallback
appended the remainder in incoming order. Any referrer sorting before its
target then failed on Postgres. Existing schemas survived by alphabetical
luck.

Two things let the bug survive review:

1. The ordering loop existed in **two hand-rolled copies** with the same flaw.
   Both now delegate to one primitive:
   `ferro_migrate::order_by_dependencies` (`crates/ferro-migrate/src/order.rs`),
   where the self-edge rule lives exactly once.
2. A doc comment claimed cycles were harmless because "Postgres `CREATE TABLE`
   with a forward inline FK reference is the pre-existing behavior" — false.
   Postgres rejects forward references at CREATE time; only SQLite tolerates
   them. Don't trust ordering-doesn't-matter claims without a Postgres test.

Genuine cross-table cycles (A→B→A) still fall through in input order and fail
on Postgres — a real fix needs post-create `ALTER TABLE ADD CONSTRAINT`
deferral (follow-up tracked from #302).

## How to recognize

- `relation "X" does not exist` during `auto_migrate` on a fresh Postgres
  schema, where X *is* among the registered models.
- The failure appears/disappears when a self-FK is added/removed anywhere in
  the dependency component, or when table names are renamed across the
  alphabetical boundary.
- SQLite passes while Postgres fails: SQLite accepts forward FK references, so
  ordering bugs are invisible there — regression tests for CREATE order must
  run on the Postgres matrix.
