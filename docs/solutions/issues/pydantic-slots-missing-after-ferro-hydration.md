---
title: AttributeError on __pydantic_extra__ after loading a row (zero-copy hydration)
type: issue
tags: [gotcha, pydantic, bridge, rust, hydration, ffi]
related_files:
  - src/operations.rs
  - tests/test_hydration.py
related_issues: []
related_prs: []
captured: 2026-05-14
---

## Problem

Anything that touches Pydantic’s slot-backed internals on a model instance fails
with `AttributeError: ... has no attribute '__pydantic_extra__'` (or
`__pydantic_private__`) **after** the instance was hydrated by Ferro’s Rust
core (for example `await Model.get(...)`, filtered query results), while the
same code works if the instance was built with `Model(...)`.

Typical stack traces include `dict(instance)` / `BaseModel.__iter__`, or
third-party code that walks return values (for example Prefect’s
`visit_collection`).

## Takeaway

Ferro intentionally bypasses `Model.__init__` for performance (see AGENTS.md
I-2). Pydantic v2 stores several attributes in `__slots__`; **unset** slots do
not behave like missing dict keys — reads raise `AttributeError`. The Rust
hydration paths must assign the same defaults `BaseModel.__init__` would,
including `__pydantic_extra__` (empty dict when `model_config["extra"] ==
"allow"`, otherwise `None`) and `__pydantic_private__` (`None`).

## Explanation

The fix lives next to the existing `__pydantic_fields_set__` assignment in
`src/operations.rs` (`set_pydantic_hydration_slots`).

## How to recognize

- Failure only on **fetched** / **query-hydrated** instances, not on freshly
  constructed ones.
- Error mentions `__pydantic_extra__` or `__pydantic_private__` inside Pydantic
  or serialization helpers.
- You recently added code that calls `dict(model)`, iterates the model, or runs
  a framework that deep-visits objects.
