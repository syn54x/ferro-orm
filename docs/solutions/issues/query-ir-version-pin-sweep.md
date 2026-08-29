---
title: QueryIR version bumps leave stale assertion pins
type: issue
tags: [gotcha, query, ir, pytest, rust]
related_files:
  - src/ferro/query/wire.py
  - src/operations.rs
  - tests/test_ir_vectors_contract.py
  - tests/test_order_by_nulls_wire.py
  - tests/test_query_builder.py
related_issues: [376, 377, 378, 380]
related_prs: []
captured: 2026-08-29
---

## Problem

A QueryIR version bump that rewrites the golden vectors still leaves
`assert envelope["ir_version"] == N` pins in tests that never open a fixture
file. #380 missed `tests/test_order_by_nulls_wire.py` and
`tests/test_query_builder.py`.

## Takeaway

Grep the whole tree for the old integer — not just `_IR_VERSION` and the
fixture directory — before calling a bump done.

## How to recognize

CI fails on a wire-shape test that never mentions the new artifact, or a
rust version-gate helper still `contains('N')` for the previous supported
version.

## When to apply

Every unconditional QueryIR bump (the one-wheel rule: v7 exists, v8 SET,
v9 column-ref). Search for leftover `== N`, `query: N`, `vN_envelope`,
`accepts_version_N`, and `contains('N')` in the version-gate helper.
