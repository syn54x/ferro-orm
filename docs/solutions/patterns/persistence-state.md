---
title: Explicit persistence state drives save() semantics
type: pattern
tags: [python, mutations, invariant, gotcha]
related_files:
  - src/ferro/models.py
  - src/operations.rs
  - tests/test_save_semantics.py
related_issues: [170, 174]
related_prs: [179]
captured: 2026-07-02
---

# Explicit persistence state drives save() semantics

**Problem:** `save()` used to compile to `INSERT ... ON CONFLICT (pk) DO
UPDATE` unconditionally, so whether it inserted or clobbered depended on what
happened to be in the table — a silent-upsert F6 finding in the 2026-07-02
adversarial review.

**Takeaway:** every instance carries an explicit transient/persisted flag, and
`save()` branches on it — transient → INSERT, persisted → `UPDATE ... WHERE
pk = ?`. Upsert only happens when named (`Model.upsert(...)` /
`save(on_conflict="update")`).

## Where it lives

- Flag: instance-dict attribute `"__ferro_persisted"` (`_FERRO_PERSISTED_ATTR`,
  `src/ferro/models.py`), with helpers `_is_persisted` / `_set_persisted`.
  Stored in `__dict__`, not a Pydantic field, so it never serializes.
- Set **True** by Rust hydration (`hydrate_model_instance` — anything fetched
  from the database) and by a successful `save()`; set **False** by
  `delete()`. Absent means transient.
- `save()` dispatch: `Model.save()` in `src/ferro/models.py` — the
  `on_conflict="update"` branch calls `save_record(mode="upsert")`, persisted
  goes through `update_record` (0 rows → `ModelDoesNotExist`), transient goes
  through `save_record(mode="insert")`.
- SQL: `build_save_sql` in `src/operations.rs` — `SaveMode::Insert` renders a
  plain INSERT; `SaveMode::Upsert` adds `ON CONFLICT (pk) DO UPDATE` over all
  non-PK columns when a conflict target exists.

## State machine

```
            Model(...)                    ── transient
transient ──save()──────────────▶ persisted   (INSERT; dup pk/unique raises
                                               UniqueViolationError)
persisted ──save()──────────────▶ persisted   (UPDATE by pk; 0 rows raises
                                               ModelDoesNotExist)
persisted ──delete()────────────▶ transient   (a later save() INSERTs anew)
persisted ──refresh()───────────▶ persisted
fetch (get/where/hydration)─────▶ persisted
```

## Edge cases worth knowing

- `model_copy()` copies `__dict__`, so it copies persistence state: saving a
  copy of a persisted instance UPDATEs the **same row**. Cloning a row means
  constructing a fresh instance.
- The UPDATE targets the instance's *current* PK value. Mutating the PK field
  of a persisted instance before `save()` matches no row and raises
  `ModelDoesNotExist` — it does not "move" the row.
- A row inserted inside a rolled-back transaction leaves the instance marked
  persisted; the next `save()` raises `ModelDoesNotExist`. The flag tracks
  what the instance *believes*, not what the database currently holds.
- The epic (#170) sketched the 0-row-UPDATE error as `StaleObjectError`; it
  shipped as `ModelDoesNotExist` (already `FerroError` + `LookupError`, and
  the failure is literally "the row does not exist").

## When to apply

Any new mutation surface (bulk save, session flush, future unit-of-work) must
route through this flag rather than re-deriving state from the identity map or
`__ferro_connection_name` — those are routing concerns, and deriving
INSERT-vs-UPDATE from them is exactly the implicitness A4 removed. Tests
pinning the behavior: `tests/test_save_semantics.py`.
