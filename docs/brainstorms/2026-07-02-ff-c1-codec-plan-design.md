# FF-C C1 — Per-model `ColumnCodec` plan: design decisions

Companion to `docs/plans/2026-07-02-001-fable-fixes-roadmap.md` (Epic FF-C, C1).
Settles the three design decisions the roadmap left open, with the constraint
set that drove each choice.

## Problem

`src/codec.rs` re-derives every column's type from the Pydantic JSON schema
**per value, per row**, across four hot functions (`schema_bind_expr`,
`query_bind_expr`, `decode_engine_value`, `typed_rows_to_parsed_data`) plus the
SELECT projection. `query_bind_expr` additionally takes a `MODEL_REGISTRY`
read-lock per bound value. The pattern sniffer `pattern_looks_decimal`
misclassifies `str` fields with numeric patterns as decimals (**F5**):

```python
class Vehicle(Model):
    id: int | None = Field(default=None, primary_key=True)
    year_code: str = Field(pattern=r"^\d{4}$")
```

Today `year_code` hydrates as `decimal.Decimal("2024")` and binds
`CAST($1 AS numeric)` on Postgres. It must round-trip as `str`.

## Decision 1 — Where the plan lives and when it is built

**Chosen: the registry value becomes one struct holding schema + plan.**

```rust
// state.rs
pub struct RegisteredModel {
    pub schema: serde_json::Value,
    pub codec_plan: ModelCodecPlan,
}
pub static MODEL_REGISTRY: Lazy<RwLock<HashMap<String, Arc<RegisteredModel>>>>
```

The plan is compiled inside `register_model_schema` (and every other insertion
path, via one shared constructor) — once per model per schema epoch. The
schema epoch **is** the registry insert: re-registering a model (the
test-suite clear+redefine pattern) rebuilds the plan atomically, because
schema and plan live in the same value. A separate plan registry was rejected
because tests (and future code) insert into `MODEL_REGISTRY` directly; two
registries can desync — exactly the stale-plan bug class the roadmap warns
about. Bonus: callers that today `clone()` the whole schema JSON per operation
now clone an `Arc`.

Hot paths resolve the `Arc<RegisteredModel>` **once per statement**, then do
O(1) per-column `HashMap` lookups — no per-value locks.

## Decision 2 — Relationship to `CodecIrPayload`

**Chosen: the runtime plan is expressed in `CodecIrPayload`'s vocabulary and
produces the payload at runtime; the golden vector pins the runtime table.**

`CodecIrPayload` is a *type-level* rule registry keyed by
`(logical_type, db_type)` (bind) and `db_type` (fetch) — it has no per-column
entries, so the per-model plan cannot literally *be* the payload. Instead:

- Each `ColumnCodec` variant maps 1:1 to a bind rule
  (`logical_type`, `db_type`, `non_null_wire_kind`, `null_wire_kind`) and a
  fetch rule (`db_type`, `wire_kind`, `python_kind`) via methods on the enum.
  The wire-kind strings are exactly the fixture's (`i64`, `numeric_text`,
  `timestamp_text`, …) — no parallel vocabulary.
- `runtime_codec_ir_payload()` enumerates the full vocabulary into a
  `CodecIrPayload`. A test asserts every rule in the golden vector
  `tests/fixtures/ir_vectors/codec_registry_core_v1.json` appears
  **byte-identically** in the runtime payload. That closes the "CodecIR has no
  runtime consumer" gap: the golden vectors now pin the table that actually
  drives bind/decode.

## Decision 3 — The `ColumnCodec` enum (and the storage/logical split)

The roadmap's enum is adopted verbatim as the *logical* codec:

```rust
pub enum ColumnCodec {
    Int, SmallInt, BigInt, Float, Bool, Str, Bytes, Uuid,
    DateTime, Date, Time, Decimal, Json,
    Enum { values: Vec<String>, storage: EnumStorage },
}
pub enum EnumStorage { PgNative { type_name: String }, Text, Int }
```

**Key constraint discovered during design:** an explicit `db_type` may legally
*widen* storage away from the logical family — `external_id: UUID =
Field(db_type="text")` is the canonical portable-storage move
(`tests/test_db_type_integration.py::test_uuid_stored_as_text_round_trip`).
Today's codec decisions are logical-type-driven (that column binds/decodes/
projects as UUID while the DDL emits `text`). A plan derived *only* from
`resolve_column_storage` would flip its hydrated type from `uuid.UUID` to
`str` — a forbidden shape change in C1.

So each plan column records **both facets**, which is precisely the
`(logical_type, db_type)` pair `CodecBindRule` already models:

```rust
pub struct ColumnCodecEntry {
    pub codec: ColumnCodec,   // logical: drives bind value kinds, decode, projection
    pub db_type: String,      // canonical FF-B storage token: DDL-consistency witness
}
pub struct ModelCodecPlan {
    pub columns: HashMap<String, ColumnCodecEntry>,
    pub pg_text_projection: bool, // precomputed: any column needs CAST(.. AS text)
}
```

Derivation per column — both facets come from the FF-B decision table, no
fourth inference:

1. `storage = resolve_column_storage_json(col, Dialect::Postgres)` →
   `db_type = canonical_to_db_type_token(...)` (or the PgEnum type name).
   Postgres is used as the resolution dialect because it preserves the most
   type information (Boolean, Uuid, PgEnum); dialect-specific behavior
   (SQLite bool-as-int, etc.) stays a consumption-time branch exactly as
   today. The registry is global across engines of different dialects, so the
   plan itself must be dialect-agnostic.
2. `codec` = `enum_values` overlay first (mirroring `resolve_column_storage`
   precedence), else `canonical_from_parts(logical_type, format, "", PG)` —
   the same cascade **without** the explicit-`db_type` override — mapped
   `CanonicalType → ColumnCodec`. When storage and logical agree on family,
   the storage canonical refines width (`int` + `db_type="bigint"` → `BigInt`).
   When they disagree (uuid-as-text), the logical codec wins, preserving
   today's bind/decode behavior.

The U2 compatibility matrix (`_annotation_utils.db_type_is_compatible`)
guarantees family-crossing combinations are limited to the string-family
widenings handled above, so plan-driven decisions are behavior-identical to
the old sniffing on every legal model — except F5, where the pattern sniffer
was simply wrong (`pattern` no longer participates in any decision).

## What each consumer reads from the plan

| Consumer | Old inputs | New inputs |
|---|---|---|
| `schema_bind_expr` | `format`/`json_type`/`is_decimal` sniffing per value | `ColumnCodecEntry` lookup; catalog maps (`enum_udt`, `uuid_columns`, `ts_cast`) unchanged (C2 caches them) |
| `query_bind_expr` | registry lock + sniffing per value | `Option<&ModelCodecPlan>` resolved once per query plan |
| `decode_engine_value` / `typed_rows_to_parsed_data` | sniffing per cell | codec lookup per cell (O(1), no JSON) |
| `apply_postgres_text_select_columns` | re-sniffs `format`/`json_type`/`is_enum` | plan: cast iff codec ∈ {Uuid, DateTime, Date, Decimal, Json, Enum} ∪ catalog native enums (the CAST itself stays — removal is C3) |

`Time` deliberately decodes as string and takes no projection cast — exactly
today's behavior; C3 revisits. Columns absent from the plan (joined/m2m
columns) fall through to the same generic arms as today's `col_info = None`
path.

## Out of scope (per roadmap)

Catalog caching (C2), native `EngineValue` variants / CAST removal (C3),
`_fix_types` deletion (C4). Hydrated Python value shapes are unchanged except
the F5 correction.
