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
- `schema` vectors are on `ir_version` 1, 2, 3 or 4. v2 (#528, ADR-0032) adds
  the optional rename hints — `renamed_from` on a model, a column and a
  foreign key, each omitted when undeclared. v3 (#668, ADR-0056) adds the
  optional envelope key `replaces` beside `parent_checksum`, a squash's
  witness: one `{migration, snapshot_checksum, steps, replaces}` entry per
  replaced migration in chain order, nesting a replaced squash's own; it is
  omitted when nothing is replaced.
  `schema_squash_witness_v3` pins a witness that nests a squash, written as
  `Snapshot::store` writes it. v4 (#792, ADR-0058) adds the optional column
  key `generated`, a generated column's value expression: a tagged tree of
  `column {name}`, `literal {token}`, `binary {op, left, right}` (`op` one of
  `add`, `sub`, `mul`, `div`), `negate {operand}` and `func {name, args}`,
  omitted on every other column; v4 is what ferro writes.
  `schema_generated_column_v4` pins it, compiled from
  `tests.test_generated_columns.declare_transaction`. A table check's
  predicate gains the kind `compare {left, op, right}` over two such value
  expressions (#796, ADR-0059), emitted only when an operand is not a plain
  column or literal, so a plain check keeps its `cmp` bytes and no version
  changes; `schema_check_fn_v4` pins it, compiled from
  `tests.test_generate_check_functions.declare_payment`. Every shipped v1 to v3
  vector stays and keeps loading through `Snapshot::load` (ADR-0023); a v1 or
  v2 envelope carrying `replaces` is refused, and so is a v1 to v3 envelope
  whose column carries `generated`.
  `codec` vectors are on `ir_version` 1. `query`
  vectors are on `ir_version: 14` (#395 — optional `before` position bound on
  fetch payloads; omitted when unset; `after` from v13 still omitted when
  unset; every `order_by` term still carries explicit `nulls`; every payload
  carries a required canonical `set` list).
  v7 introduced the recursive
  `exists` node kind beside `leaf`/`compound`/`not` (ADR-0007):
  `{"node_kind": "exists", "hops": [...], "where": [...]}` — `hops` is the
  correlation hop path in the `joins`-section hop shape (1 hop reverse FK,
  2 hops M2M), `where` the ordinary inner condition tree, `[]` = bare test);
  there is no earlier `query` vector left.
- `expect_valid` currently supports only `true` fixtures (negative vectors can be added later).
- Fixture file names use `<domain>_<scenario>_v<version>.json`, where
  `<version>` is the vector's own `ir.ir_version`.

## Coverage requirements (Phase 0 minimum)

- `schema`: one vector with parity-sensitive artifact names (`idx_*`, `uq_*`, `ck_*`, FK metadata).
- `query`: one vector with compound predicates and typed value nodes.
- `codec`: one vector with typed null and hydration ABI slot requirements.

## How to extend

1. Add a new JSON fixture in this directory.
2. Keep `vector_name` unique.
3. Update `tests/test_ir_vectors_contract.py` if new required fields are introduced.
4. Ensure CI remains deterministic (no generated timestamps/random IDs in fixtures).
