# FF-G G2 — `operations.rs` dedup: `ModelMeta` + `Executor` (design)

**Status:** approved · **Epic:** FF-G (issue #226) · **Sub-issue:** #228 ·
**Type:** `refactor(ff-g)` — behavior-preserving, no user-observable surface
change, no `!`.

## What this is

`src/operations.rs` (3,912 lines) carries two systemic duplications:

1. **Eight copy-pasted PK-discovery scans** — each op re-walks
   `schema.properties` looking for `"primary_key": true` (and sometimes
   `"autoincrement"`). Sites: `fetch_all` (~889), `fetch_one` (~1026),
   `save_record` (~1334), `update_record` (~1452), `save_bulk_records`
   (~1552), `fetch_filtered` (~1657), `count_filtered` (~1845),
   `delete_record` (~1988).
2. **Eleven doubled tx/no-tx `match tx_conn` arms** — each execution site
   mirrors a `Some(conn_arc) => { lock; call EngineConnection method }` arm
   against a `None => { call EngineHandle method }` arm with identical bodies.
   Sites: `execute_statement_with_optional_tx` (368), `postgres_catalog_rows`
   (482), `fetch_all` (907), `fetch_one` (1063), `save_record` (1366),
   `update_record` existence-check (1489), `fetch_filtered` (1717),
   `count_filtered` (1883), `raw_execute` (2641), `raw_fetch_all` (2681),
   `raw_fetch_one` (2736).

The exit gate is **identical behavior, less duplication** — every touched op
must behave bit-identically, proven by the full sqlite+postgres matrix plus a
shadow-strict sweep, not just a green suite.

## Decision 1 — `ModelMeta`, compiled at registration

A registration-time struct stored on `RegisteredModel`, following the FF-C
`codec_plan` precedent (one value per registration; a re-registration swaps
the whole `Arc`, so a stale meta can never outlive its schema):

```rust
/// Primary-key facts scanned once from the enriched schema at registration
/// (FF-G G2). Replaces the per-operation `properties` walks.
#[derive(Debug)]
pub struct ModelMeta {
    /// First property flagged `"primary_key": true`, in schema-map iteration
    /// order (serde_json default map — lexicographic by key).
    pub pk_col: Option<String>,
    /// The PK's `"autoincrement"` flag. `true` when the key is absent — and
    /// `true` when there is no PK at all, preserving the historical per-site
    /// default that `build_save_sql` receives today.
    pub pk_autoincrement: bool,
}
```

- **Where resolved:** inside `RegisteredModel::new`, next to
  `ModelCodecPlan::compile`. Zero per-operation scans, zero new registry
  locks — every op already holds the `Arc<RegisteredModel>` from its existing
  `crate::state::registered_model(&name)?` call (FF-C/FF-E threading), and
  that call's loud FF-E `"Model '…' not found"` error path is untouched.
- **Table name:** already a field on `RegisteredModel` (`table_name`, FF-E
  E2); `ModelMeta` does not duplicate it. "One ModelMeta (pk name,
  autoincrement, table)" is satisfied by the registration itself:
  `schema.meta.pk_col` + `schema.meta.pk_autoincrement` + `schema.table_name`.
- **Call-site shape:** `schema.meta.pk_col.clone()` /
  `.as_deref()`; the three pk-required sites keep their exact
  `.ok_or_else(...)` error strings (`"No primary key"` ×2 wordings,
  `"No primary key for M2M join"`). No helper method for the error — three
  sites with two distinct messages don't justify one (YAGNI).

**Equivalence argument.** The scan moves, it does not change: same
`properties` object, same `as_bool().unwrap_or(false)` handling of non-bool
`primary_key`, same `unwrap_or(true)` for `autoincrement`, same
first-match-wins over the same iteration order (the schema is immutable after
registration, and serde_json's default `Map` iterates deterministically).
Permanent unit tests lock the contract: no PK → `None`/`true`; PK without
`autoincrement` → `true`; `autoincrement: false` → `false`; non-bool
`primary_key` → skipped; two flagged columns → lexicographic first wins.

**Rejected alternative:** a free `model_meta(&RegisteredModel) -> ModelMeta`
resolved per op. Works, but re-scans per value for no benefit and misses the
codec-plan precedent; registration-time is the strictly stronger form of
"resolved once."

## Decision 2 — `Executor`: an enum, not a trait

A two-variant enum in `operations.rs` (its only consumer — defining it in
`backend.rs` would need `TransactionConnection` from `state.rs`, cycling the
existing `state → backend` dependency):

```rust
/// One execution route for an operation: the open transaction's connection,
/// or the routed engine's pool (FF-G G2). Each method holds the tx mutex for
/// exactly one engine call — the same guard scope as the match arms it
/// replaces.
enum Executor {
    Tx(TransactionConnection),   // Arc<Mutex<EngineConnection>>, cheap clone
    Pool(Arc<EngineHandle>),
}

impl Executor {
    fn new(tx_conn: Option<TransactionConnection>, engine: &Arc<EngineHandle>) -> Self;
    async fn fetch_all(&self, sql: &str, binds: &[EngineBindValue])
        -> Result<Vec<EngineRow>, sqlx::Error>;
    async fn execute(&self, sql: &str, binds: &[EngineBindValue])
        -> Result<u64, sqlx::Error>;
    async fn execute_result(&self, sql: &str, binds: &[EngineBindValue])
        -> Result<EngineExecuteResult, sqlx::Error>;
}
```

Three methods — the complete verb set the eleven sites use — and nothing
speculative. A trait buys nothing here (no third implementor, async-trait
machinery, dyn overhead); the enum's one `match self` per method replaces
eleven per-site matches.

**Exact-semantics requirements, and how the shape meets them:**

- **Connection identity/lifetime:** `Tx` holds the same
  `Arc<Mutex<EngineConnection>>` the site holds today; `lock().await` scope is
  one engine call, identical to every current arm (no site holds the guard
  across two calls). Queries can never migrate off the transaction.
- **Error propagation:** methods return raw `sqlx::Error`; every call site
  keeps its own `crate::errors::map_db_error("<exact label>", e)`. No message
  changes.
- **Lazy engine resolution on the raw path:** `raw_execute`/`raw_fetch_all`/
  `raw_fetch_one` currently call `engine_for_connection(...)` only inside the
  `None` arm — with a live tx, a missing connection never errors. Those sites
  build the executor inside the async block as
  `match tx_conn { Some(tx) => Executor::Tx(tx), None => Executor::Pool(engine_for_connection(..)?) }`,
  preserving that ordering exactly. CRUD sites resolve the engine eagerly via
  `route_engine` today and keep doing so (`Executor::new(tx_conn, &engine)`).
- **Bind conversion:** methods take `&[EngineBindValue]`; sites keep their
  existing `engine_bind_values_from_sea` calls (conversion is pure — arm-inside
  vs. before-the-match is observationally identical).
  `execute_statement_with_optional_tx` (the existing one-off mini-executor for
  `SeaValue` slices) is reimplemented as a thin wrapper over
  `Executor::execute`; its eight callers don't change.
- **Catalog path:** `postgres_catalog_rows` takes the `Executor` (plus the
  engine it already uses for `record_catalog_query()`); its match collapses
  like the rest. `postgres_table_catalog` threads it through.

## Decision 3 — proof of behavior-preservation

**Baseline (captured on `main` before any change):**

- `wc -l src/operations.rs` (3,912) and duplication greps:
  `grep -c '.get("primary_key")'` = 8, `grep -c 'match tx_conn'` = 11.
- `grep -rc "not found" src/` per file (loud-error census).
- Full matrix green: `cargo test --no-default-features --features testing`,
  `cargo test -p ferro-schema-ir -p ferro-ddl-lowering -p ferro-migrate`,
  `FERRO_POSTGRES_URL=… just test`.
- Shadow-strict green: `FERRO_SHADOW_RUNTIME=1 FERRO_SHADOW_RUNTIME_STRICT=1
  FERRO_POSTGRES_URL=… just test` (both flags — STRICT alone is a no-op).
- `cargo llvm-lines` total for the extension crate (relative metric).

**Per-cluster (after every migration step):** `uv run maturin develop`, Rust
unit tests, full sqlite+postgres matrix. Any diff from the baseline is a
regression introduced by that step.

**Structural equivalence:**

- The refactor never touches SQL construction — sea_query builder code moves
  at most verbatim. A `git diff main -- src/operations.rs` review confirms no
  builder-line edits; the existing `build_save_sql`/`build_update_by_pk_sql`
  unit tests pass unchanged, so rendered SQL and binds are unchanged by
  construction.
- Permanent `ModelMeta` unit tests (listed under Decision 1) pin the scan
  semantics.

**Final sweep:** shadow-strict (both flags) over the full matrix on both
backends; `"not found"` grep census identical to baseline; report the LOC,
grep-count, and `cargo llvm-lines` deltas in the PR.

## Execution order (detail in the companion plan)

1. `ModelMeta` on `RegisteredModel` + unit tests; migrate the 8 PK-scan sites
   in clusters (fetch ops → save/update ops → filtered/delete ops), matrix
   green after each.
2. `Executor` + unit-level introduction (`execute_statement_with_optional_tx`
   wrapper, catalog path); migrate the remaining match sites in clusters
   (CRUD fetch → save/update/count → raw ops), matrix green after each.
3. Final shadow-strict sweep, duplication metrics, loud-error census.

## Out of scope

Splitting `operations.rs` into submodules, touching SQL builders, changing
`route_engine`'s signature, migrating `match tx_conn` sites outside
`operations.rs`, and any new capability. No version bumps
(`pyproject.toml`/`Cargo.toml` stay 0.13.0).
