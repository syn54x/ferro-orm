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
native enum type names). The obvious question C2 must answer: can that plan
replace the catalog lookups? **No — see the probe below.** The plan describes
model-declared storage; bind-cast correctness follows DB truth.

## Why the plan cannot replace the catalog (probe evidence)

The obvious C2 shape — derive uuid/temporal/declared-enum info from the C1
plan and delete those catalog calls — is **unsound**. Probe against live
Postgres 16:

```sql
CREATE TEMP TABLE _c2probe(ts timestamp, u uuid);
PREPARE ins(text, text) AS INSERT INTO _c2probe VALUES ($1, $2);
-- ERROR:  column "ts" is of type timestamp without time zone
--         but expression is of type text
```

There is no assignment-context I/O coercion for typed (TEXT-OID) parameters.
Every column whose SQL type differs from the bound parameter type needs an
explicit cast, and the *DB truth* decides whether one is needed:

- **Alembic-created native enum / uuid / timestamp column, model says
  `str`:** the bind must cast (`::<udt>` / `::uuid` / `::timestamp`) or the
  prepared INSERT fails with the error above. This is FF-B's
  `test_native_postgres_enum_plain_str_column` generalized to all three type
  families — deleting any of the three lookups silently breaks inserts for
  its mismatch class.
- **Auto-migrate TEXT column, model declares an enum** (schema_bind.rs:8):
  the bind must stay plain text; a plan-driven `::<type_name>` cast would
  reference a type that doesn't exist in the DB.

Mismatches are two-directional and per-type-family, so all three lookups
must remain catalog-authoritative. The C1 plan's `db_type` token describes
what ferro's DDL *would create*, not what the DB *contains* — for bind
casts, only the latter is correct. (Where plan and DB agree — the 99% case —
the cached catalog map and the plan derive identical casts, so caching DB
truth is behavior-preserving everywhere; plan derivation is not.)

## Shrink + cache: one combined catalog probe per table per epoch

The need still shrinks — not by dropping catalog authority, but by
consolidating: the three separate queries (enum, uuid, temporal ×
save/update/bulk/filtered = up to 3 per op) collapse into **one combined
`pg_catalog` query per table**, executed at most once per table per epoch:

```sql
SELECT a.attname::text AS column_name,
       t.typname::text AS udt_name,
       t.typtype::text AS typtype
FROM pg_attribute a
JOIN pg_class c ON a.attrelid = c.oid
JOIN pg_namespace n ON c.relnamespace = n.oid
JOIN pg_type t ON a.atttypid = t.oid
WHERE n.nspname = current_schema()
  AND c.relname = $1
  AND a.attnum > 0
  AND NOT a.attisdropped
```

From one result set, `TableCatalog` derives all three maps:

```rust
pub struct TableCatalog {
    pub enum_udt: HashMap<String, String>,   // typtype = 'e'
    pub uuid_columns: HashSet<String>,       // typname = 'uuid'
    pub ts_cast: HashMap<String, String>,    // timestamp/timestamptz/date
}
```

(`typname` for the temporal families is exactly the cast token:
`timestamp`, `timestamptz`, `date` — same strings the old
`information_schema` query produced.)

The three per-kind functions and their 19 call sites are replaced by a single
cached accessor `postgres_table_catalog(table, engine, tx_conn, backend) ->
Arc<TableCatalog>`; m2m sites read `.uuid_columns` from the same entry.
Model tables and join tables share one code path.

**Cache shape.** On `EngineHandle` (a new `Arc`-shared field, like `pool`):

```rust
catalog_cache: Arc<RwLock<HashMap<String, Arc<TableCatalog>>>>  // table → snapshot
```

Lookup: read-lock check → on miss run the combined query (outside the lock,
via `tx_conn`/pool exactly as today) → write-lock insert. A racing
double-fetch is idempotent and harmless. `std` `RwLock` with
poison-recovery, matching the `pool` slot's conventions.

Population inside a transaction uses the tx connection (as today) and does
write the shared cache: DDL-in-tx followed by rollback *and* continued use of
the same table is not a supported pattern (auto-migrate never does it; its
DDL is followed by `refresh_pool()`).

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
2. **Warm-up bound:** first ops issue at most one combined catalog query per
   table (including join tables).
3. **Invalidation:** CRUD → `migrate()` adds a column (calls
   `refresh_pool`) → next CRUD sees the new column (counter may advance
   once — correctness assertion, not count).
4. **FF-B residual stays green:** existing
   `test_native_postgres_enum_plain_str_column` + a steady-state variant.
5. **SQLite no-op:** counter stays zero for the whole suite on SQLite.

## Scope boundaries

- No changes to codec/decode (C1/C3) semantics; `schema_bind_expr` /
  `query_bind_expr` signatures keep taking the maps — only their *source*
  changes (one cached snapshot instead of three per-op queries).
- No second invalidation mechanism.
- No observable behavior change — conventional commits scope `ff-c`,
  not breaking.
