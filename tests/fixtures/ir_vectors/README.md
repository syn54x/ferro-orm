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
- `ir.ir_version` must equal `1` for Phase 0 vectors.
- `expect_valid` currently supports only `true` fixtures (negative vectors can be added later).
- Fixture file names use `<domain>_<scenario>_v1.json`.

## Coverage requirements (Phase 0 minimum)

- `schema`: one vector with parity-sensitive artifact names (`idx_*`, `uq_*`, `ck_*`, FK metadata).
- `query`: one vector with compound predicates and typed value nodes.
- `codec`: one vector with typed null and hydration ABI slot requirements.

## How to extend

1. Add a new JSON fixture in this directory.
2. Keep `vector_name` unique.
3. Update `tests/test_ir_vectors_contract.py` if new required fields are introduced.
4. Ensure CI remains deterministic (no generated timestamps/random IDs in fixtures).
