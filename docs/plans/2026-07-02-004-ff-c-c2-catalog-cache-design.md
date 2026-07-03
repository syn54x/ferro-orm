# FF-C C2 — Schema-epoch catalog cache (design)

Fixes F4: up to 3 live `pg_catalog`/`information_schema` round-trips per
operation. Exit gate: **zero catalog queries on steady-state CRUD**, verified
by a statement-count test against live Postgres.

## Survey of current main (post C1/C3/C4)

19 live call sites, all in `src/operations.rs`, all funneling through the
single choke point `postgres_catalog_rows` (operations.rs:430):

| Function | Call sites | Consumers |
| --- | --- | --- |
| `postgres_enum_udt_by_column` | 8 — `save_record`, `update_record`, `save_bulk_records`, `fetch_filtered`, `count_filtered`, `delete_filtered`, `update_filtered` (values + predicates) | `schema_bind_expr` (INSERT/UPDATE values), `query_bind_expr` via `QueryDef.postgres_enum_udt` (filter predicates) |
| `postgres_uuid_column_names` | 7 — `save_record`, `update_record`, `save_bulk_records`, `update_filtered`, `add_m2m_links`, `remove_m2m_links`, `clear_m2m_links` | `schema_bind_expr` (values), `backend_column_value_expr` (m2m binds) |
| `postgres_temporal_cast_by_column` | 4 — `save_record`, `update_record`, `save_bulk_records`, `update_filtered` | `schema_bind_expr` (values) |

`postgres_enum_type_name_for_column` (schema_bind.rs) has **zero callers** —
dead code from the pre-C1 era; deleted as part of this change.

The C1 plan (`ModelCodecPlan`) carries per column: the logical `ColumnCodec`
(incl. `Uuid`, `DateTime`/`Date`/`Time`, `Enum { storage:
EnumStorage::PgNative { type_name } | Text | Int }`) **and** the canonical
FF-B storage token `db_type` from `resolve_column_storage` — the exact string
the DDL emitters create (`"timestamp"`, `"timestamptz"`, `"date"`, `"uuid"`,
native enum type names). codec_plan.rs documents the token as having "no
runtime consumer yet in C1 … C2/C3 consume it". C2 is that consumer.

## Step 1 — shrink the need (plan-derived, catalog calls deleted)

**Temporal (`postgres_temporal_cast_by_column`) — deleted entirely.**
The cast target is the plan's `db_type` token when it is
`timestamp`/`timestamptz`/`date`. Note the plan-*codec* fallback
(`temporal_cast_for_codec`: `DateTime → "timestamptz"`) is NOT a safe
replacement by itself — DDL lowers `datetime` to naive `"timestamp"`
(ferro-ddl-lowering lib.rs:202), and the catalog (DB truth) wins over the
codec fallback today. The storage token matches DDL truth exactly, so
token-first (then codec fallback, which still covers `Time`) preserves
today's casts. The residual mismatch class (model says `str`, DB column is
timestamp) loses its explicit cast, but the values path is Postgres
*assignment context* where text→temporal I/O coercion is implicit — inserts
keep working; filter predicates never used `ts_cast`.

**UUID (`postgres_uuid_column_names`) — deleted for model tables.**
Plan-derived set: `codec == Uuid` OR `db_type == "uuid"` (the cross-family
`str`-stored-as-uuid case). `schema_bind_expr` already ORs the codec in
(`uuid_columns.contains(col) || codec == Uuid`), and `query_bind_expr`
(predicates) is plan-only today. The model-says-`str`/DB-says-uuid mismatch
falls back to a text bind in assignment context — implicit I/O coercion, same
stored value.

The function **survives for m2m join tables only** — join tables are not
registered models and have no codec plan, so the catalog is the only
authority. That lookup is cached (step 2).

## Step 2 — cache the residual (DB truth the plan cannot know)

**Enum (`postgres_enum_udt_by_column`) — kept as catalog truth, cached.**
Enums are the one case where DB truth must win in *both* directions:

- Alembic-created native enum, model says `str` (FF-B correctness case,
  `test_native_postgres_enum_plain_str_column`): bind must cast
  `::<udt>` or prepared INSERT/predicate fails.
- Auto-migrate TEXT column, model declares an enum (schema_bind.rs:8): bind
  must stay plain text; casting to a nonexistent/native type would break.

So the plan cannot replace this lookup; it is cached per table per epoch.

**Cache shape.** `CatalogCache` on `EngineHandle` (a new `Arc`-shared field,
like `pool`):

```rust
struct CatalogCache {
    enum_udt: RwLock<HashMap<String, Arc<HashMap<String, String>>>>, // table → col → udt
    uuid_columns: RwLock<HashMap<String, Arc<HashSet<String>>>>,     // join table → uuid cols
}
```

Two independent maps so model tables never run the join-table uuid query and
join tables never run the enum query. Lookup: read-lock check → on miss run
the catalog query (outside the lock, via `tx_conn`/pool exactly as today) →
write-lock insert. A racing double-fetch is idempotent and harmless. `std`
`RwLock` with poison-recovery, matching the `pool` slot's conventions.

**Invalidation.** `refresh_pool()` clears both maps — the existing epoch
primitive, no new concept. Auto-migrate already calls `refresh_pool()` after
DDL (migrate.rs:875), so a mid-session schema change invalidates correctly.
Both refresh branches clear (pool rebuild and ephemeral-SQLite cache-clear).

**Non-Postgres:** the three functions short-circuit to empty before touching
catalog or cache on SQLite — unchanged; the cache adds no cost there.

## Statement-count instrument

`catalog_queries: Arc<AtomicU64>` on `EngineHandle`, incremented in
`postgres_catalog_rows` immediately before executing SQL — the single choke
point, so the count is exact by construction. Exposed via test-only FFI
`_catalog_query_count_for_test(using=None) -> int` mirroring the
`_shadow_compare_*_for_test` pattern.

## Tests (TDD red harness first)

`tests/test_catalog_cache.py`:

1. **Exit gate:** steady-state CRUD (repeated save/get/where/update/count/
   delete on a model with enum + uuid + datetime fields, plus m2m ops) after
   a warm-up first op per table → counter delta **zero** (`postgres_only`).
2. **Warm-up bound:** first ops issue at most one catalog query per table
   (enum map) + one per join table (uuid set).
3. **Invalidation:** CRUD → `migrate()` adds a column (calls
   `refresh_pool`) → next CRUD sees the new column (counter may advance
   once — correctness assertion, not count).
4. **FF-B residual stays green:** existing
   `test_native_postgres_enum_plain_str_column` + a steady-state variant.
5. **SQLite no-op:** counter stays zero for the whole suite on SQLite.

## Scope boundaries

- No changes to codec/decode (C1/C3) semantics; `schema_bind_expr` /
  `query_bind_expr` signatures keep taking the maps — only their *source*
  changes (plan-derived / cached instead of per-op queries).
- No second invalidation mechanism.
- No observable behavior change — conventional commits scope `ff-c`,
  not breaking.
