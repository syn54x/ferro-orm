# FF-G G2 — `operations.rs` dedup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the two systemic duplications in `src/operations.rs` — eight copy-pasted PK-discovery scans and eleven doubled tx/no-tx `match tx_conn` arms — with **bit-identical behavior** (this is `refactor`, no `!`; the exit gate is "identical behavior, less duplication").

**Architecture:** A registration-time `ModelMeta` (pk column + autoincrement flag) on `RegisteredModel` replaces the per-op schema scans; a borrowed, `Copy` `Executor<'a>` enum (`Tx(&TransactionConnection) | Pool(&EngineHandle)`) with `fetch_all`/`execute`/`execute_result` replaces the mirrored match arms. Design: `docs/plans/2026-07-06-013-ff-g2-operations-dedup-design.md`.

**Tech Stack:** Rust (pyo3, sqlx, sea_query), maturin, pytest matrix (sqlite+postgres).

## Global Constraints

- Branch `ff-g/operations-dedup`. Conventional commits, scope `ff-g`, types `refactor`/`docs`/`test`, **no `!`**. **No AI attribution in any commit or PR** (AGENTS.md I-6) — no `Co-Authored-By`, no "Generated with Claude".
- **Do not edit version files** — `pyproject.toml`/`Cargo.toml` stay `0.13.0`.
- **Never run `cargo fmt` / `ruff format` / any bulk formatter** — `main` itself is fmt-dirty; hand-format only lines you wrote, matching surrounding style.
- After every Rust change: `uv run maturin develop` before running pytest.
- No new `unwrap()`/`expect()` on Python-facing data; `PyResult` throughout (I-3). (`unwrap` in `#[cfg(test)]` code is fine.)
- Postgres for the matrix: `FERRO_POSTGRES_URL=postgres://postgres:password@localhost:5432/postgres` (docker `local-pg` is up). The matrix is green on `main` — **any failure is a regression you introduced**.
- **Behavior changes are bugs.** If a test or shadow-strict run flags a diff, find the semantic change you made; never adjust the test or suppress the comparator.
- Line numbers below were measured at branch point `b12b20f` (= `main` + design doc) and drift as tasks land — anchor by the quoted code, not the number.

---

### Task 1: Capture the baseline (no commit)

**Files:** none modified. Output recorded to `/tmp/g2-baseline.txt`.

**Interfaces:**
- Produces: `/tmp/g2-baseline.txt` — the numbers Task 10 diffs against.

- [ ] **Step 1: Record static metrics**

```bash
cd /Users/taylor/github/syn54x/ferro-orm
{
  echo "commit: $(git rev-parse HEAD)"
  echo "operations.rs LOC: $(wc -l < src/operations.rs)"
  echo "pk scans: $(grep -c '\.get("primary_key")' src/operations.rs)"
  echo "tx matches: $(grep -c 'match tx_conn' src/operations.rs)"
  echo "-- 'not found' census --"
  grep -rc "not found" src/ | grep -v ':0'
} | tee /tmp/g2-baseline.txt
```

Expected: LOC 3912, pk scans 8, tx matches 11; census includes `src/operations.rs:8` and `src/state.rs:1`.

- [ ] **Step 2: Record llvm-lines total (skip gracefully if tool absent)**

```bash
cargo llvm-lines --no-default-features --features testing 2>/dev/null | head -2 | tee -a /tmp/g2-baseline.txt || echo "llvm-lines unavailable" | tee -a /tmp/g2-baseline.txt
```

If unavailable, do NOT install anything mid-task; LOC + grep counts are the primary metric (roadmap says "llvm-lines (or LOC)").

- [ ] **Step 3: Verify the full matrix is green at the branch point**

```bash
uv run maturin develop
cargo test --no-default-features --features testing
cargo test -p ferro-schema-ir -p ferro-ddl-lowering -p ferro-migrate
FERRO_POSTGRES_URL=postgres://postgres:password@localhost:5432/postgres just test
```

Expected: all pass. Append the pytest summary line (test counts) to `/tmp/g2-baseline.txt`.

- [ ] **Step 4: Verify shadow-strict is green at the branch point (BOTH flags — STRICT alone is a silent no-op)**

```bash
FERRO_SHADOW_RUNTIME=1 FERRO_SHADOW_RUNTIME_STRICT=1 FERRO_POSTGRES_URL=postgres://postgres:password@localhost:5432/postgres just test
```

Expected: all pass. Append the summary line to `/tmp/g2-baseline.txt`.

---

### Task 2: `ModelMeta` compiled at registration (TDD)

**Files:**
- Modify: `src/state.rs` (struct `RegisteredModel` ~line 24, `RegisteredModel::new` ~line 36; new `#[cfg(test)] mod model_meta_tests` next to `mod session_close_tests` ~line 488)

**Interfaces:**
- Produces: `pub struct ModelMeta { pub pk_col: Option<String>, pub pk_autoincrement: bool }` with `ModelMeta::from_schema(&serde_json::Value) -> ModelMeta`; new public field `meta: ModelMeta` on `RegisteredModel`. Tasks 3–5 read `registration.meta.pk_col` / `.pk_autoincrement`.

- [ ] **Step 1: Write the failing tests** — add at the end of `src/state.rs`:

```rust
#[cfg(test)]
mod model_meta_tests {
    use super::ModelMeta;
    use serde_json::json;

    #[test]
    fn no_primary_key_yields_none_and_default_autoincrement() {
        let meta = ModelMeta::from_schema(&json!({
            "properties": {"name": {"type": "string"}}
        }));
        assert_eq!(meta.pk_col, None);
        assert!(meta.pk_autoincrement);
    }

    #[test]
    fn missing_properties_yields_none() {
        let meta = ModelMeta::from_schema(&json!({"title": "X"}));
        assert_eq!(meta.pk_col, None);
        assert!(meta.pk_autoincrement);
    }

    #[test]
    fn pk_without_autoincrement_key_defaults_true() {
        let meta = ModelMeta::from_schema(&json!({
            "properties": {"id": {"type": "integer", "primary_key": true}}
        }));
        assert_eq!(meta.pk_col.as_deref(), Some("id"));
        assert!(meta.pk_autoincrement);
    }

    #[test]
    fn pk_with_autoincrement_false_is_preserved() {
        let meta = ModelMeta::from_schema(&json!({
            "properties": {"id": {"type": "string", "primary_key": true, "autoincrement": false}}
        }));
        assert_eq!(meta.pk_col.as_deref(), Some("id"));
        assert!(!meta.pk_autoincrement);
    }

    #[test]
    fn non_bool_primary_key_flag_is_ignored() {
        let meta = ModelMeta::from_schema(&json!({
            "properties": {"id": {"type": "integer", "primary_key": "yes"}}
        }));
        assert_eq!(meta.pk_col, None);
        assert!(meta.pk_autoincrement);
    }

    #[test]
    fn first_flagged_column_in_map_order_wins() {
        // serde_json's default map iterates keys lexicographically, which is
        // exactly the order the old per-operation scans saw ("a_id" < "b_id").
        let meta = ModelMeta::from_schema(&json!({
            "properties": {
                "b_id": {"type": "integer", "primary_key": true, "autoincrement": false},
                "a_id": {"type": "integer", "primary_key": true}
            }
        }));
        assert_eq!(meta.pk_col.as_deref(), Some("a_id"));
        assert!(meta.pk_autoincrement);
    }
}
```

- [ ] **Step 2: Run to verify failure**

Run: `cargo test --no-default-features --features testing model_meta`
Expected: compile FAIL — `ModelMeta` not found.

- [ ] **Step 3: Implement `ModelMeta` and wire it into `RegisteredModel`** — in `src/state.rs`, directly above `pub struct RegisteredModel`:

```rust
/// Primary-key facts scanned once from the enriched schema at registration
/// (FF-G G2): the per-operation `properties` walks collapsed to one place.
/// The schema is immutable after registration (a re-registration swaps the
/// whole `Arc<RegisteredModel>`), so this can never go stale.
#[derive(Debug)]
pub struct ModelMeta {
    /// First property flagged `"primary_key": true`, in schema-map iteration
    /// order.
    pub pk_col: Option<String>,
    /// The PK's `"autoincrement"` flag; `true` when the key is absent — and
    /// `true` when there is no PK at all, matching the historical per-site
    /// default that `build_save_sql` receives.
    pub pk_autoincrement: bool,
}

impl ModelMeta {
    pub fn from_schema(schema: &serde_json::Value) -> Self {
        let mut pk_col = None;
        let mut pk_autoincrement = true;
        if let Some(properties) = schema.get("properties").and_then(|p| p.as_object()) {
            for (col_name, col_info) in properties {
                if col_info
                    .get("primary_key")
                    .and_then(|pk| pk.as_bool())
                    .unwrap_or(false)
                {
                    pk_col = Some(col_name.clone());
                    pk_autoincrement = col_info
                        .get("autoincrement")
                        .and_then(|auto| auto.as_bool())
                        .unwrap_or(true);
                    break;
                }
            }
        }
        ModelMeta { pk_col, pk_autoincrement }
    }
}
```

Then add the field to `RegisteredModel` (after `codec_plan`):

```rust
    /// Per-column codec decisions, compiled once at registration.
    pub codec_plan: crate::codec_plan::ModelCodecPlan,
    /// Primary-key metadata, scanned once at registration (FF-G G2).
    pub meta: ModelMeta,
```

And in `RegisteredModel::new`:

```rust
    pub fn new(schema: serde_json::Value, table_name: String) -> Arc<Self> {
        let codec_plan = crate::codec_plan::ModelCodecPlan::compile(&schema);
        let meta = ModelMeta::from_schema(&schema);
        Arc::new(RegisteredModel { schema, table_name, codec_plan, meta })
    }
}
```

- [ ] **Step 4: Run tests to verify pass**

Run: `cargo test --no-default-features --features testing model_meta`
Expected: 6 passed. Then the full crate: `cargo test --no-default-features --features testing` — all pass (existing `RegisteredModel::new` users are unaffected; nothing reads `meta` yet).

- [ ] **Step 5: Commit**

```bash
git add src/state.rs
git commit -m "refactor(ff-g): compile ModelMeta pk scan at registration (G2, #228)"
```

---

### Task 3: PK-scan cluster A — `fetch_all`, `fetch_one`

**Files:**
- Modify: `src/operations.rs` (`fetch_all` scan ~887–905, `fetch_one` scan ~1024–1039)

**Interfaces:**
- Consumes: `schema.meta.pk_col` from Task 2 (`schema` is the local `Arc<RegisteredModel>` binding in each op — already present at every site).

- [ ] **Step 1: Replace the scan in `fetch_all`** — old:

```rust
        let (sql, pk_col, schema_for_decode) = {
            let mut pk = None;
            if let Some(properties) = schema.schema.get("properties").and_then(|p| p.as_object()) {
                for (col_name, col_info) in properties {
                    if col_info
                        .get("primary_key")
                        .and_then(|pk| pk.as_bool())
                        .unwrap_or(false)
                    {
                        pk = Some(col_name.clone());
                        break;
                    }
                }
            }
            let mut stmt = Query::select();
```

new:

```rust
        let (sql, pk_col, schema_for_decode) = {
            let pk = schema.meta.pk_col.clone();
            let mut stmt = Query::select();
```

(the rest of the block — `stmt.column(...)`, the `(s, pk, schema.clone())` tuple — is untouched).

- [ ] **Step 2: Replace the scan in `fetch_one`** — old:

```rust
        let (sql, bind_values, _pk_col_name, schema_for_decode) = {
            let mut pk = None;
            if let Some(properties) = schema.schema.get("properties").and_then(|p| p.as_object()) {
                for (col_name, col_info) in properties {
                    if col_info
                        .get("primary_key")
                        .and_then(|pk| pk.as_bool())
                        .unwrap_or(false)
                    {
                        pk = Some(col_name.clone());
                        break;
                    }
                }
            }
            let pk_name =
                pk.ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("No primary key"))?;
```

new (error string identical):

```rust
        let (sql, bind_values, _pk_col_name, schema_for_decode) = {
            let pk_name = schema
                .meta
                .pk_col
                .clone()
                .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("No primary key"))?;
```

- [ ] **Step 3: Build + full test cycle**

```bash
uv run maturin develop
cargo test --no-default-features --features testing
FERRO_POSTGRES_URL=postgres://postgres:password@localhost:5432/postgres just test
```

Expected: all pass, pytest counts identical to `/tmp/g2-baseline.txt`.

- [ ] **Step 4: Commit**

```bash
git add src/operations.rs
git commit -m "refactor(ff-g): fetch_all/fetch_one read pk from ModelMeta (G2, #228)"
```

---

### Task 4: PK-scan cluster B — `save_record`, `update_record`, `save_bulk_records`

**Files:**
- Modify: `src/operations.rs` (`save_record` ~1332–1349, `update_record` ~1451–1463, `save_bulk_records` ~1550–1568)

**Interfaces:**
- Consumes: `schema.meta.pk_col`, `schema.meta.pk_autoincrement` (Task 2).

- [ ] **Step 1: `save_record`** — old:

```rust
        let mut pk_col = None;
        let mut pk_is_auto = true;
        if let Some(properties) = schema.schema.get("properties").and_then(|p| p.as_object()) {
            for (col_name, col_info) in properties {
                if col_info
                    .get("primary_key")
                    .and_then(|pk| pk.as_bool())
                    .unwrap_or(false)
                {
                    pk_col = Some(col_name.clone());
                    pk_is_auto = col_info
                        .get("autoincrement")
                        .and_then(|auto| auto.as_bool())
                        .unwrap_or(true);
                    break;
                }
            }
        }
```

new:

```rust
        let pk_col = schema.meta.pk_col.clone();
        let pk_is_auto = schema.meta.pk_autoincrement;
```

- [ ] **Step 2: `update_record`** — old:

```rust
        let mut pk_col = None;
        if let Some(properties) = schema.schema.get("properties").and_then(|p| p.as_object()) {
            for (col_name, col_info) in properties {
                if col_info
                    .get("primary_key")
                    .and_then(|pk| pk.as_bool())
                    .unwrap_or(false)
                {
                    pk_col = Some(col_name.clone());
                    break;
                }
            }
        }
```

new:

```rust
        let pk_col = schema.meta.pk_col.clone();
```

- [ ] **Step 3: `save_bulk_records`** — old (note the slightly different `let is_pk = ...` shape):

```rust
        let mut pk_col = None;
        let mut pk_is_auto = true;
        if let Some(properties) = schema.schema.get("properties").and_then(|p| p.as_object()) {
            for (col_name, col_info) in properties {
                let is_pk = col_info
                    .get("primary_key")
                    .and_then(|pk| pk.as_bool())
                    .unwrap_or(false);

                if is_pk {
                    pk_col = Some(col_name.clone());
                    pk_is_auto = col_info
                        .get("autoincrement")
                        .and_then(|auto| auto.as_bool())
                        .unwrap_or(true);
                    break;
                }
            }
        }
```

new:

```rust
        let pk_col = schema.meta.pk_col.clone();
        let pk_is_auto = schema.meta.pk_autoincrement;
```

- [ ] **Step 4: Build + full test cycle** (same three commands as Task 3 Step 3). Expected: all pass, counts identical to baseline.

- [ ] **Step 5: Commit**

```bash
git add src/operations.rs
git commit -m "refactor(ff-g): save/update/bulk pk discovery via ModelMeta (G2, #228)"
```

---

### Task 5: PK-scan cluster C — `fetch_filtered`, `count_filtered`, `delete_record`

**Files:**
- Modify: `src/operations.rs` (`fetch_filtered` ~1655–1668, `count_filtered` ~1843–1858, `delete_record` ~1986–2001)

**Interfaces:**
- Consumes: `schema.meta.pk_col` (Task 2).

- [ ] **Step 1: `fetch_filtered`** — old:

```rust
        let (sql, bind_values, pk_col, schema_for_decode) = {
            let mut pk = None;
            if let Some(properties) = schema.schema.get("properties").and_then(|p| p.as_object()) {
                for (col_name, col_info) in properties {
                    if col_info
                        .get("primary_key")
                        .and_then(|pk| pk.as_bool())
                        .unwrap_or(false)
                    {
                        pk = Some(col_name.clone());
                        break;
                    }
                }
            }

            let mut select = Query::select();
```

new:

```rust
        let (sql, bind_values, pk_col, schema_for_decode) = {
            let pk = schema.meta.pk_col.clone();

            let mut select = Query::select();
```

The M2M branch below keeps its exact required-pk check (`pk.as_ref().ok_or_else(... "No primary key for M2M join")`) — do not touch it.

- [ ] **Step 2: `count_filtered`** — inside the `if let Some(m2m) = &plan.m2m {` branch, old:

```rust
                // We need the PK name of the target table to join
                let mut pk = None;
                if let Some(properties) = schema.schema.get("properties").and_then(|p| p.as_object()) {
                    for (col_name, col_info) in properties {
                        if col_info
                            .get("primary_key")
                            .and_then(|pk| pk.as_bool())
                            .unwrap_or(false)
                        {
                            pk = Some(col_name.clone());
                            break;
                        }
                    }
                }
                let pk_name =
                    pk.ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("No primary key"))?;
```

new (error string identical; the scan was branch-local and pure, so hoisting it to registration is unobservable):

```rust
                // We need the PK name of the target table to join
                let pk_name = schema
                    .meta
                    .pk_col
                    .clone()
                    .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("No primary key"))?;
```

- [ ] **Step 3: `delete_record`** — old:

```rust
        let (sql, bind_values) = {
            let mut pk = None;
            if let Some(properties) = schema.schema.get("properties").and_then(|p| p.as_object()) {
                for (col_name, col_info) in properties {
                    if col_info
                        .get("primary_key")
                        .and_then(|pk| pk.as_bool())
                        .unwrap_or(false)
                    {
                        pk = Some(col_name.clone());
                        break;
                    }
                }
            }
            let pk_name =
                pk.ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("No primary key"))?;
```

new:

```rust
        let (sql, bind_values) = {
            let pk_name = schema
                .meta
                .pk_col
                .clone()
                .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("No primary key"))?;
```

- [ ] **Step 4: Verify the scans are gone**

Run: `grep -c '\.get("primary_key")' src/operations.rs`
Expected: `0` (the one remaining scan lives in `src/state.rs::ModelMeta::from_schema`).

- [ ] **Step 5: Build + full test cycle** (same three commands as Task 3 Step 3). Expected: all pass, counts identical to baseline.

- [ ] **Step 6: Commit**

```bash
git add src/operations.rs
git commit -m "refactor(ff-g): filtered ops and delete_record pk via ModelMeta (G2, #228)"
```

---

### Task 6: `Executor` enum + reimplement `execute_statement_with_optional_tx` (TDD)

**Files:**
- Modify: `src/operations.rs` (replace `execute_statement_with_optional_tx` ~362–381; add `Executor` directly above it; add `#[cfg(test)] mod executor_tests` near the other test modules at the end of the file)

**Interfaces:**
- Produces:
  - `#[derive(Clone, Copy)] enum Executor<'a> { Tx(&'a TransactionConnection), Pool(&'a EngineHandle) }`
  - `Executor::new(tx_conn: Option<&'a TransactionConnection>, engine: &'a EngineHandle) -> Executor<'a>`
  - `async fn fetch_all(&self, sql: &str, binds: &[EngineBindValue]) -> Result<Vec<EngineRow>, sqlx::Error>`
  - `async fn execute(&self, sql: &str, binds: &[EngineBindValue]) -> Result<u64, sqlx::Error>`
  - `async fn execute_result(&self, sql: &str, binds: &[EngineBindValue]) -> Result<crate::backend::EngineExecuteResult, sqlx::Error>`
  - `execute_statement_with_optional_tx`'s `tx_conn` param becomes `Option<&TransactionConnection>`; its eight callers add `.as_ref()`.
- Consumed by Tasks 7–9.

- [ ] **Step 1: Write the failing tests** — add near the other `#[cfg(test)]` modules at the end of `src/operations.rs`:

```rust
#[cfg(test)]
mod executor_tests {
    use super::Executor;
    use crate::backend::{EngineBindValue, EngineHandle};
    use crate::state::TransactionConnection;
    use sqlx::sqlite::SqlitePoolOptions;
    use std::sync::Arc;
    use tokio::sync::Mutex;

    /// max_connections(1): one shared in-memory database, and any query the
    /// executor wrongly routed to the pool while the tx holds the only
    /// connection would hang instead of silently passing.
    async fn sqlite_engine() -> EngineHandle {
        let pool = SqlitePoolOptions::new()
            .max_connections(1)
            .connect("sqlite::memory:")
            .await
            .expect("sqlite memory pool");
        EngineHandle::new_sqlite(pool)
    }

    #[tokio::test]
    async fn pool_variant_executes_and_fetches() {
        let engine = sqlite_engine().await;
        let exec = Executor::new(None, &engine);
        exec.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)", &[])
            .await
            .unwrap();
        let n = exec
            .execute(
                "INSERT INTO t (v) VALUES (?)",
                &[EngineBindValue::String("x".to_string())],
            )
            .await
            .unwrap();
        assert_eq!(n, 1);
        let res = exec
            .execute_result(
                "INSERT INTO t (v) VALUES (?)",
                &[EngineBindValue::String("y".to_string())],
            )
            .await
            .unwrap();
        assert_eq!(res.rows_affected, 1);
        assert_eq!(res.last_insert_id, Some(2));
        let rows = exec.fetch_all("SELECT v FROM t ORDER BY id", &[]).await.unwrap();
        assert_eq!(rows.len(), 2);
    }

    #[tokio::test]
    async fn tx_variant_routes_through_the_transaction_connection() {
        let engine = sqlite_engine().await;
        Executor::new(None, &engine)
            .execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)", &[])
            .await
            .unwrap();

        let conn = engine
            .begin_transaction_connection()
            .await
            .expect("begin transaction connection");
        let tx: TransactionConnection = Arc::new(Mutex::new(conn));
        let exec = Executor::new(Some(&tx), &engine);
        exec.execute(
            "INSERT INTO t (v) VALUES (?)",
            &[EngineBindValue::String("x".to_string())],
        )
        .await
        .unwrap();
        let rows = exec.fetch_all("SELECT v FROM t", &[]).await.unwrap();
        assert_eq!(rows.len(), 1, "tx must see its own uncommitted write");

        tx.lock().await.rollback().await.unwrap();
        drop(tx);

        let rows = Executor::new(None, &engine)
            .fetch_all("SELECT v FROM t", &[])
            .await
            .unwrap();
        assert!(rows.is_empty(), "rolled-back insert must not be visible");
    }
}
```

- [ ] **Step 2: Run to verify failure**

Run: `cargo test --no-default-features --features testing executor_tests`
Expected: compile FAIL — `Executor` not found.

- [ ] **Step 3: Implement `Executor` and rewire the helper** — replace the whole of `execute_statement_with_optional_tx` (old body has the `match tx_conn` with mirrored `conn.execute_sql_with_binds` / `engine.execute_sql_with_binds` arms) with:

```rust
/// One execution route for an operation: the open transaction's connection,
/// or the routed engine's pool (FF-G G2).
///
/// Replaces the per-site `match tx_conn { Some(..) => .., None => .. }`
/// mirror arms. Each method holds the transaction mutex for exactly one
/// engine call — the same guard scope as the arms it replaces — so a query
/// can never migrate off its transaction. Methods return raw `sqlx::Error`;
/// call sites keep their own `map_db_error` context labels.
#[derive(Clone, Copy)]
enum Executor<'a> {
    /// A live transaction's dedicated connection.
    Tx(&'a TransactionConnection),
    /// The routed engine's pool.
    Pool(&'a EngineHandle),
}

impl<'a> Executor<'a> {
    fn new(tx_conn: Option<&'a TransactionConnection>, engine: &'a EngineHandle) -> Self {
        match tx_conn {
            Some(tx) => Executor::Tx(tx),
            None => Executor::Pool(engine),
        }
    }

    async fn fetch_all(
        &self,
        sql: &str,
        binds: &[EngineBindValue],
    ) -> Result<Vec<EngineRow>, sqlx::Error> {
        match self {
            Executor::Tx(tx) => {
                let mut conn = tx.lock().await;
                conn.fetch_all_sql_with_binds(sql, binds).await
            }
            Executor::Pool(engine) => engine.fetch_all_sql_with_binds(sql, binds).await,
        }
    }

    async fn execute(&self, sql: &str, binds: &[EngineBindValue]) -> Result<u64, sqlx::Error> {
        match self {
            Executor::Tx(tx) => {
                let mut conn = tx.lock().await;
                conn.execute_sql_with_binds(sql, binds).await
            }
            Executor::Pool(engine) => engine.execute_sql_with_binds(sql, binds).await,
        }
    }

    async fn execute_result(
        &self,
        sql: &str,
        binds: &[EngineBindValue],
    ) -> Result<crate::backend::EngineExecuteResult, sqlx::Error> {
        match self {
            Executor::Tx(tx) => {
                let mut conn = tx.lock().await;
                conn.execute_sql_with_binds_result(sql, binds).await
            }
            Executor::Pool(engine) => engine.execute_sql_with_binds_result(sql, binds).await,
        }
    }
}

async fn execute_statement_with_optional_tx(
    engine: &EngineHandle,
    tx_conn: Option<&TransactionConnection>,
    sql: &str,
    bind_values: &[SeaValue],
) -> Result<u64, sqlx::Error> {
    let engine_bind_values = engine_bind_values_from_sea(bind_values);
    Executor::new(tx_conn, engine)
        .execute(sql, &engine_bind_values)
        .await
}
```

- [ ] **Step 4: Update the helper's eight callers** — each currently reads `execute_statement_with_optional_tx(&engine, tx_conn, &sql, &bind_values.0)`; change the second argument to `tx_conn.as_ref()`:

```rust
            execute_statement_with_optional_tx(&engine, tx_conn.as_ref(), &sql, &bind_values.0)
```

Sites (find them all with `grep -n 'execute_statement_with_optional_tx(&engine' src/operations.rs`): `update_record` (~1482), `save_bulk_records` (~1613), `delete_record` (~2024), `delete_filtered` (~2077), `update_filtered` (~2158), `add_m2m_links` (~2241), `remove_m2m_links` (~2314), `clear_m2m_links` (~2368). Nothing else about those lines changes.

- [ ] **Step 5: Run tests**

```bash
cargo test --no-default-features --features testing executor_tests
```
Expected: 2 passed. Then the full cycle:
```bash
uv run maturin develop
cargo test --no-default-features --features testing
FERRO_POSTGRES_URL=postgres://postgres:password@localhost:5432/postgres just test
```
Expected: all pass, counts identical to baseline.

- [ ] **Step 6: Commit**

```bash
git add src/operations.rs
git commit -m "refactor(ff-g): add Executor over tx/pool routes (G2, #228)"
```

---

### Task 7: Catalog path takes the `Executor`

**Files:**
- Modify: `src/operations.rs` (`postgres_catalog_rows` ~473–506, `postgres_table_catalog` ~545–594, and its 10 call sites)

**Interfaces:**
- Consumes: `Executor` (Task 6).
- Produces: `postgres_catalog_rows(engine: &EngineHandle, exec: Executor<'_>, sql: &str, table_name: &str, label: &str)`; `postgres_table_catalog(table_name: &str, engine: &EngineHandle, exec: Executor<'_>, backend: Dialect)`. Every op that calls the catalog now holds a `let exec = Executor::new(tx_conn.as_ref(), &engine);` local, which Tasks 8–9 reuse.

- [ ] **Step 1: Collapse `postgres_catalog_rows`** — replace the whole function:

```rust
async fn postgres_catalog_rows(
    engine: &EngineHandle,
    exec: Executor<'_>,
    sql: &str,
    table_name: &str,
    label: &str,
) -> PyResult<Vec<EngineRow>> {
    engine.record_catalog_query();
    let values = [EngineBindValue::String(table_name.to_string())];
    exec.fetch_all(sql, &values).await.map_err(|e| {
        crate::errors::map_db_error(
            &format!("Failed to inspect {} for '{}'", label, table_name),
            e,
        )
    })
}
```

(Confirm it has exactly one caller: `grep -n 'postgres_catalog_rows(' src/operations.rs` → the definition + the call inside `postgres_table_catalog`.)

- [ ] **Step 2: Update `postgres_table_catalog`** — signature `tx_conn: &Option<TransactionConnection>` → `exec: Executor<'_>`, and the inner call:

```rust
async fn postgres_table_catalog(
    table_name: &str,
    engine: &EngineHandle,
    exec: Executor<'_>,
    backend: Dialect,
) -> PyResult<Arc<TableCatalog>> {
```
```rust
    for row in postgres_catalog_rows(engine, exec, sql, table_name, "table catalog").await? {
```

(doc comment and everything else unchanged).

- [ ] **Step 3: Update the 10 call sites.** In each op below, insert one line immediately after the `let (…, engine, tx_conn, backend) = route_engine(r)?;` destructure (and after `let session_id = …` where present):

```rust
        let exec = Executor::new(tx_conn.as_ref(), &engine);
```

then change the catalog call's third argument from `&tx_conn` to `exec`:

| op | old call | new call |
|---|---|---|
| `save_record` (~1351) | `postgres_table_catalog(&table_name, &engine, &tx_conn, backend)` | `postgres_table_catalog(&table_name, &engine, exec, backend)` |
| `update_record` (~1465) | same shape | same replacement |
| `save_bulk_records` (~1570) | same shape | same replacement |
| `fetch_filtered` (~1652) | same shape | same replacement |
| `count_filtered` (~1829) | `postgres_table_catalog(&table_name, &engine, &tx_conn, backend)` (feeding `plan.postgres_enum_udt`) | `postgres_table_catalog(&table_name, &engine, exec, backend)` |
| `delete_filtered` (~2063) | same as count_filtered | same replacement |
| `update_filtered` (~2130) | `postgres_table_catalog(&table_name, &engine, &tx_conn, backend)` | `postgres_table_catalog(&table_name, &engine, exec, backend)` |
| `add_m2m_links` (~2212) | `postgres_table_catalog(&join_table, &engine, &tx_conn, backend)` | `postgres_table_catalog(&join_table, &engine, exec, backend)` |
| `remove_m2m_links` (~2287) | same as add_m2m | same replacement |
| `clear_m2m_links` (~2351) | same as add_m2m | same replacement |

Note: `exec` borrows `tx_conn`, and the later `execute_statement_with_optional_tx(&engine, tx_conn.as_ref(), …)` calls in these ops also only borrow (Task 6 changed the param) — no moves, no borrow conflicts.

- [ ] **Step 4: Build + full test cycle** (same three commands as Task 3 Step 3). Expected: all pass, counts identical to baseline. `_catalog_query_count_for_test`-based tests (FF-C C2 zero-catalog-query gate) must be among them and green.

- [ ] **Step 5: Commit**

```bash
git add src/operations.rs
git commit -m "refactor(ff-g): catalog path takes Executor (G2, #228)"
```

---

### Task 8: Collapse the fetch-path matches — `fetch_all`, `fetch_one`, `fetch_filtered`, `count_filtered`

**Files:**
- Modify: `src/operations.rs` (match sites ~907, ~1063, ~1717, ~1883)

**Interfaces:**
- Consumes: `Executor` (Task 6); the `exec` locals already present in `fetch_filtered`/`count_filtered` (Task 7). `fetch_all`/`fetch_one` add their own.

- [ ] **Step 1: `fetch_all`** — old:

```rust
        let parsed_data = match tx_conn {
            Some(conn_arc) => {
                let mut conn = conn_arc.lock().await;
                let rows = conn
                    .fetch_all_sql_with_binds(&sql, &[])
                    .await
                    .map_err(|e| crate::errors::map_db_error("Fetch failed", e))?;
                typed_rows_to_parsed_data(rows, &schema_for_decode, pk_col.as_deref())
            }
            None => {
                let rows = engine
                    .fetch_all_sql_with_binds(&sql, &[])
                    .await
                    .map_err(|e| crate::errors::map_db_error("Fetch failed", e))?;
                typed_rows_to_parsed_data(rows, &schema_for_decode, pk_col.as_deref())
            }
        };
```

new (also add the `exec` local after `route_engine`, as in Task 7 Step 3 — `fetch_all` didn't get one there):

```rust
        let exec = Executor::new(tx_conn.as_ref(), &engine);
        let rows = exec
            .fetch_all(&sql, &[])
            .await
            .map_err(|e| crate::errors::map_db_error("Fetch failed", e))?;
        let parsed_data = typed_rows_to_parsed_data(rows, &schema_for_decode, pk_col.as_deref());
```

- [ ] **Step 2: `fetch_one`** — old:

```rust
        let parsed_row = match tx_conn {
            Some(conn_arc) => {
                let engine_bind_values = engine_bind_values_from_sea(&bind_values.0);
                let mut conn = conn_arc.lock().await;
                let rows = conn
                    .fetch_all_sql_with_binds(&sql, &engine_bind_values)
                    .await
                    .map_err(|e| crate::errors::map_db_error("Fetch failed", e))?;
                typed_rows_to_parsed_data(rows, &schema_for_decode, None)
                    .into_iter()
                    .next()
                    .map(|(_, fields)| fields)
            }
            None => {
                let engine_bind_values = engine_bind_values_from_sea(&bind_values.0);
                let rows = engine
                    .fetch_all_sql_with_binds(&sql, &engine_bind_values)
                    .await
                    .map_err(|e| crate::errors::map_db_error("Fetch failed", e))?;
                typed_rows_to_parsed_data(rows, &schema_for_decode, None)
                    .into_iter()
                    .next()
                    .map(|(_, fields)| fields)
            }
        };
```

new (plus the `exec` local after `route_engine`):

```rust
        let exec = Executor::new(tx_conn.as_ref(), &engine);
        let engine_bind_values = engine_bind_values_from_sea(&bind_values.0);
        let rows = exec
            .fetch_all(&sql, &engine_bind_values)
            .await
            .map_err(|e| crate::errors::map_db_error("Fetch failed", e))?;
        let parsed_row = typed_rows_to_parsed_data(rows, &schema_for_decode, None)
            .into_iter()
            .next()
            .map(|(_, fields)| fields);
```

(Place the `exec` local right after the `route_engine` destructure at the top of the async block, not at the match site.)

- [ ] **Step 3: `fetch_filtered`** — old match identical in shape to `fetch_one`'s but with `pk_col.as_deref()` and no `.into_iter()` tail; new (exec exists from Task 7):

```rust
        let engine_bind_values = engine_bind_values_from_sea(&bind_values.0);
        let rows = exec
            .fetch_all(&sql, &engine_bind_values)
            .await
            .map_err(|e| crate::errors::map_db_error("Fetch failed", e))?;
        let parsed_data = typed_rows_to_parsed_data(rows, &schema_for_decode, pk_col.as_deref());
```

- [ ] **Step 4: `count_filtered`** — old:

```rust
        let engine_bind_values = engine_bind_values_from_sea(&bind_values.0);
        let count = match tx_conn {
            Some(conn_arc) => {
                let mut conn = conn_arc.lock().await;
                let rows = conn
                    .fetch_all_sql_with_binds(&sql, &engine_bind_values)
                    .await
                    .map_err(|e| crate::errors::map_db_error("Count failed", e))?;
                rows.first()
                    .and_then(|row| row.values.first())
                    .and_then(|(_, value)| value.as_i64())
                    .unwrap_or(0)
            }
            None => {
                let rows = engine
                    .fetch_all_sql_with_binds(&sql, &engine_bind_values)
                    .await
                    .map_err(|e| crate::errors::map_db_error("Count failed", e))?;
                rows.first()
                    .and_then(|row| row.values.first())
                    .and_then(|(_, value)| value.as_i64())
                    .unwrap_or(0)
            }
        };
```

new (exec exists from Task 7):

```rust
        let engine_bind_values = engine_bind_values_from_sea(&bind_values.0);
        let rows = exec
            .fetch_all(&sql, &engine_bind_values)
            .await
            .map_err(|e| crate::errors::map_db_error("Count failed", e))?;
        let count = rows
            .first()
            .and_then(|row| row.values.first())
            .and_then(|(_, value)| value.as_i64())
            .unwrap_or(0);
```

- [ ] **Step 5: Build + full test cycle** (same three commands as Task 3 Step 3). Expected: all pass, counts identical to baseline.

- [ ] **Step 6: Commit**

```bash
git add src/operations.rs
git commit -m "refactor(ff-g): fetch paths execute via Executor (G2, #228)"
```

---

### Task 9: Collapse the remaining matches — `save_record`, `update_record`, raw ops

**Files:**
- Modify: `src/operations.rs` (`save_record` match ~1366–1414, `update_record` `ExistenceCheck` arm ~1487–1500, `raw_execute` ~2641, `raw_fetch_all` ~2681, `raw_fetch_one` ~2736)

**Interfaces:**
- Consumes: `Executor` (Task 6); `save_record`/`update_record`'s `exec` locals (Task 7).

- [ ] **Step 1: `save_record`** — replace the whole `match tx_conn { … }` tail (both arms carry an identical `if needs_postgres_returning` split) with:

```rust
        let engine_bind_values = engine_bind_values_from_sea(&bind_values.0);
        if needs_postgres_returning {
            let rows = exec
                .fetch_all(&sql, &engine_bind_values)
                .await
                .map_err(|e| crate::errors::map_db_error("Save failed", e))?;
            // FF-G G4a: return the RETURNING value as decoded. A
            // non-positive id is a legitimate PK (sequence MINVALUE
            // <= 0); only a missing row / non-integer PK maps to None.
            let id = rows
                .first()
                .and_then(|row| row.values.first())
                .and_then(|(_, value)| value.as_i64());
            Ok(id)
        } else {
            let exec_res = exec
                .execute_result(&sql, &engine_bind_values)
                .await
                .map_err(|e| crate::errors::map_db_error("Save failed", e))?;
            Ok(exec_res.last_insert_id)
        }
```

(The G4a comment survives once — it was duplicated across the two arms.)

- [ ] **Step 2: `update_record`'s `ExistenceCheck` arm** — old:

```rust
            UpdateByPkSql::ExistenceCheck(sql, bind_values) => {
                let engine_bind_values = engine_bind_values_from_sea(&bind_values.0);
                let rows = match tx_conn {
                    Some(conn_arc) => {
                        let mut conn = conn_arc.lock().await;
                        conn.fetch_all_sql_with_binds(&sql, &engine_bind_values)
                            .await
                            .map_err(|e| crate::errors::map_db_error("Update failed", e))?
                    }
                    None => engine
                        .fetch_all_sql_with_binds(&sql, &engine_bind_values)
                        .await
                        .map_err(|e| crate::errors::map_db_error("Update failed", e))?,
                };
```

new:

```rust
            UpdateByPkSql::ExistenceCheck(sql, bind_values) => {
                let engine_bind_values = engine_bind_values_from_sea(&bind_values.0);
                let rows = exec
                    .fetch_all(&sql, &engine_bind_values)
                    .await
                    .map_err(|e| crate::errors::map_db_error("Update failed", e))?;
```

(The `Update` arm keeps its `execute_statement_with_optional_tx(&engine, tx_conn.as_ref(), …)` call from Task 6.)

- [ ] **Step 3: The three raw ops.** Each op's async block currently opens with a `match tx_conn` whose `None` arm resolves the engine lazily — that laziness is load-bearing (with a live tx, a missing connection must not error) and must survive. Replace each match with the deferred-init construction + one executor call.

`raw_execute` — old:

```rust
        let rows_affected = match tx_conn {
            Some(conn_arc) => {
                let mut conn = conn_arc.lock().await;
                conn.execute_sql_with_binds(&sql, &bind_values).await
            }
            None => {
                let engine = engine_for_connection(Some(route_connection_name))?;
                engine.execute_sql_with_binds(&sql, &bind_values).await
            }
        }
        .map_err(|e| crate::errors::map_db_error("Raw SQL execute failed", e))?;
```

new:

```rust
        let pool_engine;
        let exec = match &tx_conn {
            Some(tx) => Executor::Tx(tx),
            None => {
                pool_engine = engine_for_connection(Some(route_connection_name))?;
                Executor::Pool(&pool_engine)
            }
        };
        let rows_affected = exec
            .execute(&sql, &bind_values)
            .await
            .map_err(|e| crate::errors::map_db_error("Raw SQL execute failed", e))?;
```

`raw_fetch_all` — same construction, then:

```rust
        let rows = exec
            .fetch_all(&sql, &bind_values)
            .await
            .map_err(|e| crate::errors::map_db_error("Raw SQL fetch_all failed", e))?;
```

`raw_fetch_one` — same construction, then:

```rust
        let rows = exec
            .fetch_all(&sql, &bind_values)
            .await
            .map_err(|e| crate::errors::map_db_error("Raw SQL fetch_one failed", e))?;
```

(Error labels differ per op — copy each exactly. `Executor::Pool(&pool_engine)` relies on `&Arc<EngineHandle>` deref-coercing to `&EngineHandle`; if inference balks, write `Executor::Pool(&*pool_engine)`.)

- [ ] **Step 4: Verify the mirrored arms are gone**

```bash
grep -c 'match tx_conn' src/operations.rs   # expected: 0
grep -c 'match &tx_conn' src/operations.rs  # expected: 3 (raw-op lazy constructors — single-purpose, no duplicated engine-call bodies)
```

- [ ] **Step 5: Build + full test cycle** (same three commands as Task 3 Step 3). Expected: all pass, counts identical to baseline.

- [ ] **Step 6: Commit**

```bash
git add src/operations.rs
git commit -m "refactor(ff-g): save/update/raw paths via Executor (G2, #228)"
```

---

### Task 10: Final sweep — shadow-strict proof, metrics, roadmap tick

**Files:**
- Modify: `docs/plans/2026-07-02-001-fable-fixes-roadmap.md` (G2 checkbox ~line 385, FF-G exit-gate line ~line 418, possibly a roadmap-complete note at the top)

**Interfaces:**
- Consumes: `/tmp/g2-baseline.txt` (Task 1).
- Produces: `/tmp/g2-after.txt` — the deltas the PR body reports.

- [ ] **Step 1: Full verification battery**

```bash
uv run maturin develop
cargo test --no-default-features --features testing
cargo test -p ferro-schema-ir -p ferro-ddl-lowering -p ferro-migrate
FERRO_POSTGRES_URL=postgres://postgres:password@localhost:5432/postgres just test
FERRO_SHADOW_RUNTIME=1 FERRO_SHADOW_RUNTIME_STRICT=1 FERRO_POSTGRES_URL=postgres://postgres:password@localhost:5432/postgres just test
```

Expected: everything green, pytest counts identical to `/tmp/g2-baseline.txt`, zero shadow-strict mismatches. **Both env flags together** — STRICT alone is a silent no-op.

- [ ] **Step 2: Record the after-metrics and diff against baseline**

```bash
{
  echo "commit: $(git rev-parse HEAD)"
  echo "operations.rs LOC: $(wc -l < src/operations.rs)"
  echo "pk scans: $(grep -c '\.get("primary_key")' src/operations.rs)"
  echo "tx matches: $(grep -c 'match tx_conn' src/operations.rs)"
  echo "-- 'not found' census --"
  grep -rc "not found" src/ | grep -v ':0'
} | tee /tmp/g2-after.txt
cargo llvm-lines --no-default-features --features testing 2>/dev/null | head -2 | tee -a /tmp/g2-after.txt || true
diff /tmp/g2-baseline.txt /tmp/g2-after.txt
```

Expected: pk scans 8→0, tx matches 11→0, LOC meaningfully down. **The `not found` census must be unchanged, per file** — `src/operations.rs` at baseline (8: transaction-lifetime strings + doc comments, none pk-related) and `src/state.rs` at baseline (1: the FF-E `Model '…' not found` path). This refactor touches none of those lines, so both counts must equal baseline; a moved count means a loud-error string was dropped — find it.

Separately (not part of the `not found` census, since the string is `"No primary key"`), confirm the three pk-required error sites survive: `grep -c '"No primary key"' src/operations.rs` = 3 (`fetch_one`, `count_filtered` M2M, `delete_record`) plus `grep -c '"No primary key for M2M join"' src/operations.rs` = 1 (`fetch_filtered`).

- [ ] **Step 3: Confirm no SQL-builder lines changed**

```bash
git diff main -- src/operations.rs | grep -E '^[-+].*(Query::|InsertStatement|sea_query_build|sea_query_to_string|OnConflict|schema_value_expr|bind_input_to_expr)' | grep -vE '^[-+]\s*//' || echo "no builder-line changes"
```

Expected: `no builder-line changes` (rendered SQL and binds unchanged by construction).

- [ ] **Step 4: Tick the roadmap** — in `docs/plans/2026-07-02-001-fable-fixes-roadmap.md`:
  - `- [ ] **G2 — \`operations.rs\` dedup.**` → `- [x]`
  - `- [ ] \`cargo llvm-lines\` (or LOC) shows \`operations.rs\` duplication removed (G2).` → `- [x]`
  - Then check for any remaining unticked boxes: `grep -n '\- \[ \]' docs/plans/2026-07-02-001-fable-fixes-roadmap.md`. If **zero** remain, add directly under the document's title: `> **Roadmap complete (2026-07-06):** every epic (FF-A…FF-G) has shipped and all exit gates are verified.`

- [ ] **Step 5: Commit**

```bash
git add docs/plans/2026-07-02-001-fable-fixes-roadmap.md
git commit -m "docs(ff-g): tick G2 and the FF-G exit gate (#228)"
```

---

## Post-plan (session-level, not subagent tasks)

PR creation (lead with the behavior-neutrality proof: LOC/grep/llvm-lines deltas, shadow-strict-clean, unchanged loud-error census), push via the `0x054` inline-token URL, and Project #7 status updates are handled by the orchestrating session per the G2 work order — they are not plan tasks.
