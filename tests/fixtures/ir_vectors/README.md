# IR golden vectors

Phase 0 conformance vectors for IR contracts.

## Purpose

- Pin the canonical wire shape for `SchemaIR`, `QueryIR`, and `CodecIR`.
- Provide deterministic fixtures that CI can validate before Phase 1 runtime cutover work.

## File format

Each vector is one JSON file with this envelope:

```json
{
  "vector_name": "schema_invoice_baseline_v1",
  "domain": "schema|query|codec",
  "expect_valid": true,
  "ir": {
    "ir_kind": "schema|query|codec",
    "ir_version": 1,
    "payload": {}
  }
}
```

Rules:

- `domain` and `ir.ir_kind` must match.
- `ir.ir_version` must equal `1` for `schema` and `codec` vectors. `query`
  vectors are on `ir_version: 13` (#393 — optional `after` position bound on
  fetch payloads; omitted when unset; every `order_by` term still carries
  explicit `nulls`; every payload carries a required canonical `set` list).
  v7 introduced the recursive
  `exists` node kind beside `leaf`/`compound`/`not` (ADR-0007):
  `{"node_kind": "exists", "hops": [...], "where": [...]}` — `hops` is the
  correlation hop path in the `joins`-section hop shape (1 hop reverse FK,
  2 hops M2M), `where` the ordinary inner condition tree, `[]` = bare test);
  there is no earlier `query` vector left.
- `expect_valid` currently supports only `true` fixtures (negative vectors can be added later).
- Fixture file names use `<domain>_<scenario>_v<version>.json` (matching that
  domain's current `ir_version`).

## Coverage requirements (Phase 0 minimum)

- `schema`: one vector with parity-sensitive artifact names (`idx_*`, `uq_*`, `ck_*`, FK metadata).
- `query`: one vector with compound predicates and typed value nodes.
- `codec`: one vector with typed null and hydration ABI slot requirements.

## How to extend

1. Add a new JSON fixture in this directory.
2. Keep `vector_name` unique.
3. Update `tests/test_ir_vectors_contract.py` if new required fields are introduced.
4. Ensure CI remains deterministic (no generated timestamps/random IDs in fixtures).
