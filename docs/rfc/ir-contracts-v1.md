---
title: "IR contracts v1 (SchemaIR, QueryIR, CodecIR)"
type: rfc
status: draft
date: 2026-06-19
phase: IR-P0
roadmap: docs/plans/2026-06-19-001-ir-first-roadmap.md
---

# IR contracts v1

## Purpose

Define the v1 canonical intermediate representation contracts for schema, query, and bind/fetch codec behavior before Phase 1 implementation.

This RFC is normative for wire shape, versioning, and validation behavior.

## Non-goals

- Implementing new runtime/compiler behavior (Phase 1+).
- Removing compatibility layers in existing JSON pipelines (later phases).

## Shared contract rules

### Envelope

All IR artifacts use a top-level envelope:

```json
{
  "ir_kind": "schema|query|codec",
  "ir_version": 1,
  "payload": {}
}
```

Rules:

- `ir_kind` and `ir_version` are required.
- Unknown `ir_kind` is a hard error.
- Unsupported `ir_version` is a hard error.
- `payload` must be an object; missing or non-object payload is a hard error.

### Compatibility policy

- Minor additive evolution inside `ir_version: 1` may add optional payload fields.
- Existing required fields in v1 cannot be removed or retyped.
- New required fields require a major version bump.
- Readers must fail loudly on malformed required fields.
- Writers must emit deterministic field ordering where serialization APIs allow it.

## SchemaIR v1

`SchemaIR` represents canonical model schema semantics consumed by runtime DDL, migration planning, and Alembic adapters.

### SchemaIR payload shape

```json
{
  "dialect_agnostic": true,
  "models": [
    {
      "model_name": "Invoice",
      "table_name": "invoice",
      "columns": [
        {
          "name": "id",
          "logical_type": "integer",
          "db_type": "bigint",
          "nullable": false,
          "primary_key": true,
          "autoincrement": true,
          "unique": false,
          "index": false,
          "default": null,
          "format": null
        }
      ],
      "foreign_keys": [
        {
          "column": "customer_id",
          "to_table": "customer",
          "to_column": "id",
          "on_delete": "CASCADE",
          "name": null
        }
      ],
      "indexes": [
        {
          "name": "idx_invoice_created_at",
          "columns": ["created_at"],
          "unique": false
        }
      ],
      "uniques": [
        {
          "name": "uq_invoice_number",
          "columns": ["number"]
        }
      ],
      "checks": [
        {
          "name": "ck_invoice_total",
          "expression": "total >= 0"
        }
      ]
    }
  ]
}
```

### SchemaIR requirements

- `table_name` must be canonical lower-case model table name.
- `db_type` must use canonical Ferro tokens (`text`, `varchar(N)`, `smallint`, `int`, `bigint`, `uuid`, `timestamp`, `timestamptz`, `date`, `time`).
- `nullable` is explicit and never inferred by the reader.
- Constraint/index names are required and must follow cross-emitter parity naming rules.
- Foreign key shadow columns (for `ForeignKey`) are represented as normal columns plus FK metadata.
- Any schema artifact emitted by one emitter must be representable without lossy translation.

## QueryIR v1

`QueryIR` represents typed query intent currently serialized through ad-hoc query JSON.

### QueryIR payload shape

```json
{
  "model_name": "User",
  "where": [
    {
      "node_kind": "compound",
      "operator": "AND",
      "left": {
        "node_kind": "leaf",
        "column": "active",
        "operator": "==",
        "value": {"kind": "bool", "value": true}
      },
      "right": {
        "node_kind": "leaf",
        "column": "email",
        "operator": "LIKE",
        "value": {"kind": "string", "value": "%@example.com"}
      }
    }
  ],
  "order_by": [
    {"column": "id", "direction": "asc"}
  ],
  "limit": 100,
  "offset": 0,
  "m2m": null
}
```

### QueryIR requirements

- Node variants are explicit (`leaf` vs `compound`), never inferred from nullable fields.
- Operator domain is restricted to: `==`, `!=`, `<`, `<=`, `>`, `>=`, `IN`, `LIKE`, `AND`, `OR`.
- `where` is a list of root predicate trees combined by implicit AND semantics unless nested compound nodes define otherwise.
- `order_by.direction` is normalized to `asc|desc`.
- Value literals are typed nodes (`kind`, `value`) so codec selection does not depend on lossy JSON inference.
- Null comparisons for equality/inequality map to `IS NULL` / `IS NOT NULL` semantics in execution lowering.

## CodecIR v1

`CodecIR` centralizes type semantics for both bind and fetch paths.

### CodecIR payload shape

```json
{
  "bind_rules": [
    {
      "logical_type": "uuid",
      "db_type": "uuid",
      "non_null_wire_kind": "uuid",
      "null_wire_kind": "uuid_null"
    },
    {
      "logical_type": "integer",
      "db_type": "bigint",
      "non_null_wire_kind": "i64",
      "null_wire_kind": "i64_null"
    }
  ],
  "fetch_rules": [
    {
      "db_type": "uuid",
      "wire_kind": "uuid",
      "python_kind": "uuid.UUID"
    }
  ],
  "hydration_abi": {
    "constructor_mode": "direct_dict",
    "required_slots": [
      "__pydantic_fields_set__",
      "__pydantic_extra__",
      "__pydantic_private__"
    ]
  }
}
```

### CodecIR requirements

- Typed null kinds are first-class and must not degrade to untyped text null in schema-driven paths.
- Bind and fetch semantics are defined per logical/db type pair.
- Hydration ABI explicitly requires slot initialization for observational equivalence with Pydantic initialization semantics.
- Runtime lowering must fail loudly when a required codec rule is absent.

## Validation behavior

For all IR kinds in v1:

- Parse/shape validation errors are fatal and actionable.
- Unknown required enum values are fatal.
- Unknown optional fields may be ignored by readers in the same major version.
- CI conformance vectors are the executable source of truth for schema/query/codec payload acceptance.

## Phase 1 handoff

Phase 1 implementation must:

1. Introduce strongly typed Rust/Python representations matching this RFC.
2. Add compile/serialize tests that round-trip all golden vectors without shape drift.
3. Keep existing behavior unchanged unless explicitly called out by roadmap phase gates.
