//! Core database operations for Ferro models.
//!
//! This module implements high-performance CRUD operations, leveraging
//! GIL-free parsing and zero-copy Direct Injection into Python objects.

use crate::backend::{
    EngineBindValue, EngineHandle, EngineRow, EngineValue, NullKind, TableCatalog,
};
use crate::query::{JoinPlan, QueryPlan};
use crate::state::{
    Dialect, TRANSACTION_REGISTRY,
    TransactionConnection, TransactionHandle, connection_for_route, ensure_session_idle_for_close,
    engine_for_connection, register_session, session_state, unregister_session,
};
use dashmap::DashMap;
use ferro_schema_ir::{Materialization, QueryIrPayload};
use pyo3::prelude::*;
use sea_query::{
    Alias, Condition, Expr, Iden, InsertStatement, JoinType, OnConflict, Order,
    PostgresQueryBuilder, Query, SelectStatement, SimpleExpr, SqliteQueryBuilder, UpdateStatement,
    Value as SeaValue,
};
use serde::Deserialize;
use std::collections::{HashMap, HashSet};
use std::sync::Arc;

fn active_connection_for_route(using: Option<String>) -> PyResult<(String, Arc<EngineHandle>)> {
    connection_for_route(using)
}

/// Derive engine + transaction connection from an already-resolved route.
///
/// The only route-consumption site (FF-D D3): a map lookup, never a
/// re-derivation of `using`/session precedence. `route.connection_name` is
/// trusted as authoritative — Python's resolvers already folded transaction /
/// session / explicit-`using` precedence into it.
fn route_engine(
    route: &crate::state::RouteHandle,
) -> PyResult<(String, Arc<EngineHandle>, Option<TransactionConnection>, Dialect)> {
    let engine = engine_for_connection(Some(route.connection_name.clone()))?;
    let tx_conn = match &route.tx_id {
        Some(tx_id) => Some(
            tx_get(route.session_id.as_deref(), tx_id)?
                .ok_or_else(|| {
                    pyo3::exceptions::PyRuntimeError::new_err("Transaction not found")
                })?
                .conn,
        ),
        None => None,
    };
    let backend = engine.backend();
    Ok((route.connection_name.clone(), engine, tx_conn, backend))
}

/// Sweep the map for dead weakrefs every N tracked operations (FF-D D1).
///
/// Amortized O(1)/op; memory is bounded by live instances plus at most one
/// interval of tombstones. Callback-based eviction is deliberately avoided:
/// a weakref callback fires mid-GC and would re-enter the DashMap, risking a
/// shard-lock deadlock across the FFI boundary (AGENTS.md I-3).
const IDENTITY_SWEEP_INTERVAL: usize = 1024;

fn weakref_is_live(py: Python<'_>, weak: &Py<PyAny>) -> bool {
    weak.bind(py)
        .call0()
        .map(|obj| !obj.is_none())
        .unwrap_or(false)
}

/// Identity-map lock-order guard (FF-G G4c).
///
/// Every access to a session's `identity_map` DashMap must hold the GIL:
/// sweeps and liveness checks call into Python while holding a shard guard,
/// so a GIL-less thread blocking on that shard while the shard-holder waits
/// for the GIL would deadlock. GIL-before-shard is the required lock order;
/// this asserts it in debug builds (free in release). pyo3-ffi hides
/// `PyGILState_Check` behind `#[cfg(not(Py_LIMITED_API))]`, hence the local
/// extern declaration below; the call itself sits under `debug_assert!`, so
/// release abi3 wheels never reference the symbol.
#[inline]
fn debug_assert_gil_held() {
    // PyGILState_Check is a Python C API function that's available at runtime
    // since we're running as a Python extension module. Declaration works via
    // symbol lookup at runtime (Python is already loaded in the address space).
    unsafe {
        unsafe extern "C" {
            fn PyGILState_Check() -> i32;
        }
        debug_assert!(
            PyGILState_Check() == 1,
            "identity-map access without the GIL — GIL-before-shard is the required lock order"
        );
    }
}

fn maybe_sweep(
    py: Python<'_>,
    map: &DashMap<(String, String, String), Py<PyAny>>,
    ops: &std::sync::atomic::AtomicUsize,
) {
    debug_assert_gil_held();
    use std::sync::atomic::Ordering;
    if ops.fetch_add(1, Ordering::Relaxed) % IDENTITY_SWEEP_INTERVAL
        == IDENTITY_SWEEP_INTERVAL - 1
    {
        map.retain(|_, weak| weakref_is_live(py, weak));
    }
}

fn identity_map_get(
    py: Python<'_>,
    session_id: Option<&str>,
    key: &(String, String, String),
) -> PyResult<Option<Py<PyAny>>> {
    debug_assert_gil_held();
    // Sessionless operations have no identity map (FF-D Option B): nothing
    // is cached, so nothing can dedup — and nothing can go stale.
    let Some(session_id) = session_id else {
        return Ok(None);
    };
    let session = session_state(session_id)?;
    // Clone the weakref out of the shard guard before touching Python:
    // GIL-before-shard ordering is the invariant (see maybe_sweep).
    let weak = session.identity_map.get(key).map(|e| e.value().clone_ref(py));
    let Some(weak) = weak else { return Ok(None) };
    // Upgrade to a strong ref; hit-vs-miss is decided on the upgraded ref so
    // the instance cannot die between the check and use.
    let obj = weak.bind(py).call0()?;
    if obj.is_none() {
        // Dead entry: prune the tombstone, report a miss.
        session.identity_map.remove(key);
        return Ok(None);
    }
    Ok(Some(obj.unbind()))
}

fn identity_map_insert(
    py: Python<'_>,
    session_id: Option<&str>,
    key: (String, String, String),
    value: &Bound<'_, PyAny>,
) -> PyResult<()> {
    debug_assert_gil_held();
    // Sessionless operations have no identity map (FF-D Option B): no-op.
    let Some(session_id) = session_id else {
        return Ok(());
    };
    // Weak by design: the map must never keep an instance alive (FF-D D1).
    // A class that can't be weakly referenced is a loud error, never a
    // silent strong-ref fallback.
    let weak = pyo3::types::PyWeakrefReference::new(value)
        .map_err(|err| {
            pyo3::exceptions::PyTypeError::new_err(format!(
                "Cannot track instance in the identity map: the class does not \
                 support weak references ({err}). Ferro model instances must be \
                 weakly referenceable."
            ))
        })?
        .into_any()
        .unbind();
    let session = session_state(session_id)?;
    session.identity_map.insert(key, weak);
    maybe_sweep(py, &session.identity_map, &session.identity_ops);
    Ok(())
}

fn identity_map_remove(session_id: Option<&str>, key: &(String, String, String)) -> PyResult<()> {
    debug_assert_gil_held();
    let Some(session_id) = session_id else {
        return Ok(());
    };
    let session = session_state(session_id)?;
    session.identity_map.remove(key);
    Ok(())
}

fn identity_map_retain_model(session_id: Option<&str>, model_name: &str) -> PyResult<()> {
    debug_assert_gil_held();
    let Some(session_id) = session_id else {
        return Ok(());
    };
    let session = session_state(session_id)?;
    session
        .identity_map
        .retain(|(_, m_name, _), _| m_name != model_name);
    Ok(())
}

fn identity_map_clear(session_id: Option<&str>) -> PyResult<()> {
    debug_assert_gil_held();
    let Some(session_id) = session_id else {
        return Ok(());
    };
    let session = session_state(session_id)?;
    session.identity_map.clear();
    Ok(())
}

/// Count *live* identity-map entries (test diagnostic for FF-D D1).
#[pyfunction]
#[pyo3(signature = (session_id=None))]
pub fn identity_map_len(py: Python<'_>, session_id: Option<String>) -> PyResult<usize> {
    debug_assert_gil_held();
    // Sessionless operations have no identity map (FF-D Option B): always 0.
    let Some(session_id) = session_id else {
        return Ok(0);
    };
    let map = &session_state(&session_id)?.identity_map;
    Ok(map.iter().filter(|e| weakref_is_live(py, e.value())).count())
}

fn tx_get(session_id: Option<&str>, tx_id: &str) -> PyResult<Option<TransactionHandle>> {
    if let Some(session_id) = session_id {
        let session = session_state(session_id)?;
        return Ok(session
            .transaction_registry
            .get(tx_id)
            .map(|entry| entry.value().clone()));
    }
    Ok(TRANSACTION_REGISTRY
        .get(tx_id)
        .map(|entry| entry.value().clone()))
}

fn tx_insert(session_id: Option<&str>, tx_id: String, handle: TransactionHandle) -> PyResult<()> {
    if let Some(session_id) = session_id {
        let session = session_state(session_id)?;
        session.transaction_registry.insert(tx_id, handle);
        return Ok(());
    }
    TRANSACTION_REGISTRY.insert(tx_id, handle);
    Ok(())
}

fn tx_remove(session_id: Option<&str>, tx_id: &str) -> PyResult<Option<TransactionHandle>> {
    if let Some(session_id) = session_id {
        let session = session_state(session_id)?;
        return Ok(session
            .transaction_registry
            .remove(tx_id)
            .map(|(_, handle)| handle));
    }
    Ok(TRANSACTION_REGISTRY.remove(tx_id).map(|(_, handle)| handle))
}

/// The only QueryIR version this build accepts (#269, #278).
///
/// Python and Rust ship in one wheel, so there is exactly one supported version at any
/// time — no negotiation, no fallback. A mismatch can only mean a mixed build (a stale
/// `.so` next to a rebuilt Python package, or vice versa).
const SUPPORTED_QUERY_IR_VERSION: u32 = 3;

/// Envelope shell used only to read `ir_kind`/`ir_version` before committing to a strict
/// [`QueryIrPayload`] parse. `payload` is deliberately raw JSON: a real v1/v2 payload
/// (no `joins`/`path` fields, no `materialization` section) must fail on the version
/// check below with an actionable message, not on a generic "missing field" serde error
/// from parsing a payload shape this build no longer understands.
#[derive(Debug, Clone, Deserialize)]
struct QueryIrEnvelope {
    ir_kind: String,
    ir_version: u32,
    payload: serde_json::Value,
}

fn query_plan_from_ir_json(query_ir_json: &str) -> PyResult<QueryPlan> {
    let envelope: QueryIrEnvelope = serde_json::from_str(query_ir_json).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("Invalid QueryIR JSON: {}", e))
    })?;
    if envelope.ir_kind != "query" {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "Invalid QueryIR envelope kind {:?}; expected \"query\"",
            envelope.ir_kind
        )));
    }
    if envelope.ir_version != SUPPORTED_QUERY_IR_VERSION {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "Unsupported QueryIR version {}: this build of ferro accepts only version {}. \
             Python and Rust ship in one wheel, so a version mismatch means mixed builds — \
             reinstall/rebuild ferro so the Python package and the compiled extension match.",
            envelope.ir_version, SUPPORTED_QUERY_IR_VERSION
        )));
    }
    let payload: QueryIrPayload = serde_json::from_value(envelope.payload).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("Invalid QueryIR JSON: {}", e))
    })?;
    QueryPlan::from_ir_payload(payload)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("Invalid QueryIR: {e}")))
}

/// Build a SeaQuery `Condition` from `plan`'s `where_clause`.
///
/// `qualify_with`: `Some(root_table)` qualifies every column as `root_table.column`
/// (see [`crate::query::QueryPlan::to_condition_for_backend`]). All four filtered
/// walkers qualify with the root alias in production — the mutating `UPDATE`/`DELETE
/// ... WHERE` paths pass `Some(root_table)` here, and the SELECT paths use
/// [`query_condition_with_joins`] instead. `None` leaves columns unqualified and is
/// exercised only by unit tests.
fn query_condition_for_backend(
    plan: &QueryPlan,
    backend: Dialect,
    qualify_with: Option<&str>,
) -> PyResult<Condition> {
    plan.to_condition_for_backend(backend, qualify_with)
        .map_err(pyo3::exceptions::PyValueError::new_err)
}

/// Build the relation-JOIN plan for a SELECT walker (#270).
///
/// `reserved` names (the M2M association table, when present) are protected from
/// alias collisions alongside the root table. Errors map planner `String`s to
/// `PyValueError` (unknown `join_type`).
fn query_join_plan(plan: &QueryPlan, root_table: &str, reserved: &[&str]) -> PyResult<JoinPlan> {
    plan.build_join_plan(root_table, reserved)
        .map_err(pyo3::exceptions::PyValueError::new_err)
}

/// Build the WHERE `Condition` with columns qualified by their relation-path
/// JOIN alias (SELECT walkers, #270).
fn query_condition_with_joins(
    plan: &QueryPlan,
    backend: Dialect,
    root_table: &str,
    join_plan: &JoinPlan,
) -> PyResult<Condition> {
    plan.to_condition_with_joins(backend, root_table, join_plan)
        .map_err(pyo3::exceptions::PyValueError::new_err)
}

/// Render a [`JoinPlan`]'s edges onto a SELECT as aliased INNER/LEFT joins.
///
/// Each edge emits `<join> <to_table> AS <alias> ON
/// <prev_alias>.<from_column> = <alias>.<to_column>`; the join type is validated
/// (`"inner"`/`"left"`), so an unknown type is a loud error, never a mis-render.
fn apply_relation_joins(select: &mut SelectStatement, join_plan: &JoinPlan) -> PyResult<()> {
    for edge in &join_plan.renders {
        let join_type = match edge.join_type.as_str() {
            "inner" => JoinType::InnerJoin,
            "left" => JoinType::LeftJoin,
            other => {
                return Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "unsupported join_type {other:?} while rendering relation joins"
                )));
            }
        };
        select.join_as(
            join_type,
            Alias::new(&edge.to_table),
            Alias::new(&edge.alias),
            Expr::col((Alias::new(&edge.prev_alias), Alias::new(&edge.from_column)))
                .equals((Alias::new(&edge.alias), Alias::new(&edge.to_column))),
        );
    }
    Ok(())
}

/// Resolve, for every table a relation JOIN reaches, the owning model's
/// registration and native-enum catalog so a traversed WHERE leaf binds against
/// its OWN model — not the root's codec plan / enum catalog (#270 critical).
///
/// Both maps are keyed by physical table name (root and joined columns cannot
/// collide on the enum-UDT lookup). Registration resolution is route (a):
/// `MODEL_REGISTRY` is scanned by `table_name` (one model per table, so
/// unambiguous), and a joined table with NO registration is a loud error — never
/// a silent fallback to root/generic binds. The enum catalog reuses the
/// per-table `postgres_table_catalog` cache (one probe per table per schema
/// epoch), so steady-state traversal queries add no extra round-trips.
async fn populate_hop_bind_context(
    plan: &mut QueryPlan,
    join_plan: &JoinPlan,
    engine: &EngineHandle,
    exec: Executor<'_>,
    backend: Dialect,
) -> PyResult<()> {
    for edge in &join_plan.renders {
        let table = &edge.to_table;
        if !plan.hop_registrations.contains_key(table) {
            let registration = crate::state::registration_for_table(table)?;
            plan.hop_registrations.insert(table.clone(), registration);
        }
        if !plan.hop_enum_udt.contains_key(table) {
            let catalog = postgres_table_catalog(table, engine, exec, backend).await?;
            plan.hop_enum_udt
                .insert(table.clone(), catalog.enum_udt.clone());
        }
    }
    Ok(())
}

/// Reject relation traversal on a mutating operation (#270 renders joins in SELECT
/// only). The Python builder never emits joins on a mutating payload, so a `joins`
/// list or a path-carrying WHERE leaf here is misuse — fail loud.
fn reject_traversal_on_mutation(plan: &QueryPlan, operation: &str) -> PyResult<()> {
    plan.ensure_no_traversal().map_err(|detail| {
        pyo3::exceptions::PyValueError::new_err(format!(
            "{operation}() does not support relation traversal: {detail}. \
             Filter by a column on the target model, or resolve the related \
             primary keys first and {operation} by primary-key set."
        ))
    })
}

/// Reject a materialization plan this walker does not implement (#278).
///
/// Every query carries exactly one plan (ADR-0007). `root_instances` is the
/// only kind the non-projecting walkers accept: `record` is the partial-select
/// plan (built by the projecting fetch path, #279 — `count()`/`exists()` are
/// unaffected by projection and mutations reject projection at build time, so
/// the Python builder never emits `record` to any other walker); `instances`
/// is reserved for joined-row hydration (#267 stage 2) and not yet
/// implemented anywhere. A disallowed kind here is a loud error, never a
/// silent fallback to full hydration (I-6).
fn reject_unsupported_materialization(plan: &QueryPlan, operation: &str) -> PyResult<()> {
    let kind = match &plan.materialization {
        Materialization::RootInstances => return Ok(()),
        Materialization::Record { .. } => "record",
        Materialization::Instances => "instances",
    };
    Err(pyo3::exceptions::PyValueError::new_err(format!(
        "{operation}() does not support materialization kind {kind:?}: this \
         walker materializes complete root instances only (\"root_instances\"). \
         \"record\" is the partial-select plan and applies to projecting \
         fetches only; \"instances\" is reserved for joined-row hydration and \
         is not yet implemented."
    )))
}

/// Reject `limit`/`offset` on mutating operations (FF-A A1, #171).
///
/// Portable SQL has no `UPDATE/DELETE ... LIMIT`. The Python builder raises
/// before serializing and omits the keys from mutating payloads; this
/// boundary guard keeps the contract from regressing via any other caller.
fn reject_pagination_on_mutation(plan: &QueryPlan, operation: &str) -> PyResult<()> {
    if plan.limit.is_some() || plan.offset.is_some() {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "{operation}() does not support limit/offset: portable SQL has no {} ... LIMIT",
            operation.to_uppercase()
        )));
    }
    Ok(())
}

/// Persistence strategy for `save_record` (FF-A A3/A4).
///
/// `Insert` renders a plain INSERT — a duplicate primary key or unique value
/// surfaces as a database error (mapped to `UniqueViolationError`). `Upsert`
/// renders `INSERT ... ON CONFLICT (pk) DO UPDATE` and is only reachable from
/// the explicit upsert surface (`Model.upsert()` / `save(on_conflict="update")`).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum SaveMode {
    Insert,
    Upsert,
}

fn parse_save_mode(mode: &str) -> PyResult<SaveMode> {
    match mode {
        "insert" => Ok(SaveMode::Insert),
        "upsert" => Ok(SaveMode::Upsert),
        other => Err(pyo3::exceptions::PyValueError::new_err(format!(
            "save_record mode must be 'insert' or 'upsert', got {other:?}"
        ))),
    }
}

/// Map SeaQuery `Value` variants to `EngineBindValue`.
///
/// Typed `None` variants are preserved as `Null(NullKind::T)` so the bind
/// layer can emit a parameter with the correct OID on backends that perform
/// strict type validation (notably PostgreSQL). Any unmapped variant -- new
/// SeaQuery variants, feature-gated types we don't yet handle -- falls
/// through to `Null(NullKind::Untyped)`. The fallback is locked in by
/// `engine_bind_tests::falls_back_to_untyped_for_unmapped_variant`.
fn engine_bind_values_from_sea(values: &[SeaValue]) -> Vec<EngineBindValue> {
    values
        .iter()
        .map(|val| match val {
            SeaValue::Bool(Some(b)) => EngineBindValue::Bool(*b),
            SeaValue::TinyInt(Some(i)) => EngineBindValue::I64(*i as i64),
            SeaValue::SmallInt(Some(i)) => EngineBindValue::I64(*i as i64),
            SeaValue::Int(Some(i)) => EngineBindValue::I64(*i as i64),
            SeaValue::BigInt(Some(i)) => EngineBindValue::I64(*i),
            SeaValue::BigUnsigned(Some(i)) => EngineBindValue::I64(*i as i64),
            SeaValue::Float(Some(f)) => EngineBindValue::F64(*f as f64),
            SeaValue::Double(Some(f)) => EngineBindValue::F64(*f),
            SeaValue::String(Some(s)) => EngineBindValue::String(s.as_ref().clone()),
            SeaValue::Char(Some(c)) => EngineBindValue::String(c.to_string()),
            SeaValue::Bytes(Some(b)) => EngineBindValue::Bytes(b.as_ref().clone()),
            // Critical: without this arm, `Value::Uuid(Some(_))` falls through
            // to the `_ => Null(Untyped)` catch-all and silently becomes a
            // text-typed null bind on the wire. PG then rejects with
            // "column ... is of type uuid but expression is of type text".
            SeaValue::Uuid(Some(u)) => EngineBindValue::Uuid(**u),
            SeaValue::Bool(None) => EngineBindValue::Null(NullKind::Bool),
            SeaValue::TinyInt(None)
            | SeaValue::SmallInt(None)
            | SeaValue::Int(None)
            | SeaValue::BigInt(None)
            | SeaValue::BigUnsigned(None) => EngineBindValue::Null(NullKind::I64),
            SeaValue::Float(None) | SeaValue::Double(None) => EngineBindValue::Null(NullKind::F64),
            SeaValue::String(None) | SeaValue::Char(None) => {
                EngineBindValue::Null(NullKind::String)
            }
            SeaValue::Bytes(None) => EngineBindValue::Null(NullKind::Bytes),
            SeaValue::Uuid(None) => EngineBindValue::Null(NullKind::Uuid),
            _ => EngineBindValue::Null(NullKind::Untyped),
        })
        .collect()
}

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

async fn execute_transaction_sql(
    tx_conn: &TransactionConnection,
    sql: &str,
) -> Result<u64, sqlx::Error> {
    let mut conn = tx_conn.lock().await;
    conn.execute_sql(sql).await
}

#[cfg(test)]
fn engine_value_to_rust_value(
    value: EngineValue,
    schema: &serde_json::Value,
    col_name: &str,
) -> crate::state::RustValue {
    let columns = crate::schema::infer_test_schema_columns(schema);
    let plan = crate::codec_plan::ModelCodecPlan::compile_from_columns(&columns)
        .expect("test schema columns should compile");
    crate::codec::decode_engine_value(value, &plan, col_name)
}

/// One parsed row: the primary-key value (when present) plus all column values.
type ParsedRow = crate::codec::ParsedRow;

fn typed_rows_to_parsed_data(
    rows: Vec<EngineRow>,
    model: &crate::state::RegisteredModel,
    pk_col: Option<&str>,
) -> Vec<ParsedRow> {
    crate::codec::typed_rows_to_parsed_data(rows, &model.codec_plan, pk_col)
}

fn engine_row_string(row: &EngineRow, column_name: &str) -> Option<String> {
    row.values
        .iter()
        .find(|(name, _)| name == column_name)
        .and_then(|(_, value)| match value {
            EngineValue::String(value) => Some(value.clone()),
            EngineValue::I64(value) => Some(value.to_string()),
            EngineValue::Uuid(value) => Some(value.hyphenated().to_string()),
            _ => None,
        })
}

/// Convert an [`EngineRow`] into a Python `dict[str, Any]` of wire-close primitives.
///
/// Used by the raw-SQL `fetch_all` / `fetch_one` paths. UUIDs, datetimes, JSON,
/// and decimals come out as **strings** — there is no schema-driven decoding here.
/// If callers want typed rows, they should use the ORM.
fn engine_row_to_pydict<'py>(
    py: Python<'py>,
    row: EngineRow,
) -> PyResult<Bound<'py, pyo3::types::PyDict>> {
    use pyo3::IntoPyObjectExt;
    use pyo3::types::{PyBytes, PyDict};

    let dict = PyDict::new(py);
    for (col_name, value) in row.values {
        let py_val: Bound<'py, PyAny> = match value {
            EngineValue::Null => py.None().into_bound(py),
            EngineValue::Bool(b) => b.into_py_any(py)?.into_bound(py),
            EngineValue::I64(i) => i.into_py_any(py)?.into_bound(py),
            EngineValue::F64(f) => f.into_py_any(py)?.into_bound(py),
            EngineValue::String(s) => s.into_py_any(py)?.into_bound(py),
            EngineValue::Bytes(b) => PyBytes::new(py, &b).into_any(),
            // Typed Postgres wire values keep the documented raw contract:
            // rich types come out as their canonical strings. (Before FF-C C3
            // these decoded as None on Postgres — the generic ladder had no
            // arm for them.)
            EngineValue::Uuid(u) => u.hyphenated().to_string().into_py_any(py)?.into_bound(py),
            EngineValue::TimestampTz(dt) => dt
                .to_rfc3339_opts(chrono::SecondsFormat::Micros, false)
                .into_py_any(py)?
                .into_bound(py),
            EngineValue::Timestamp(dt) => dt
                .format("%Y-%m-%dT%H:%M:%S%.6f")
                .to_string()
                .into_py_any(py)?
                .into_bound(py),
            EngineValue::Date(d) => d.to_string().into_py_any(py)?.into_bound(py),
            EngineValue::Time(t) => t
                .format("%H:%M:%S%.6f")
                .to_string()
                .into_py_any(py)?
                .into_bound(py),
            EngineValue::Decimal(s) => s.into_py_any(py)?.into_bound(py),
            EngineValue::Json(v) => v.to_string().into_py_any(py)?.into_bound(py),
        };
        dict.set_item(col_name, py_val)?;
    }
    Ok(dict)
}

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

macro_rules! sea_query_build_for_backend {
    ($stmt:expr, $backend:expr) => {{
        match $backend {
            crate::state::Dialect::Sqlite => $stmt.build(SqliteQueryBuilder),
            crate::state::Dialect::Postgres => $stmt.build(PostgresQueryBuilder),
        }
    }};
}

macro_rules! sea_query_to_string_for_backend {
    ($stmt:expr, $backend:expr) => {{
        match $backend {
            crate::state::Dialect::Sqlite => $stmt.to_string(SqliteQueryBuilder),
            crate::state::Dialect::Postgres => $stmt.to_string(PostgresQueryBuilder),
        }
    }};
}

/// On Postgres, cast text-like special columns in SELECT output so Python hydration
/// sees the same string representation as SQLite.
/// One combined catalog probe per table per schema epoch.
///
/// Returns the table's [`TableCatalog`] — native enum UDTs, `uuid` columns,
/// and temporal `CAST` targets — from the engine's schema-epoch cache,
/// introspecting `pg_catalog` once on the first operation against the table.
/// `refresh_pool()` (the engine's only epoch boundary; auto-migrate calls it
/// after DDL) invalidates the cache, so steady-state CRUD issues zero
/// catalog round-trips.
///
/// Bind casts must follow DB truth rather than model-declared storage —
/// Postgres has no assignment coercion for typed parameters, so e.g. an
/// Alembic-created native enum on a column the model declares as `str` still
/// needs the `::udt` cast (and an auto-migrate TEXT enum column must not be
/// cast). See the C2 design doc for the probe evidence.
///
/// `typname` doubles as the temporal cast token: `timestamp`, `timestamptz`,
/// and `date` are exactly the strings the bind layer casts to.
async fn postgres_table_catalog(
    table_name: &str,
    engine: &EngineHandle,
    exec: Executor<'_>,
    backend: Dialect,
) -> PyResult<Arc<TableCatalog>> {
    if backend != Dialect::Postgres {
        static EMPTY: std::sync::OnceLock<Arc<TableCatalog>> = std::sync::OnceLock::new();
        return Ok(EMPTY.get_or_init(|| Arc::new(TableCatalog::default())).clone());
    }

    if let Some(cached) = engine.cached_table_catalog(table_name) {
        return Ok(cached);
    }

    let sql = r#"
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
        "#;

    let mut catalog = TableCatalog::default();
    for row in postgres_catalog_rows(engine, exec, sql, table_name, "table catalog").await? {
        let column_name = engine_row_string(&row, "column_name").unwrap_or_default();
        let udt_name = engine_row_string(&row, "udt_name").unwrap_or_default();
        let typtype = engine_row_string(&row, "typtype").unwrap_or_default();
        if column_name.is_empty() || udt_name.is_empty() {
            continue;
        }
        if typtype == "e" {
            catalog.enum_udt.insert(column_name, udt_name);
        } else if udt_name == "uuid" {
            catalog.uuid_columns.insert(column_name);
        } else if matches!(udt_name.as_str(), "timestamp" | "timestamptz" | "date") {
            catalog.ts_cast.insert(column_name, udt_name);
        }
    }

    let catalog = Arc::new(catalog);
    engine.store_table_catalog(table_name, catalog.clone());
    Ok(catalog)
}

/// Build a SeaQuery expression for a column value, preserving type information
/// across NULL and primitive binds so the SQLx layer can emit a parameter with
/// the correct OID on strict-typing backends (notably PostgreSQL).
///
/// `table_name` is used only for error diagnostics (e.g. UUID parse failures).
///
/// Schema-driven null handling: the JSON-`null` arm picks a typed SeaQuery
/// `None` variant from column metadata (JSON type / format /
/// `uuid_columns` / `ts_cast`). Non-null UUID values on Postgres are parsed
/// to `uuid::Uuid` and emitted as `Value::Uuid(Some(...))` -- no
/// `cast_as("uuid")` wrapping. See `docs/solutions/patterns/typed-null-binds.md`.
#[allow(clippy::too_many_arguments)]
fn schema_value_expr(
    model: &crate::state::RegisteredModel,
    table_name: &str,
    col_name: &str,
    value: &serde_json::Value,
    enum_udt: &HashMap<String, String>,
    uuid_columns: &HashSet<String>,
    ts_cast: &HashMap<String, String>,
    backend: Dialect,
) -> PyResult<SimpleExpr> {
    crate::codec::schema_bind_expr(
        &model.codec_plan,
        table_name,
        col_name,
        value,
        enum_udt,
        uuid_columns,
        ts_cast,
        backend,
    )
}

/// Wrap an M2M target/source ID in a SeaQuery expression for a join table.
///
/// On Postgres, UUID join columns receive a typed `Value::Uuid(Some(_))` bind
/// rather than the legacy `cast_as("uuid")` workaround. Non-string values for
/// UUID columns (e.g. an int passed to a UUID FK) and unparseable UUID strings
/// fall through to a text-cast for backward compatibility -- Postgres still
/// surfaces an `invalid input syntax for type uuid` error in those cases.
fn backend_column_value_expr(
    col_name: &str,
    value: sea_query::Value,
    uuid_columns: &HashSet<String>,
    backend: Dialect,
) -> SimpleExpr {
    crate::codec::m2m_bind_expr(col_name, value, uuid_columns, backend)
}

/// Begin a root database transaction or a nested savepoint.
///
/// Root transactions call `BEGIN` on a fresh pool connection. Nested transactions require
/// `parent_tx_id` and issue `SAVEPOINT` on the parent's connection.
///
/// Args:
///     route (RouteHandle): Resolved route (FF-D D3); `route.tx_id` is the
///         *parent* transaction for nested savepoints, or `None` to open a
///         root transaction on `route.connection_name`.
///
/// Returns:
///     str: Opaque transaction id for `commit_transaction` / `rollback_transaction`.
///
/// # Errors
/// `PyRuntimeError` when the parent transaction id is unknown, or on
/// BEGIN/SAVEPOINT failure.
#[pyfunction]
#[pyo3(signature = (route))]
pub fn begin_transaction(
    py: Python<'_>,
    route: Py<crate::state::RouteHandle>,
) -> PyResult<Bound<'_, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let r = route.get();
        let tx_id = uuid::Uuid::new_v4().to_string();
        if let Some(parent_tx_id) = r.tx_id.clone() {
            let parent = tx_get(r.session_id.as_deref(), &parent_tx_id)?
                .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("Parent transaction not found"))?;
            let conn = parent.conn.clone();
            let connection_name = parent.connection_name.clone();

            let savepoint_name = format!("sp_{}", tx_id.replace('-', "_"));
            execute_transaction_sql(&conn, &format!("SAVEPOINT {savepoint_name}"))
                .await
                .map_err(|e| crate::errors::map_db_error("Failed to create SAVEPOINT", e))?;

            tx_insert(
                r.session_id.as_deref(),
                tx_id.clone(),
                TransactionHandle::nested(conn, savepoint_name, connection_name),
            )?;
        } else {
            let (connection_name, engine, _, _) = route_engine(r)?;
            let conn = engine
                .begin_transaction_connection()
                .await
                .map_err(|e| crate::errors::map_db_error("Failed to BEGIN", e))?;

            tx_insert(
                r.session_id.as_deref(),
                tx_id.clone(),
                TransactionHandle::root(conn, connection_name),
            )?;
        }

        Ok(tx_id)
    })
}

/// Commit a transaction or release a nested savepoint.
///
/// Args:
///     tx_id (str): Id returned by `begin_transaction`.
///     session_id (str | None): Session scope when the transaction was opened.
///
/// Returns:
///     None
///
/// # Errors
/// `PyRuntimeError` when the id is unknown or `COMMIT` / `RELEASE SAVEPOINT` fails.
#[pyfunction]
#[pyo3(signature = (tx_id, session_id=None))]
pub fn commit_transaction(
    py: Python<'_>,
    tx_id: String,
    session_id: Option<String>,
) -> PyResult<Bound<'_, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let tx_handle = tx_remove(session_id.as_deref(), &tx_id)?
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("Transaction not found"))?;

        if let Some(savepoint_name) = tx_handle.savepoint_name {
            execute_transaction_sql(
                &tx_handle.conn,
                &format!("RELEASE SAVEPOINT {savepoint_name}"),
            )
            .await
            .map_err(|e| crate::errors::map_db_error("Failed to RELEASE SAVEPOINT", e))?;
        } else {
            execute_transaction_sql(&tx_handle.conn, "COMMIT")
                .await
                .map_err(|e| crate::errors::map_db_error("Failed to COMMIT", e))?;
        }

        Ok(())
    })
}

/// Return the connection name a transaction was opened on.
///
/// Args:
///     tx_id (str): Active transaction id.
///     session_id (str | None): Session scope when the transaction was opened.
///
/// Returns:
///     str: Registered connection name.
///
/// # Errors
/// `PyRuntimeError` when the transaction id is not found.
#[pyfunction]
#[pyo3(signature = (tx_id, session_id=None))]
pub fn transaction_connection_name(tx_id: String, session_id: Option<String>) -> PyResult<String> {
    tx_get(session_id.as_deref(), &tx_id)?
        .map(|tx| tx.connection_name)
        .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("Transaction not found"))
}

/// Roll back a transaction or nested savepoint and evict the transaction's
/// session-scoped identity map (a session is pinned to one connection, so
/// this is exactly the affected `(connection, session)` scope; sessionless
/// rollbacks have no map to evict).
///
/// Args:
///     tx_id (str): Id returned by `begin_transaction`.
///     session_id (str | None): Session scope when the transaction was opened.
///
/// Returns:
///     None
///
/// # Errors
/// `PyRuntimeError` when the id is unknown or rollback SQL fails.
#[pyfunction]
#[pyo3(signature = (tx_id, session_id=None))]
pub fn rollback_transaction(
    py: Python<'_>,
    tx_id: String,
    session_id: Option<String>,
) -> PyResult<Bound<'_, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let tx_handle = tx_remove(session_id.as_deref(), &tx_id)?
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("Transaction not found"))?;

        if let Some(savepoint_name) = tx_handle.savepoint_name {
            execute_transaction_sql(
                &tx_handle.conn,
                &format!("ROLLBACK TO SAVEPOINT {savepoint_name}"),
            )
            .await
            .map_err(|e| crate::errors::map_db_error("Failed to ROLLBACK TO SAVEPOINT", e))?;
            execute_transaction_sql(
                &tx_handle.conn,
                &format!("RELEASE SAVEPOINT {savepoint_name}"),
            )
            .await
            .map_err(|e| crate::errors::map_db_error("Failed to RELEASE SAVEPOINT", e))?;
        } else {
            execute_transaction_sql(&tx_handle.conn, "ROLLBACK")
                .await
                .map_err(|e| crate::errors::map_db_error("Failed to ROLLBACK", e))?;
        }

        // Re-acquire the GIL before accessing identity map (async context has no GIL).
        pyo3::Python::attach(|_py| {
            identity_map_clear(session_id.as_deref())
        })?;
        Ok(())
    })
}

/// Open a session that pins connection routing and isolates transaction/identity state.
///
/// Args:
///     using (str | None): Connection to bind; defaults to the selected default connection.
///
/// Returns:
///     tuple[str, str]: `(session_id, connection_name)`.
///
/// # Errors
/// Same routing errors as [`crate::state::connection_for_route`].
#[pyfunction]
#[pyo3(signature = (using=None))]
pub fn open_session(using: Option<String>) -> PyResult<(String, String)> {
    let (connection_name, _) = active_connection_for_route(using)?;
    let session_id = register_session();
    Ok((session_id, connection_name))
}

/// Close a session after all transactions have exited.
///
/// Args:
///     session_id (str): Id returned by `open_session`.
///
/// Returns:
///     None
///
/// # Errors
/// `PyRuntimeError` when transactions are still active or the session is unknown.
#[pyfunction]
pub fn close_session(session_id: String) -> PyResult<()> {
    ensure_session_idle_for_close(&session_id)?;
    if !unregister_session(&session_id) {
        return Err(pyo3::exceptions::PyRuntimeError::new_err(format!(
            "Session '{}' is not active",
            session_id
        )));
    }
    Ok(())
}

/// Fetches all records for a given model class.
///
/// This function releases the Python GIL during database I/O and
/// initial row parsing for maximum performance.
///
/// Args:
///     cls (PyAny): The Python model class (e.g., `User`).
///
/// Returns:
///     list[PyAny]: A list of hydrated model instances.
///
/// # Errors
/// Returns a `PyErr` if the engine is not initialized or if the query fails.
#[pyfunction]
#[pyo3(signature = (cls, route))]
pub fn fetch_all<'py>(
    py: Python<'py>,
    cls: Bound<'py, PyAny>,
    route: Py<crate::state::RouteHandle>,
) -> PyResult<Bound<'py, PyAny>> {
    let name = crate::state::model_identity(&cls)?;
    let cls_py = cls.unbind();

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let r = route.get();
        let (connection_name, engine, tx_conn, backend) = route_engine(r)?;
        let exec = Executor::new(tx_conn.as_ref(), &engine);
        let session_id = r.session_id.clone();
        let use_identity_map = engine.is_identity_map_enabled();

        let schema = crate::state::registered_model(&name)?;
        let table_name = schema.table_name.clone();
        // ... same sql generation ...
        let (sql, pk_col, schema_for_decode) = {
            let pk = schema.meta.pk_col.clone();
            let mut stmt = Query::select();
            stmt.column((Alias::new(&table_name), sea_query::Asterisk));
            let s = sea_query_to_string_for_backend!(stmt.from(Alias::new(&table_name)), backend);
            (s, pk, schema.clone())
        };

        let rows = exec
            .fetch_all(&sql, &[])
            .await
            .map_err(|e| crate::errors::map_db_error("Fetch failed", e))?;
        let parsed_data = typed_rows_to_parsed_data(rows, &schema_for_decode, pk_col.as_deref());

        Python::attach(|py| {
            let results = pyo3::types::PyList::empty(py);
            let cls = cls_py.bind(py);

            let mut py_col_names = HashMap::new();
            if let Some(first_row) = parsed_data.first() {
                for (col_name, _) in &first_row.1 {
                    py_col_names.insert(
                        col_name.clone(),
                        pyo3::types::PyString::new(py, col_name).unbind(),
                    );
                }
            }
            let enum_classes = crate::hydration::enum_classes_for(py, cls);

            for (row_pk_val, fields) in parsed_data {
                if use_identity_map
                    && let Some(ref pk_val) = row_pk_val
                    && let Some(existing_obj) = identity_map_get(
                        py,
                        session_id.as_deref(),
                        &(connection_name.clone(), name.clone(), pk_val.clone()),
                    )?
                {
                    let existing = existing_obj.bind(py);
                    crate::hydration::refresh_model_instance(
                        py,
                        cls,
                        existing,
                        fields,
                        &py_col_names,
                        &enum_classes,
                    )?;
                    results.append(existing)?;
                    continue;
                }

                let instance = crate::hydration::hydrate_model_instance(
                    py,
                    cls,
                    &connection_name,
                    fields,
                    &py_col_names,
                    &enum_classes,
                )?;

                if use_identity_map && let Some(pk_val) = row_pk_val {
                    identity_map_insert(
                        py,
                        session_id.as_deref(),
                        (connection_name.clone(), name.clone(), pk_val),
                        &instance,
                    )?;
                }

                results.append(instance)?;
            }
            Ok(results.into_any().unbind())
        })
    })
}

/// Fetches a single record by its primary key.
///
/// Priority is given to the internal Identity Map. If not found, a database
/// lookup is performed.
///
/// Args:
///     cls (PyAny): The Python model class.
///     pk_val (str): The stringified primary key value.
///
/// Returns:
///     PyAny | None: The hydrated model instance or None.
///
/// # Errors
/// Returns a `PyErr` if the engine is not initialized or if the query fails.
#[pyfunction]
#[pyo3(signature = (cls, pk_val, route))]
pub fn fetch_one<'py>(
    py: Python<'py>,
    cls: Bound<'py, PyAny>,
    pk_val: String,
    route: Py<crate::state::RouteHandle>,
) -> PyResult<Bound<'py, PyAny>> {
    let name = crate::state::model_identity(&cls)?;
    let cls_py = cls.unbind();
    // No identity-map short-circuit before the query (FF-D D1b): the map is
    // an identity/dedup structure, not a query cache — every fetch reads the
    // database and refreshes the cached instance in place.

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let r = route.get();
        let (connection_name, engine, tx_conn, backend) = route_engine(r)?;
        let exec = Executor::new(tx_conn.as_ref(), &engine);
        let session_id = r.session_id.clone();
        let use_identity_map = engine.is_identity_map_enabled();

        let schema = crate::state::registered_model(&name)?;
        let table_name = schema.table_name.clone();
        // ... sql logic ...
        let (sql, bind_values, _pk_col_name, schema_for_decode) = {
            let pk_name = schema
                .meta
                .pk_col
                .clone()
                .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("No primary key"))?;
            let mut stmt = Query::select();
            stmt.column((Alias::new(&table_name), sea_query::Asterisk));
            let no_enum_udt = HashMap::new();
            let no_uuid = HashSet::new();
            let no_ts: HashMap<String, String> = HashMap::new();
            let pk_expr = schema_value_expr(
                &schema,
                &table_name,
                &pk_name,
                &serde_json::Value::String(pk_val.clone()),
                &no_enum_udt,
                &no_uuid,
                &no_ts,
                backend,
            )?;
            let (s, values) = sea_query_build_for_backend!(
                stmt.from(Alias::new(&table_name))
                    .and_where(Expr::col(Alias::new(&pk_name)).eq(pk_expr)),
                backend
            );
            (s, values, pk_name, schema.clone())
        };

        let engine_bind_values = engine_bind_values_from_sea(&bind_values.0);
        let rows = exec
            .fetch_all(&sql, &engine_bind_values)
            .await
            .map_err(|e| crate::errors::map_db_error("Fetch failed", e))?;
        let parsed_row = typed_rows_to_parsed_data(rows, &schema_for_decode, None)
            .into_iter()
            .next()
            .map(|(_, fields)| fields);

        match parsed_row {
            Some(fields) => Python::attach(|py| {
                let cls = cls_py.bind(py);
                let py_col_names = HashMap::new();
                let enum_classes = crate::hydration::enum_classes_for(py, cls);

                if use_identity_map
                    && let Some(existing_obj) = identity_map_get(
                        py,
                        session_id.as_deref(),
                        &(connection_name.clone(), name.clone(), pk_val.clone()),
                    )?
                {
                    let existing = existing_obj.bind(py);
                    crate::hydration::refresh_model_instance(
                        py,
                        cls,
                        existing,
                        fields,
                        &py_col_names,
                        &enum_classes,
                    )?;
                    return Ok(existing.clone().unbind());
                }

                let instance = crate::hydration::hydrate_model_instance(
                    py,
                    cls,
                    &connection_name,
                    fields,
                    &py_col_names,
                    &enum_classes,
                )?;
                if use_identity_map {
                    identity_map_insert(
                        py,
                        session_id.as_deref(),
                        (connection_name.clone(), name.clone(), pk_val),
                        &instance,
                    )?;
                }
                Ok(instance.into_any().unbind())
            }),
            None => Python::attach(|py| Ok(py.None())),
        }
    })
}

/// Render the INSERT statement for `save_record`.
///
/// `SaveMode::Insert` renders a plain INSERT; `SaveMode::Upsert` adds
/// `ON CONFLICT (pk) DO UPDATE` over the non-PK columns when a conflict
/// target exists (an explicit PK value, or a non-autoincrement PK). An
/// autoincrement PK left null is omitted from the column list either way.
///
/// Returns `(sql, bind_values, needs_postgres_returning)`.
#[allow(clippy::too_many_arguments)]
fn build_save_sql(
    model: &crate::state::RegisteredModel,
    table_name: &str,
    bind_inputs: &[(String, BindInput)],
    pk_col: Option<&str>,
    pk_is_auto: bool,
    mode: SaveMode,
    backend: Dialect,
    enum_udt: &HashMap<String, String>,
    uuid_columns: &HashSet<String>,
    ts_cast: &HashMap<String, String>,
) -> PyResult<(String, sea_query::Values, bool)> {
    let mut columns = Vec::new();
    let mut values = Vec::new();
    let mut pk_provided = false;
    for (key, input) in bind_inputs {
        let is_pk = pk_col == Some(key.as_str());
        if is_pk && pk_is_auto && input.is_json_null() {
            continue;
        }
        if is_pk && !input.is_json_null() {
            pk_provided = true;
        }
        columns.push(Alias::new(key));
        values.push(bind_input_to_expr(
            model,
            table_name,
            key,
            input,
            enum_udt,
            uuid_columns,
            ts_cast,
            backend,
        )?);
    }
    let mut insert_stmt = InsertStatement::new()
        .into_table(Alias::new(table_name))
        .columns(columns.clone())
        .values(values)
        .map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("invalid INSERT values: {e}"))
        })?
        .to_owned();
    if mode == SaveMode::Upsert
        && let Some(pk) = pk_col
        && (pk_provided || !pk_is_auto)
    {
        let mut on_conflict = OnConflict::column(Alias::new(pk));
        let mut update_cols = Vec::new();
        for col in &columns {
            if col.to_string() != pk {
                update_cols.push(col.clone());
            }
        }
        if !update_cols.is_empty() {
            on_conflict.update_columns(update_cols);
            insert_stmt.on_conflict(on_conflict);
        }
    }
    let needs_postgres_returning =
        backend == Dialect::Postgres && pk_col.is_some() && pk_is_auto && !pk_provided;
    let (mut sql, values) = sea_query_build_for_backend!(insert_stmt, backend);
    if needs_postgres_returning && let Some(pk) = pk_col {
        sql.push_str(&format!(" RETURNING \"{}\"", pk));
    }
    Ok((sql, values, needs_postgres_returning))
}

/// SQL plan for `update_record`: either a real UPDATE, or — for models whose
/// only column is the primary key — an existence check that preserves the
/// rows-matched contract (0 means the row is gone) without rendering an
/// `UPDATE ... SET` with an empty assignment list, which is invalid SQL.
#[derive(Debug)]
enum UpdateByPkSql {
    Update(String, sea_query::Values),
    ExistenceCheck(String, sea_query::Values),
}

#[allow(clippy::too_many_arguments)]
fn build_update_by_pk_sql(
    model: &crate::state::RegisteredModel,
    table_name: &str,
    bind_inputs: &[(String, BindInput)],
    pk_col: Option<&str>,
    backend: Dialect,
    enum_udt: &HashMap<String, String>,
    uuid_columns: &HashSet<String>,
    ts_cast: &HashMap<String, String>,
) -> PyResult<UpdateByPkSql> {
    let pk = pk_col.ok_or_else(|| {
        pyo3::exceptions::PyValueError::new_err(format!(
            "Model for table '{table_name}' has no primary key; cannot UPDATE by primary key"
        ))
    })?;
    let pk_input = bind_inputs
        .iter()
        .find(|(key, _)| key.as_str() == pk)
        .filter(|(_, input)| !input.is_json_null())
        .ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err(format!(
                "Cannot UPDATE '{table_name}' without a primary key value"
            ))
        })?;
    let pk_expr = bind_input_to_expr(
        model,
        table_name,
        pk,
        &pk_input.1,
        enum_udt,
        uuid_columns,
        ts_cast,
        backend,
    )?;

    let mut update_stmt = UpdateStatement::new()
        .table(Alias::new(table_name))
        .to_owned();
    let mut set_count = 0usize;
    for (key, input) in bind_inputs {
        if key.as_str() == pk {
            continue;
        }
        update_stmt.value(
            Alias::new(key),
            bind_input_to_expr(
                model,
                table_name,
                key,
                input,
                enum_udt,
                uuid_columns,
                ts_cast,
                backend,
            )?,
        );
        set_count += 1;
    }
    if set_count == 0 {
        let select = Query::select()
            .expr(Expr::cust("COUNT(*)"))
            .from(Alias::new(table_name))
            .and_where(Expr::col(Alias::new(pk)).eq(pk_expr))
            .to_owned();
        let (sql, values) = sea_query_build_for_backend!(select, backend);
        return Ok(UpdateByPkSql::ExistenceCheck(sql, values));
    }
    update_stmt.and_where(Expr::col(Alias::new(pk)).eq(pk_expr));
    let (sql, values) = sea_query_build_for_backend!(update_stmt, backend);
    Ok(UpdateByPkSql::Update(sql, values))
}

/// Persists a model's data to the database.
///
/// `mode` selects the persistence strategy: `"insert"` (default) renders a
/// plain INSERT — a duplicate primary key or unique value raises
/// `UniqueViolationError` — while `"upsert"` renders
/// `INSERT ... ON CONFLICT (pk) DO UPDATE` for the explicit upsert surface.
///
/// Args:
///     name (str): The model name.
///     data (dict[str, Any]): Per-column value map for the model instance.
///         ``bytes``/``bytearray`` values are preserved verbatim; all other
///         values are routed through the schema-guided casting authority.
///     mode (str): `"insert"` or `"upsert"`.
///
/// # Errors
/// Returns a `PyErr` if the engine is not initialized, the model is not
/// registered, the mode is unknown, or a column value cannot be bound.
#[pyfunction]
#[pyo3(signature = (name, data, route, mode="insert"))]
pub fn save_record<'py>(
    py: Python<'py>,
    name: String,
    data: Bound<'py, pyo3::types::PyDict>,
    route: Py<crate::state::RouteHandle>,
    mode: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let bind_inputs = bind_inputs_from_py(&data)?; // GIL held here, before the async move
    let mode = parse_save_mode(mode)?;
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let r = route.get();
        let (_connection_name, engine, tx_conn, backend) = route_engine(r)?;
        let exec = Executor::new(tx_conn.as_ref(), &engine);

        let schema = crate::state::registered_model(&name)?;
        let table_name = schema.table_name.clone();

        let pk_col = schema.meta.pk_col.clone();
        let pk_is_auto = schema.meta.pk_autoincrement;

        let catalog = postgres_table_catalog(&table_name, &engine, exec, backend).await?;
        let TableCatalog { enum_udt, uuid_columns, ts_cast } = &*catalog;
        let (sql, bind_values, needs_postgres_returning) = build_save_sql(
            &schema,
            &table_name,
            &bind_inputs,
            pk_col.as_deref(),
            pk_is_auto,
            mode,
            backend,
            enum_udt,
            uuid_columns,
            ts_cast,
        )?;

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
    })
}

/// UPDATE one row by primary key: `UPDATE <table> SET <non-pk cols> WHERE <pk> = ?`.
///
/// Args:
///     name (str): The model name.
///     data (dict[str, Any]): Per-column value map for the model instance,
///         including the primary key (used for the WHERE filter, never SET).
///
/// Returns:
///     int: Rows matched. 0 means no row with that primary key exists any
///     more — the Python layer raises `ModelDoesNotExist`. For models whose
///     only column is the primary key there is nothing to SET; an existence
///     check runs instead so the 0-means-gone contract holds.
///
/// # Errors
/// `PyValueError` when the model has no primary key or the primary-key value
/// is missing/null; `PyRuntimeError` on registry failures; typed
/// `ferro.exceptions` subclasses on database errors.
#[pyfunction]
#[pyo3(signature = (name, data, route))]
pub fn update_record<'py>(
    py: Python<'py>,
    name: String,
    data: Bound<'py, pyo3::types::PyDict>,
    route: Py<crate::state::RouteHandle>,
) -> PyResult<Bound<'py, PyAny>> {
    let bind_inputs = bind_inputs_from_py(&data)?; // GIL held here, before the async move
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let r = route.get();
        let (_connection_name, engine, tx_conn, backend) = route_engine(r)?;
        let exec = Executor::new(tx_conn.as_ref(), &engine);

        let schema = crate::state::registered_model(&name)?;
        let table_name = schema.table_name.clone();

        let pk_col = schema.meta.pk_col.clone();

        let catalog = postgres_table_catalog(&table_name, &engine, exec, backend).await?;
        let TableCatalog { enum_udt, uuid_columns, ts_cast } = &*catalog;

        let plan = build_update_by_pk_sql(
            &schema,
            &table_name,
            &bind_inputs,
            pk_col.as_deref(),
            backend,
            enum_udt,
            uuid_columns,
            ts_cast,
        )?;

        match plan {
            UpdateByPkSql::Update(sql, bind_values) => {
                let rows_affected =
                    execute_statement_with_optional_tx(&engine, tx_conn.as_ref(), &sql, &bind_values.0)
                        .await
                        .map_err(|e| crate::errors::map_db_error("Update failed", e))?;
                Ok(rows_affected)
            }
            UpdateByPkSql::ExistenceCheck(sql, bind_values) => {
                let engine_bind_values = engine_bind_values_from_sea(&bind_values.0);
                let rows = exec
                    .fetch_all(&sql, &engine_bind_values)
                    .await
                    .map_err(|e| crate::errors::map_db_error("Update failed", e))?;
                let count = rows
                    .first()
                    .and_then(|row| row.values.first())
                    .and_then(|(_, value)| value.as_i64())
                    .unwrap_or(0);
                Ok(count.max(0) as u64)
            }
        }
    })
}

/// Persist multiple model instances in a single batch `INSERT`.
///
/// Args:
///     name (str): Model class name.
///     rows (list[dict[str, Any]]): Per-row column→value maps. Bytes columns are
///         preserved verbatim; all other values flow through the typed codec path.
///     tx_id (str | None): Optional active transaction.
///     using (str | None): Connection override.
///     session_id (str | None): Session-scoped routing when set.
///
/// Returns:
///     int: Number of rows inserted.
///
/// # Errors
/// `PyRuntimeError` on registry/execute failures; `PyTypeError` for unbindable values.
#[pyfunction]
#[pyo3(signature = (name, rows, route))]
pub fn save_bulk_records<'py>(
    py: Python<'py>,
    name: String,
    rows: Vec<Bound<'py, pyo3::types::PyDict>>,
    route: Py<crate::state::RouteHandle>,
) -> PyResult<Bound<'py, PyAny>> {
    let record_inputs: Vec<Vec<(String, BindInput)>> = rows
        .iter()
        .map(bind_inputs_from_py)
        .collect::<PyResult<_>>()?;
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let r = route.get();
        let (_connection_name, engine, tx_conn, backend) = route_engine(r)?;
        let exec = Executor::new(tx_conn.as_ref(), &engine);

        let schema = crate::state::registered_model(&name)?;
        let table_name = schema.table_name.clone();

        if record_inputs.is_empty() {
            return Ok(0);
        }

        let pk_col = schema.meta.pk_col.clone();
        let pk_is_auto = schema.meta.pk_autoincrement;

        let catalog = postgres_table_catalog(&table_name, &engine, exec, backend).await?;
        let TableCatalog { enum_udt, uuid_columns, ts_cast } = &*catalog;
        let (sql, bind_values) = {
            let mut insert_stmt = InsertStatement::new()
                .into_table(Alias::new(&table_name))
                .to_owned();

            // Columns come from the first row's order (skip a null auto-pk).
            let mut column_names: Vec<String> = Vec::new();
            for (key, input) in &record_inputs[0] {
                let is_pk = pk_col.as_deref() == Some(key.as_str());
                if is_pk && pk_is_auto && input.is_json_null() {
                    continue;
                }
                column_names.push(key.clone());
            }
            insert_stmt.columns(column_names.iter().map(|c| Alias::new(c)));

            let null_input = BindInput::Json(serde_json::Value::Null);
            for row in &record_inputs {
                let lookup: std::collections::HashMap<&str, &BindInput> =
                    row.iter().map(|(k, v)| (k.as_str(), v)).collect();
                let mut row_values = Vec::with_capacity(column_names.len());
                for col in &column_names {
                    let input = lookup.get(col.as_str()).copied().unwrap_or(&null_input);
                    row_values.push(bind_input_to_expr(
                        &schema, &table_name, col, input,
                        enum_udt, uuid_columns, ts_cast, backend,
                    )?);
                }
                insert_stmt.values(row_values).map_err(|e| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!(
                        "Statement build failed: {}",
                        e
                    ))
                })?;
            }

            let (s, values) = sea_query_build_for_backend!(insert_stmt, backend);
            (s, values)
        };

        let rows_affected =
            execute_statement_with_optional_tx(&engine, tx_conn.as_ref(), &sql, &bind_values.0)
                .await
                .map_err(|e| {
                    crate::errors::map_db_error(&format!("Bulk save failed for '{}'", name), e)
                })?;

        Ok(rows_affected)
    })
}

/// Fetches records for a given model class based on a QueryIR-defined query.
///
/// Args:
///     cls (PyAny): The Python model class.
///     query_ir_json (str): The serialized QueryIR envelope JSON.
///
/// Returns:
///     list[PyAny]: A list of hydrated model instances.
#[pyfunction]
#[pyo3(signature = (cls, query_ir_json, route))]
pub fn fetch_filtered<'py>(
    py: Python<'py>,
    cls: Bound<'py, PyAny>,
    query_ir_json: String,
    route: Py<crate::state::RouteHandle>,
) -> PyResult<Bound<'py, PyAny>> {
    let name = crate::state::model_identity(&cls)?;
    let cls_py = cls.unbind();

    let mut plan = query_plan_from_ir_json(&query_ir_json)?;
    reject_unsupported_materialization(&plan, "fetch_filtered")?;

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let r = route.get();
        let (connection_name, engine, tx_conn, backend) = route_engine(r)?;
        let session_id = r.session_id.clone();
        let exec = Executor::new(tx_conn.as_ref(), &engine);
        let use_identity_map = engine.is_identity_map_enabled();

        let schema = crate::state::registered_model(&name)?;
        let table_name = schema.table_name.clone();
        let catalog = postgres_table_catalog(&table_name, &engine, exec, backend).await?;
        plan.postgres_enum_udt = catalog.enum_udt.clone();

        // Relation-traversal JOINs (#270): the M2M association table (when
        // present) is reserved so a relation alias can never collide with it.
        // The join plan is built up front so each hop table's registration +
        // enum catalog can be resolved before conditions bind (#270 critical).
        let m2m_join_table = plan.m2m.as_ref().map(|m2m| m2m.join_table.clone());
        let reserved: Vec<&str> = m2m_join_table.as_deref().into_iter().collect();
        let join_plan = query_join_plan(&plan, &table_name, &reserved)?;
        populate_hop_bind_context(&mut plan, &join_plan, &engine, exec, backend).await?;
        // ...
        let (sql, bind_values, pk_col, schema_for_decode) = {
            let pk = schema.meta.pk_col.clone();

            let mut select = Query::select();
            select.column((Alias::new(&table_name), sea_query::Asterisk));
            select.from(Alias::new(&table_name));

            if let Some(m2m) = &plan.m2m {
                let join_table = Alias::new(&m2m.join_table);
                let source_col = Alias::new(&m2m.source_col);
                let target_col = Alias::new(&m2m.target_col);
                let pk_name = pk.as_ref().ok_or_else(|| {
                    pyo3::exceptions::PyRuntimeError::new_err("No primary key for M2M join")
                })?;

                select.inner_join(
                    join_table.clone(),
                    Expr::col((Alias::new(&table_name), Alias::new(pk_name)))
                        .equals((join_table.clone(), target_col.clone())),
                );
                select.and_where(Expr::col((join_table.clone(), source_col.clone())).eq(
                    plan.value_rhs_simple_expr_for_backend(
                        &m2m.source_col,
                        &m2m.source_id,
                        true,
                        backend,
                    ),
                ));
            }

            apply_relation_joins(&mut select, &join_plan)?;

            // WHERE columns are qualified by their relation-path alias (root leaf ->
            // root table, path leaf -> its JOIN alias). A path with no matching join
            // entry is a loud error, never a silently unqualified column.
            select.cond_where(query_condition_with_joins(
                &plan,
                backend,
                &table_name,
                &join_plan,
            )?);
            // ORDER BY terms are qualified the same way as WHERE leaves: an empty
            // path qualifies by the root table, a relation path by its JOIN alias
            // (#271). A path with no matching join entry is a loud error — the
            // Python builder always registers the join for an order_by path, so
            // this is defense-in-depth, never reachable in normal use.
            for order in &plan.order_by {
                let col = crate::query::qualify_column_with_joins(
                    &table_name,
                    &join_plan,
                    &order.column,
                    &order.path,
                )
                .map_err(pyo3::exceptions::PyValueError::new_err)?;
                let dir = if order.direction.to_lowercase() == "desc" {
                    Order::Desc
                } else {
                    Order::Asc
                };
                select.order_by_expr(col.into(), dir);
            }
            if let Some(limit) = plan.limit {
                select.limit(limit);
            }
            if let Some(offset) = plan.offset {
                select.offset(offset);
            }
            let (s, values) = sea_query_build_for_backend!(select, backend);
            (s, values, pk, schema.clone())
        };

        let engine_bind_values = engine_bind_values_from_sea(&bind_values.0);
        let rows = exec
            .fetch_all(&sql, &engine_bind_values)
            .await
            .map_err(|e| crate::errors::map_db_error("Fetch failed", e))?;
        let parsed_data = typed_rows_to_parsed_data(rows, &schema_for_decode, pk_col.as_deref());

        Python::attach(|py| {
            let results = pyo3::types::PyList::empty(py);
            let cls = cls_py.bind(py);

            let mut py_col_names = HashMap::new();
            if let Some(first_row) = parsed_data.first() {
                for (col_name, _) in &first_row.1 {
                    py_col_names.insert(
                        col_name.clone(),
                        pyo3::types::PyString::new(py, col_name).unbind(),
                    );
                }
            }
            let enum_classes = crate::hydration::enum_classes_for(py, cls);

            for (row_pk_val, fields) in parsed_data {
                if use_identity_map
                    && let Some(ref pk_val) = row_pk_val
                    && let Some(existing_obj) = identity_map_get(
                        py,
                        session_id.as_deref(),
                        &(connection_name.clone(), name.clone(), pk_val.clone()),
                    )?
                {
                    let existing = existing_obj.bind(py);
                    crate::hydration::refresh_model_instance(
                        py,
                        cls,
                        existing,
                        fields,
                        &py_col_names,
                        &enum_classes,
                    )?;
                    results.append(existing)?;
                    continue;
                }

                let instance = crate::hydration::hydrate_model_instance(
                    py,
                    cls,
                    &connection_name,
                    fields,
                    &py_col_names,
                    &enum_classes,
                )?;

                if use_identity_map && let Some(pk_val) = row_pk_val {
                    identity_map_insert(
                        py,
                        session_id.as_deref(),
                        (connection_name.clone(), name.clone(), pk_val),
                        &instance,
                    )?;
                }

                results.append(instance)?;
            }
            Ok(results.into_any().unbind())
        })
    })
}

/// Return the number of rows matching a filtered query.
///
/// Args:
///     name (str): Model class name.
///     query_ir_json (str): Serialized Query IR envelope JSON.
///     tx_id (str | None): Optional active transaction.
///     using (str | None): Connection override.
///     session_id (str | None): Session-scoped routing when set.
///
/// Returns:
///     int: Row count.
///
/// # Errors
/// `PyRuntimeError` on registry, planning, or SQL failures.
#[pyfunction]
#[pyo3(signature = (name, query_ir_json, route))]
pub fn count_filtered(
    py: Python<'_>,
    name: String,
    query_ir_json: String,
    route: Py<crate::state::RouteHandle>,
) -> PyResult<Bound<'_, PyAny>> {
    let mut plan = query_plan_from_ir_json(&query_ir_json)?;
    // count() is unaffected by projection (PRD #277 verb table): the Python
    // builder always emits root_instances on count payloads, projected or not.
    reject_unsupported_materialization(&plan, "count_filtered")?;

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let r = route.get();
        let (_, engine, tx_conn, backend) = route_engine(r)?;
        let exec = Executor::new(tx_conn.as_ref(), &engine);

        let schema = crate::state::registered_model(&name)?;
        let table_name = schema.table_name.clone();
        plan.postgres_enum_udt = postgres_table_catalog(&table_name, &engine, exec, backend)
            .await?
            .enum_udt
            .clone();

        // Relation-traversal JOINs render into the COUNT the same as the SELECT
        // (#270). Plain COUNT(*), no DISTINCT: forward-FK hops are many-to-one and
        // never multiply root rows, so count() equals the matching root-row count.
        // Build the plan up front and resolve each hop table's registration +
        // enum catalog before conditions bind (#270 critical).
        let m2m_join_table = plan.m2m.as_ref().map(|m2m| m2m.join_table.clone());
        let reserved: Vec<&str> = m2m_join_table.as_deref().into_iter().collect();
        let join_plan = query_join_plan(&plan, &table_name, &reserved)?;
        populate_hop_bind_context(&mut plan, &join_plan, &engine, exec, backend).await?;
        // ... sql ...
        let (sql, bind_values) = {
            let mut select = Query::select();
            select.expr(Expr::cust("COUNT(*)"));

            if let Some(m2m) = &plan.m2m {
                let join_table = Alias::new(&m2m.join_table);
                let source_col = Alias::new(&m2m.source_col);
                let target_col = Alias::new(&m2m.target_col);

                // We need the PK name of the target table to join
                let pk_name = schema
                    .meta
                    .pk_col
                    .clone()
                    .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("No primary key"))?;

                select.from(Alias::new(&table_name));
                select.inner_join(
                    join_table.clone(),
                    Expr::col((Alias::new(&table_name), Alias::new(pk_name)))
                        .equals((join_table.clone(), target_col.clone())),
                );
                select.and_where(Expr::col((join_table.clone(), source_col.clone())).eq(
                    plan.value_rhs_simple_expr_for_backend(
                        &m2m.source_col,
                        &m2m.source_id,
                        true,
                        backend,
                    ),
                ));
            } else {
                select.from(Alias::new(&table_name));
            }

            apply_relation_joins(&mut select, &join_plan)?;

            // WHERE columns qualified by their relation-path alias (see fetch_filtered).
            select.cond_where(query_condition_with_joins(
                &plan,
                backend,
                &table_name,
                &join_plan,
            )?);
            sea_query_build_for_backend!(select, backend)
        };

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

        Ok(count)
    })
}

/// Register a live Python instance in the identity map for deduplication.
///
/// Args:
///     connection_name (str): Connection the instance belongs to.
///     name (str): Model class name.
///     pk_val (str): Stringified primary key.
///     obj (PyAny): Model instance to cache.
///
/// Returns:
///     None
///
/// # Errors
/// `PyRuntimeError` when the identity map cannot be updated.
#[pyfunction]
#[pyo3(signature = (name, pk, obj, route))]
pub fn register_instance(
    py: Python<'_>,
    name: String,
    pk: String,
    obj: Py<PyAny>,
    route: Py<crate::state::RouteHandle>,
) -> PyResult<()> {
    let r = route.get();
    let (connection_name, engine, _, _) = route_engine(r)?;
    if engine.is_identity_map_enabled() {
        identity_map_insert(
            py,
            r.session_id.as_deref(),
            (connection_name, name, pk),
            obj.bind(py),
        )?;
    }
    Ok(())
}

/// Remove one instance from the identity map.
///
/// Args:
///     connection_name (str): Connection the instance was loaded on.
///     name (str): Model class name.
///     pk_val (str): Stringified primary key.
///
/// Returns:
///     None
#[pyfunction]
#[pyo3(signature = (name, pk, route))]
pub fn evict_instance(
    name: String,
    pk: String,
    route: Py<crate::state::RouteHandle>,
) -> PyResult<()> {
    let r = route.get();
    let (connection_name, engine, _, _) = route_engine(r)?;
    if engine.is_identity_map_enabled() {
        identity_map_remove(r.session_id.as_deref(), &(connection_name, name, pk))?;
    }
    Ok(())
}

/// Deletes a record by its primary key.
#[pyfunction]
#[pyo3(signature = (name, pk_val, route))]
pub fn delete_record(
    py: Python<'_>,
    name: String,
    pk_val: String,
    route: Py<crate::state::RouteHandle>,
) -> PyResult<Bound<'_, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let r = route.get();
        let (_, engine, tx_conn, backend) = route_engine(r)?;

        let schema = crate::state::registered_model(&name)?;
        let table_name = schema.table_name.clone();
        // ... sql ...
        let (sql, bind_values) = {
            let pk_name = schema
                .meta
                .pk_col
                .clone()
                .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("No primary key"))?;
            let no_enum_udt = HashMap::new();
            let no_uuid = HashSet::new();
            let no_ts: HashMap<String, String> = HashMap::new();
            let pk_expr = schema_value_expr(
                &schema,
                &table_name,
                &pk_name,
                &serde_json::Value::String(pk_val),
                &no_enum_udt,
                &no_uuid,
                &no_ts,
                backend,
            )?;
            let (s, values) = sea_query_build_for_backend!(
                Query::delete()
                    .from_table(Alias::new(&table_name))
                    .and_where(Expr::col(Alias::new(&pk_name)).eq(pk_expr)),
                backend
            );
            (s, values)
        };

        execute_statement_with_optional_tx(&engine, tx_conn.as_ref(), &sql, &bind_values.0)
            .await
            .map_err(|e| crate::errors::map_db_error("Delete failed", e))?;

        Ok(true)
    })
}

/// Delete rows matching a filtered query.
///
/// Args:
///     name (str): Model class name.
///     query_ir_json (str): Serialized Query IR envelope JSON.
///     tx_id (str | None): Optional active transaction.
///     using (str | None): Connection override.
///     session_id (str | None): Session-scoped routing when set.
///
/// Returns:
///     int: Rows deleted.
///
/// # Errors
/// `PyRuntimeError` on planning or execute failure.
#[pyfunction]
#[pyo3(signature = (name, query_ir_json, route))]
pub fn delete_filtered(
    py: Python<'_>,
    name: String,
    query_ir_json: String,
    route: Py<crate::state::RouteHandle>,
) -> PyResult<Bound<'_, PyAny>> {
    let mut plan = query_plan_from_ir_json(&query_ir_json)?;
    reject_pagination_on_mutation(&plan, "delete")?;
    reject_traversal_on_mutation(&plan, "delete")?;
    reject_unsupported_materialization(&plan, "delete")?;

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let r = route.get();
        let (_, engine, tx_conn, backend) = route_engine(r)?;
        let session_id = r.session_id.clone();
        let exec = Executor::new(tx_conn.as_ref(), &engine);

        let table_name = crate::state::registered_model(&name)?.table_name.clone();
        plan.postgres_enum_udt = postgres_table_catalog(&table_name, &engine, exec, backend)
            .await?
            .enum_udt
            .clone();
        // ... sql ...
        let (sql, bind_values) = {
            let mut delete = Query::delete();
            // Qualified by the root table alias (#269): both dialects accept
            // `table.column` in `DELETE ... WHERE`, and uniform qualification across
            // every walker avoids retrofitting the mutating paths when joins reach
            // them later.
            delete
                .from_table(Alias::new(&table_name))
                .cond_where(query_condition_for_backend(
                    &plan,
                    backend,
                    Some(&table_name),
                )?);
            sea_query_build_for_backend!(delete, backend)
        };

        let rows_affected =
            execute_statement_with_optional_tx(&engine, tx_conn.as_ref(), &sql, &bind_values.0)
                .await
                .map_err(|e| crate::errors::map_db_error("Delete failed", e))?;

        // After bulk delete, we MUST clear the Identity Map for this model to avoid stale objects
        // Re-acquire the GIL before accessing identity map (async context has no GIL).
        if engine.is_identity_map_enabled() {
            pyo3::Python::attach(|_py| {
                identity_map_retain_model(session_id.as_deref(), &name)
            })?;
        }

        Ok(rows_affected)
    })
}

/// Update rows matching a filtered query with column values from JSON.
///
/// Args:
///     name (str): Model class name.
///     query_ir_json (str): Serialized Query IR envelope JSON.
///     updates (dict): Per-column value map; ``bytes`` values are bound
///         directly (non-UTF-8 safe), all other values are routed through
///         the schema casting authority.
///     tx_id (str | None): Optional active transaction.
///     using (str | None): Connection override.
///     session_id (str | None): Session-scoped routing when set.
///
/// Returns:
///     int: Rows updated. Clears the identity map for the model when enabled.
///
/// # Errors
/// `PyTypeError` for unbindable column values; `PyRuntimeError` on execute failure.
#[pyfunction]
#[pyo3(signature = (name, query_ir_json, updates, route))]
pub fn update_filtered<'py>(
    py: Python<'py>,
    name: String,
    query_ir_json: String,
    updates: Bound<'py, pyo3::types::PyDict>,
    route: Py<crate::state::RouteHandle>,
) -> PyResult<Bound<'py, PyAny>> {
    let mut plan = query_plan_from_ir_json(&query_ir_json)?;
    reject_pagination_on_mutation(&plan, "update")?;
    reject_traversal_on_mutation(&plan, "update")?;
    reject_unsupported_materialization(&plan, "update")?;
    let update_inputs = bind_inputs_from_py(&updates)?;

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let r = route.get();
        let (_, engine, tx_conn, backend) = route_engine(r)?;
        let session_id = r.session_id.clone();
        let exec = Executor::new(tx_conn.as_ref(), &engine);

        let schema = crate::state::registered_model(&name)?;
        let table_name = schema.table_name.clone();
        let catalog = postgres_table_catalog(&table_name, &engine, exec, backend).await?;
        let TableCatalog { enum_udt, uuid_columns, ts_cast } = &*catalog;
        plan.postgres_enum_udt = enum_udt.clone();
        // ... sql ...
        let (sql, bind_values) = {
            // Qualified by the root table alias (#269) — same rationale as
            // `delete_filtered` above. SET columns stay bare: SQL forbids
            // qualified column names on the left of `SET` assignments.
            let mut update = UpdateStatement::new()
                .table(Alias::new(&table_name))
                .cond_where(query_condition_for_backend(
                    &plan,
                    backend,
                    Some(&table_name),
                )?)
                .to_owned();
            for (key, input) in &update_inputs {
                update.value(
                    Alias::new(key),
                    bind_input_to_expr(
                        &schema,
                        &table_name,
                        key,
                        input,
                        enum_udt,
                        uuid_columns,
                        ts_cast,
                        backend,
                    )?,
                );
            }
            sea_query_build_for_backend!(update, backend)
        };

        let rows_affected =
            execute_statement_with_optional_tx(&engine, tx_conn.as_ref(), &sql, &bind_values.0)
                .await
                .map_err(|e| crate::errors::map_db_error("Update failed", e))?;

        // After bulk update, we MUST clear the Identity Map for this model to avoid stale objects
        // Re-acquire the GIL before accessing identity map (async context has no GIL).
        if engine.is_identity_map_enabled() {
            pyo3::Python::attach(|_py| {
                identity_map_retain_model(session_id.as_deref(), &name)
            })?;
        }

        Ok(rows_affected)
    })
}

/// Insert many-to-many association rows into a join table.
///
/// Args:
///     join_table (str): Association table name.
///     source_col (str): FK column for the source model.
///     target_col (str): FK column for the target model.
///     source_id: Source row primary key.
///     target_ids: Target row primary keys to link.
///     tx_id (str | None): Optional active transaction.
///     using (str | None): Connection override.
///     session_id (str | None): Session-scoped routing when set.
///
/// Returns:
///     None
///
/// # Errors
/// `PyValueError` when an id is `None` or INSERT values are invalid; `PyRuntimeError` on execute failure.
#[pyfunction]
#[pyo3(signature = (join_table, source_col, target_col, source_id, target_ids, route))]
#[allow(clippy::too_many_arguments)]
pub fn add_m2m_links<'py>(
    py: Python<'py>,
    join_table: String,
    source_col: String,
    target_col: String,
    source_id: Bound<'py, PyAny>,
    target_ids: Vec<Bound<'py, PyAny>>,
    route: Py<crate::state::RouteHandle>,
) -> PyResult<Bound<'py, PyAny>> {
    let s_id = python_to_sea_value(source_id)?;
    let t_ids: Vec<sea_query::Value> = target_ids
        .into_iter()
        .map(|id| python_to_sea_value(id))
        .collect::<PyResult<Vec<_>>>()?;

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let r = route.get();
        let (_, engine, tx_conn, backend) = route_engine(r)?;
        let exec = Executor::new(tx_conn.as_ref(), &engine);
        let catalog = postgres_table_catalog(&join_table, &engine, exec, backend).await?;
        let uuid_columns = &catalog.uuid_columns;

        let (sql, bind_values) = {
            let mut insert = InsertStatement::new()
                .into_table(Alias::new(&join_table))
                .columns(vec![Alias::new(&source_col), Alias::new(&target_col)])
                .to_owned();

            for t_id in t_ids {
                insert
                    .values(vec![
                        backend_column_value_expr(
                            &source_col,
                            s_id.clone(),
                            uuid_columns,
                            backend,
                        ),
                        backend_column_value_expr(&target_col, t_id, uuid_columns, backend),
                    ])
                    .map_err(|e| {
                        pyo3::exceptions::PyValueError::new_err(format!(
                            "invalid M2M INSERT values: {e}"
                        ))
                    })?;
            }
            sea_query_build_for_backend!(insert, backend)
        };

        execute_statement_with_optional_tx(&engine, tx_conn.as_ref(), &sql, &bind_values.0)
            .await
            .map_err(|e| crate::errors::map_db_error("Add M2M links failed", e))?;

        Ok(())
    })
}

/// Delete specific many-to-many association rows from a join table.
///
/// Args:
///     join_table (str): Association table name.
///     source_col (str): FK column for the source model.
///     target_col (str): FK column for the target model.
///     source_id: Source row primary key.
///     target_ids: Target ids to unlink (must match existing rows).
///     tx_id (str | None): Optional active transaction.
///     using (str | None): Connection override.
///     session_id (str | None): Session-scoped routing when set.
///
/// Returns:
///     None
///
/// # Errors
/// `PyRuntimeError` on execute failure.
#[pyfunction]
#[pyo3(signature = (join_table, source_col, target_col, source_id, target_ids, route))]
#[allow(clippy::too_many_arguments)]
pub fn remove_m2m_links<'py>(
    py: Python<'py>,
    join_table: String,
    source_col: String,
    target_col: String,
    source_id: Bound<'py, PyAny>,
    target_ids: Vec<Bound<'py, PyAny>>,
    route: Py<crate::state::RouteHandle>,
) -> PyResult<Bound<'py, PyAny>> {
    let s_id = python_to_sea_value(source_id)?;
    let t_ids: Vec<sea_query::Value> = target_ids
        .into_iter()
        .map(|id| python_to_sea_value(id))
        .collect::<PyResult<Vec<_>>>()?;

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let r = route.get();
        let (_, engine, tx_conn, backend) = route_engine(r)?;
        let exec = Executor::new(tx_conn.as_ref(), &engine);
        let catalog = postgres_table_catalog(&join_table, &engine, exec, backend).await?;
        let uuid_columns = &catalog.uuid_columns;

        let (sql, bind_values) = sea_query_build_for_backend!(
            Query::delete()
                .from_table(Alias::new(&join_table))
                .and_where(
                    Expr::col(Alias::new(&source_col)).eq(backend_column_value_expr(
                        &source_col,
                        s_id,
                        uuid_columns,
                        backend
                    ))
                )
                .and_where(
                    Expr::col(Alias::new(&target_col)).is_in(
                        t_ids
                            .into_iter()
                            .map(|t_id| {
                                backend_column_value_expr(&target_col, t_id, uuid_columns, backend)
                            })
                            .collect::<Vec<_>>()
                    )
                ),
            backend
        );

        execute_statement_with_optional_tx(&engine, tx_conn.as_ref(), &sql, &bind_values.0)
            .await
            .map_err(|e| crate::errors::map_db_error("Remove M2M links failed", e))?;

        Ok(())
    })
}

/// Delete all many-to-many links for a source row from a join table.
///
/// Args:
///     join_table (str): Association table name.
///     source_col (str): FK column for the source model.
///     source_id: Source row primary key.
///     tx_id (str | None): Optional active transaction.
///     using (str | None): Connection override.
///     session_id (str | None): Session-scoped routing when set.
///
/// Returns:
///     None
///
/// # Errors
/// `PyRuntimeError` on execute failure.
#[pyfunction]
#[pyo3(signature = (join_table, source_col, source_id, route))]
pub fn clear_m2m_links<'py>(
    py: Python<'py>,
    join_table: String,
    source_col: String,
    source_id: Bound<'py, PyAny>,
    route: Py<crate::state::RouteHandle>,
) -> PyResult<Bound<'py, PyAny>> {
    let s_id = python_to_sea_value(source_id)?;

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let r = route.get();
        let (_, engine, tx_conn, backend) = route_engine(r)?;
        let exec = Executor::new(tx_conn.as_ref(), &engine);
        let catalog = postgres_table_catalog(&join_table, &engine, exec, backend).await?;
        let uuid_columns = &catalog.uuid_columns;

        let (sql, bind_values) = sea_query_build_for_backend!(
            Query::delete()
                .from_table(Alias::new(&join_table))
                .and_where(
                    Expr::col(Alias::new(&source_col)).eq(backend_column_value_expr(
                        &source_col,
                        s_id,
                        uuid_columns,
                        backend
                    ))
                ),
            backend
        );

        execute_statement_with_optional_tx(&engine, tx_conn.as_ref(), &sql, &bind_values.0)
            .await
            .map_err(|e| crate::errors::map_db_error("Clear M2M links failed", e))?;

        Ok(())
    })
}

/// Convert a Python value into a SeaQuery `Value` for M2M source / target IDs.
///
/// M2M IDs cannot be `None`: a NULL target id has no meaningful join semantics
/// and was previously routed to `String(None)`, which (a) reproduces the #38
/// text-typed-null bug for non-UUID FKs on Postgres and (b) silently no-ops
/// on insert. Reject up front with a `PyValueError`.
///
/// UUID-string detection happens downstream in `backend_column_value_expr`,
/// which has the column-context (`uuid_columns`) needed to convert to a typed
/// `Value::Uuid(Some(_))` for UUID FK columns.
fn python_to_sea_value(val: Bound<'_, PyAny>) -> PyResult<sea_query::Value> {
    if val.is_none() {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "M2M source/target ID cannot be None",
        ));
    }
    // Order matters: in Python, `bool` is a subtype of `int`. Check bool
    // before i64 or True/False round-trip as 1/0.
    if let Ok(b) = val.extract::<bool>() {
        Ok(sea_query::Value::Bool(Some(b)))
    } else if let Ok(i) = val.extract::<i64>() {
        Ok(sea_query::Value::BigInt(Some(i)))
    } else if let Ok(f) = val.extract::<f64>() {
        Ok(sea_query::Value::Double(Some(f)))
    } else if let Ok(s) = val.extract::<String>() {
        Ok(sea_query::Value::String(Some(Box::new(s))))
    } else {
        // Fallback to string representation for other types
        Ok(sea_query::Value::String(Some(Box::new(val.to_string()))))
    }
}

/// A per-column bind value for the schema-guided write path: either a
/// JSON-canonicalized value (routed through the unchanged `schema_bind_expr`
/// casting authority) or raw bytes (bound directly, so non-UTF-8 binary
/// survives).
#[derive(Debug)]
enum BindInput {
    Json(serde_json::Value),
    Bytes(Vec<u8>),
}

impl BindInput {
    fn is_json_null(&self) -> bool {
        matches!(self, BindInput::Json(serde_json::Value::Null))
    }
}

/// Convert a JSON-safe Python value (scalar or nested container) into a
/// `serde_json::Value`. `bytes` are handled by `bind_input_from_py` *before*
/// this is called; anything else that is not JSON-representable is rejected
/// with a clear `TypeError` (the Ferro-level error floor — AGENTS.md I-3, no
/// `unwrap`/`expect`).
fn py_to_json_value(value: &Bound<'_, PyAny>, col: &str) -> PyResult<serde_json::Value> {
    use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString};

    if value.is_none() {
        return Ok(serde_json::Value::Null);
    }
    // bool BEFORE int: in Python bool is a subtype of int.
    if let Ok(b) = value.downcast::<PyBool>() {
        return Ok(serde_json::Value::Bool(b.is_true()));
    }
    if let Ok(i) = value.downcast::<PyInt>() {
        if let Ok(n) = i.extract::<i64>() {
            return Ok(serde_json::Value::from(n));
        }
        if let Ok(n) = i.extract::<u64>() {
            return Ok(serde_json::Value::from(n));
        }
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "Integer out of range for column '{col}'"
        )));
    }
    if let Ok(f) = value.downcast::<PyFloat>() {
        let n = f.extract::<f64>()?;
        return serde_json::Number::from_f64(n)
            .map(serde_json::Value::Number)
            .ok_or_else(|| {
                pyo3::exceptions::PyValueError::new_err(format!(
                    "Non-finite float (NaN/Inf) for column '{col}'"
                ))
            });
    }
    if let Ok(s) = value.downcast::<PyString>() {
        return Ok(serde_json::Value::String(s.extract::<String>()?));
    }
    if let Ok(list) = value.downcast::<PyList>() {
        let mut arr = Vec::with_capacity(list.len());
        for item in list.iter() {
            arr.push(py_to_json_value(&item, col)?);
        }
        return Ok(serde_json::Value::Array(arr));
    }
    if let Ok(dict) = value.downcast::<PyDict>() {
        let mut map = serde_json::Map::new();
        for (k, v) in dict.iter() {
            let key: String = k.extract().map_err(|_| {
                pyo3::exceptions::PyTypeError::new_err(format!(
                    "Non-string key in JSON value for column '{col}'"
                ))
            })?;
            map.insert(key, py_to_json_value(&v, col)?);
        }
        return Ok(serde_json::Value::Object(map));
    }
    Err(pyo3::exceptions::PyTypeError::new_err(format!(
        "Cannot bind value {} for column '{col}' (unsupported type). \
         Supported: str, int, float, bool, bytes, None, and JSON-safe dict/list.",
        value.repr()?
    )))
}

/// Marshal a single Python column value into a `BindInput`.
/// `bytes`/`bytearray` are preserved verbatim; everything else is JSON-canonicalized.
fn bind_input_from_py(value: &Bound<'_, PyAny>, col: &str) -> PyResult<BindInput> {
    use pyo3::types::{PyByteArray, PyBytes};

    if let Ok(b) = value.downcast::<PyBytes>() {
        return Ok(BindInput::Bytes(b.as_bytes().to_vec()));
    }
    if let Ok(b) = value.downcast::<PyByteArray>() {
        return Ok(BindInput::Bytes(b.to_vec()));
    }
    Ok(BindInput::Json(py_to_json_value(value, col)?))
}

/// Extract a Python row dict into an ordered column→`BindInput` list.
fn bind_inputs_from_py(map: &Bound<'_, pyo3::types::PyDict>) -> PyResult<Vec<(String, BindInput)>> {
    let mut out = Vec::with_capacity(map.len());
    for (k, v) in map.iter() {
        let key: String = k.extract().map_err(|_| {
            pyo3::exceptions::PyTypeError::new_err("Record keys must be strings")
        })?;
        let input = bind_input_from_py(&v, &key)?;
        out.push((key, input));
    }
    Ok(out)
}

/// Route a `BindInput` to a SeaQuery expression: raw bytes bind directly to
/// `SeaValue::Bytes`; everything else flows through the unchanged casting
/// authority (`schema_bind_expr`).
#[allow(clippy::too_many_arguments)]
fn bind_input_to_expr(
    model: &crate::state::RegisteredModel,
    table_name: &str,
    col_name: &str,
    input: &BindInput,
    enum_udt: &HashMap<String, String>,
    uuid_columns: &HashSet<String>,
    ts_cast: &HashMap<String, String>,
    backend: Dialect,
) -> PyResult<SimpleExpr> {
    match input {
        BindInput::Bytes(b) => Ok(Expr::value(SeaValue::Bytes(Some(Box::new(b.clone()))))),
        BindInput::Json(v) => crate::codec::schema_bind_expr(
            &model.codec_plan, table_name, col_name, v, enum_udt, uuid_columns, ts_cast, backend,
        ),
    }
}

/// Convert a Python primitive into an [`EngineBindValue`] for raw SQL bind parameters.
///
/// The Python wrapper (`src/ferro/raw.py:_marshal`) is responsible for richer
/// types (UUID, datetime, Decimal, Enum, dict, list); this helper accepts only the
/// primitive set and raises `TypeError` for anything else as a defensive guard.
///
/// Order matters: in Python, `bool` is a subtype of `int`, so we must check `bool`
/// before `i64` or `True`/`False` would round-trip as `1`/`0`.
///
/// **Raw-SQL boundary.** This is the documented exception to Ferro's typed-null
/// architectural rule (R3). The raw-SQL bind path has no schema or column-type
/// context -- the user supplies pre-built SQL text and bare Python values --
/// so Python `None` becomes [`NullKind::Untyped`]. Schema-driven emitters
/// (INSERT/UPDATE values, query-filter predicates, M2M target IDs) infer the
/// kind from column metadata and emit a typed null. See
/// `docs/solutions/patterns/typed-null-binds.md`.
///
/// [`NullKind::Untyped`]: crate::backend::NullKind::Untyped
fn python_to_engine_bind_value(
    val: &Bound<'_, PyAny>,
) -> PyResult<crate::backend::EngineBindValue> {
    use crate::backend::EngineBindValue;

    if val.is_none() {
        return Ok(EngineBindValue::Null(crate::backend::NullKind::Untyped));
    }
    if let Ok(b) = val.extract::<bool>() {
        return Ok(EngineBindValue::Bool(b));
    }
    if let Ok(i) = val.extract::<i64>() {
        return Ok(EngineBindValue::I64(i));
    }
    if let Ok(f) = val.extract::<f64>() {
        return Ok(EngineBindValue::F64(f));
    }
    if let Ok(s) = val.extract::<String>() {
        return Ok(EngineBindValue::String(s));
    }
    if let Ok(b) = val.extract::<Vec<u8>>() {
        return Ok(EngineBindValue::Bytes(b));
    }
    Err(pyo3::exceptions::PyTypeError::new_err(format!(
        "Unsupported raw SQL bind value: {}",
        val.repr()?
    )))
}

/// Look up a transaction connection by id, returning a sharper error than the
/// CRUD path's "Transaction not found" — this surface is reachable by users
/// who hold a `Transaction` handle past the end of `async with transaction():`.
fn get_raw_tx_conn(
    tx_id: Option<String>,
    session_id: Option<&str>,
) -> PyResult<Option<TransactionConnection>> {
    match tx_id {
        Some(id) => {
            let conn = tx_get(session_id, &id)?
                .map(|tx| tx.conn)
                .ok_or_else(|| {
                    pyo3::exceptions::PyRuntimeError::new_err(
                        "Transaction has already been closed or never existed",
                    )
                })?;
            Ok(Some(conn))
        }
        None => Ok(None),
    }
}

/// Run a raw SQL statement and return rows_affected as an int.
///
/// Honors `tx_id` (looked up in [`TRANSACTION_REGISTRY`]); falls back to a
/// one-off pool connection when `tx_id` is `None`.
///
/// # Errors
/// - [`PyRuntimeError`](pyo3::exceptions::PyRuntimeError) on engine/transaction
///   lookup failure or DB error.
/// - [`PyTypeError`](pyo3::exceptions::PyTypeError) if any element of `args` is
///   not a supported primitive (the Python `_marshal` wrapper guarantees this
///   never trips in normal use).
#[pyfunction]
#[pyo3(signature = (sql, args, route))]
pub fn raw_execute<'py>(
    py: Python<'py>,
    sql: String,
    args: Vec<Bound<'py, PyAny>>,
    route: Py<crate::state::RouteHandle>,
) -> PyResult<Bound<'py, PyAny>> {
    let bind_values: Vec<EngineBindValue> = args
        .iter()
        .map(python_to_engine_bind_value)
        .collect::<PyResult<_>>()?;
    // Clone the route's fields before the future_into_py move (FF-D D3):
    // `Py<RouteHandle>` itself is Send/Sync and could be moved in directly,
    // but `get_raw_tx_conn` must run synchronously, before the async block,
    // to preserve its sharper "transaction already closed" error surface.
    let (route_connection_name, route_tx_id, route_session_id) = {
        let r = route.get();
        (r.connection_name.clone(), r.tx_id.clone(), r.session_id.clone())
    };
    let tx_conn = get_raw_tx_conn(route_tx_id, route_session_id.as_deref())?;

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
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

        Ok(rows_affected as i64)
    })
}

/// Run a raw SQL query and return all rows as a list of dicts.
///
/// Values are wire-close primitives (`str | int | float | bool | bytes | None`).
/// UUID/datetime/JSON columns come back as strings — Ferro does not decode them
/// for raw SQL. If you want typed rows, use the ORM.
#[pyfunction]
#[pyo3(signature = (sql, args, route))]
pub fn raw_fetch_all<'py>(
    py: Python<'py>,
    sql: String,
    args: Vec<Bound<'py, PyAny>>,
    route: Py<crate::state::RouteHandle>,
) -> PyResult<Bound<'py, PyAny>> {
    let bind_values: Vec<EngineBindValue> = args
        .iter()
        .map(python_to_engine_bind_value)
        .collect::<PyResult<_>>()?;
    let (route_connection_name, route_tx_id, route_session_id) = {
        let r = route.get();
        (r.connection_name.clone(), r.tx_id.clone(), r.session_id.clone())
    };
    let tx_conn = get_raw_tx_conn(route_tx_id, route_session_id.as_deref())?;

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let pool_engine;
        let exec = match &tx_conn {
            Some(tx) => Executor::Tx(tx),
            None => {
                pool_engine = engine_for_connection(Some(route_connection_name))?;
                Executor::Pool(&pool_engine)
            }
        };
        let rows = exec
            .fetch_all(&sql, &bind_values)
            .await
            .map_err(|e| crate::errors::map_db_error("Raw SQL fetch_all failed", e))?;

        Python::attach(|py| {
            let out = pyo3::types::PyList::empty(py);
            for row in rows {
                out.append(engine_row_to_pydict(py, row)?)?;
            }
            Ok(out.into_any().unbind())
        })
    })
}

/// Run a raw SQL query and return the first row as a dict, or `None`.
///
/// Args:
///     sql (str): Parameterized SQL (`?` / `$n` placeholders).
///     args: Bind parameters (marshalled to typed engine binds).
///     tx_id (str | None): Optional active transaction.
///     using (str | None): Connection override.
///     session_id (str | None): Session-scoped routing when set.
///
/// Returns:
///     dict[str, Any] | None: First row as wire-close primitives, or `None` when no rows.
///
/// Values are wire-close primitives (`str | int | float | bool | bytes | None`).
/// UUID/datetime/JSON columns come back as strings — use the ORM for typed hydration.
#[pyfunction]
#[pyo3(signature = (sql, args, route))]
pub fn raw_fetch_one<'py>(
    py: Python<'py>,
    sql: String,
    args: Vec<Bound<'py, PyAny>>,
    route: Py<crate::state::RouteHandle>,
) -> PyResult<Bound<'py, PyAny>> {
    let bind_values: Vec<EngineBindValue> = args
        .iter()
        .map(python_to_engine_bind_value)
        .collect::<PyResult<_>>()?;
    let (route_connection_name, route_tx_id, route_session_id) = {
        let r = route.get();
        (r.connection_name.clone(), r.tx_id.clone(), r.session_id.clone())
    };
    let tx_conn = get_raw_tx_conn(route_tx_id, route_session_id.as_deref())?;

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let pool_engine;
        let exec = match &tx_conn {
            Some(tx) => Executor::Tx(tx),
            None => {
                pool_engine = engine_for_connection(Some(route_connection_name))?;
                Executor::Pool(&pool_engine)
            }
        };
        let rows = exec
            .fetch_all(&sql, &bind_values)
            .await
            .map_err(|e| crate::errors::map_db_error("Raw SQL fetch_one failed", e))?;

        Python::attach(|py| match rows.into_iter().next() {
            Some(row) => Ok(engine_row_to_pydict(py, row)?.into_any().unbind()),
            None => Ok(py.None()),
        })
    })
}

/// Lifetime catalog-introspection query count for a connection's engine.
///
/// Test-only instrument for the FF-C C2 exit gate: steady-state CRUD must
/// issue zero catalog queries. Counts every execution in
/// `postgres_catalog_rows` — the single choke point for catalog SQL.
///
/// Args:
///     using (str | None): Connection override; defaults to the active default.
///
/// Returns:
///     int: Catalog queries issued by the engine since connect.
///
/// # Errors
/// `PyRuntimeError` if no engine is connected.
#[pyfunction]
#[pyo3(name = "_catalog_query_count_for_test")]
#[pyo3(signature = (using=None))]
pub fn _catalog_query_count_for_test(using: Option<String>) -> PyResult<u64> {
    let (_, engine) = active_connection_for_route(using)?;
    Ok(engine.catalog_query_count())
}

#[cfg(test)]
mod m2m_value_tests {
    use super::{
        backend_column_value_expr, bind_input_from_py, bind_input_to_expr, py_to_json_value,
        python_to_sea_value, BindInput,
    };
    use crate::state::Dialect;
    use pyo3::Python;
    use sea_query::{Alias, PostgresQueryBuilder, Query, SqliteQueryBuilder, Value as SeaValue};
    use std::collections::{HashMap, HashSet};

    fn extract_pg_value(expr: sea_query::SimpleExpr) -> SeaValue {
        let (_, values) = Query::insert()
            .into_table(Alias::new("t"))
            .columns([Alias::new("c")])
            .values_panic([expr])
            .build(PostgresQueryBuilder);
        values.0.into_iter().next().expect("one value")
    }

    #[test]
    fn python_to_sea_value_rejects_none() {
        Python::attach(|py| {
            let none = py.None().into_bound(py);
            let err = python_to_sea_value(none).expect_err("None must be rejected");
            let msg = err.to_string();
            assert!(
                msg.contains("M2M") && msg.contains("None"),
                "unexpected error message: {msg}"
            );
        });
    }

    #[test]
    fn python_to_sea_value_routes_bool_before_int() {
        // Regression guard: in Python bool is subtype of int. We must extract
        // bool first or True/False round-trip as 1/0.
        Python::attach(|py| {
            use pyo3::types::PyBool;

            let py_true = PyBool::new(py, true).to_owned().into_any();
            let v = python_to_sea_value(py_true).unwrap();
            assert!(matches!(v, SeaValue::Bool(Some(true))));
        });
    }

    #[test]
    fn backend_column_value_expr_emits_typed_uuid_on_postgres_no_cast() {
        let mut uuid_cols = HashSet::new();
        uuid_cols.insert("user_id".to_string());

        let uuid_str = "550e8400-e29b-41d4-a716-446655440000";
        let value = SeaValue::String(Some(Box::new(uuid_str.to_string())));
        let expr = backend_column_value_expr("user_id", value, &uuid_cols, Dialect::Postgres);

        let sql = Query::select()
            .expr(expr.clone())
            .to_string(PostgresQueryBuilder);
        assert!(
            !sql.contains("AS uuid"),
            "M2M typed UUID bind should not CAST: {sql}"
        );
        match extract_pg_value(expr) {
            SeaValue::Uuid(Some(u)) => assert_eq!(u.to_string(), uuid_str),
            other => panic!("expected typed Uuid bind, got {other:?}"),
        }
    }

    #[test]
    fn backend_column_value_expr_passthrough_for_non_uuid_column() {
        let uuid_cols = HashSet::new();
        let expr = backend_column_value_expr(
            "team_id",
            SeaValue::BigInt(Some(42)),
            &uuid_cols,
            Dialect::Postgres,
        );
        let sql = Query::select().expr(expr).to_string(PostgresQueryBuilder);
        // No CAST for plain integer FK
        assert!(!sql.contains("CAST"), "non-UUID FK should not CAST: {sql}");
    }

    #[test]
    fn backend_column_value_expr_passthrough_on_sqlite() {
        let mut uuid_cols = HashSet::new();
        uuid_cols.insert("user_id".to_string());

        let uuid_str = "550e8400-e29b-41d4-a716-446655440000";
        let value = SeaValue::String(Some(Box::new(uuid_str.to_string())));
        let expr = backend_column_value_expr("user_id", value, &uuid_cols, Dialect::Sqlite);

        let sql = Query::select().expr(expr).to_string(SqliteQueryBuilder);
        assert!(!sql.contains("AS uuid"), "SQLite must not CAST: {sql}");
    }

    #[test]
    fn backend_column_value_expr_falls_back_to_text_cast_on_unparseable_uuid() {
        // Defensive path: if a non-UUID-shaped string lands on a UUID FK
        // column, surface the error from Postgres rather than silently
        // emitting a typed Uuid bind that would fail with a less obvious
        // diagnostic.
        let mut uuid_cols = HashSet::new();
        uuid_cols.insert("user_id".to_string());

        let value = SeaValue::String(Some(Box::new("not-a-uuid".to_string())));
        let expr = backend_column_value_expr("user_id", value, &uuid_cols, Dialect::Postgres);
        let sql = Query::select().expr(expr).to_string(PostgresQueryBuilder);
        assert!(
            sql.contains("AS uuid"),
            "fallback CAST expected for unparseable UUID: {sql}"
        );
    }

    #[test]
    fn py_to_json_value_maps_json_scalars_and_containers() {
        Python::attach(|py| {
            use pyo3::types::{PyDict, PyDictMethods, PyList};
            use pyo3::IntoPyObject;

            let i = 42i64.into_pyobject(py).unwrap().into_any();
            assert_eq!(py_to_json_value(&i, "c").unwrap(), serde_json::json!(42));

            let b = pyo3::types::PyBool::new(py, true).to_owned().into_any();
            assert_eq!(py_to_json_value(&b, "c").unwrap(), serde_json::json!(true));

            let s = "hi".into_pyobject(py).unwrap().into_any();
            assert_eq!(py_to_json_value(&s, "c").unwrap(), serde_json::json!("hi"));

            let list = PyList::new(py, [1i64, 2, 3]).unwrap().into_any();
            assert_eq!(py_to_json_value(&list, "c").unwrap(), serde_json::json!([1, 2, 3]));

            let dict = PyDict::new(py);
            dict.set_item("w", 10i64).unwrap();
            let d = dict.into_any();
            assert_eq!(py_to_json_value(&d, "c").unwrap(), serde_json::json!({"w": 10}));
        });
    }

    #[test]
    fn py_to_json_value_rejects_unsupported_type() {
        Python::attach(|py| {
            let set = pyo3::types::PySet::empty(py).unwrap().into_any();
            let err = py_to_json_value(&set, "mycol").expect_err("set must be rejected");
            assert!(err.is_instance_of::<pyo3::exceptions::PyTypeError>(py));
            assert!(err.to_string().contains("mycol"), "msg: {}", err);
        });
    }

    #[test]
    fn bind_input_from_py_preserves_non_utf8_bytes() {
        Python::attach(|py| {
            let raw = vec![0x89u8, 0x00, 0xff, 0xfe];
            let obj = pyo3::types::PyBytes::new(py, &raw).into_any();
            match bind_input_from_py(&obj, "data").unwrap() {
                BindInput::Bytes(b) => assert_eq!(b, raw),
                other => panic!("expected Bytes, got {other:?}"),
            }
        });
    }

    #[test]
    fn bind_input_to_expr_binds_bytes_to_sea_bytes() {
        use sea_query::{Alias, Query, SqliteQueryBuilder};
        let schema = serde_json::json!({
            "properties": { "data": { "type": "string", "format": "binary" } }
        });
        let input = BindInput::Bytes(vec![0x89, 0x00, 0xff]);
        let model = crate::state::RegisteredModel::new_for_test(schema, "test_table".to_string());
        let expr = bind_input_to_expr(
            &model, "doc", "data", &input,
            &HashMap::new(), &HashSet::new(), &HashMap::new(), Dialect::Sqlite,
        ).unwrap();
        let (_, values) = Query::insert()
            .into_table(Alias::new("doc"))
            .columns([Alias::new("data")])
            .values_panic([expr])
            .build(SqliteQueryBuilder);
        match values.0.into_iter().next().unwrap() {
            SeaValue::Bytes(Some(b)) => assert_eq!(*b, vec![0x89, 0x00, 0xff]),
            other => panic!("expected SeaValue::Bytes, got {other:?}"),
        }
    }
}

#[cfg(test)]
mod schema_value_expr_tests {
    use super::schema_value_expr;
    use crate::state::Dialect;
    use sea_query::{Alias, PostgresQueryBuilder, Query, Value as SeaValue};
    use std::collections::{HashMap, HashSet};

    fn schema_for(col: &str, prop: serde_json::Value) -> serde_json::Value {
        serde_json::json!({
            "properties": { col: prop }
        })
    }

    fn build_pg_value(
        schema: &serde_json::Value,
        table: &str,
        col: &str,
        value: &serde_json::Value,
        uuid_columns: HashSet<String>,
    ) -> pyo3::PyResult<(String, sea_query::Values)> {
        let enum_udt = HashMap::new();
        let ts_cast = HashMap::new();
        let model = crate::state::RegisteredModel::new_for_test(schema.clone(), "test_table".to_string());
        let expr = schema_value_expr(
            &model,
            table,
            col,
            value,
            &enum_udt,
            &uuid_columns,
            &ts_cast,
            Dialect::Postgres,
        )?;
        let (sql, values) = Query::insert()
            .into_table(Alias::new(table))
            .columns([Alias::new(col)])
            .values_panic([expr])
            .build(PostgresQueryBuilder);
        Ok((sql, values))
    }

    fn nullable(inner: serde_json::Value) -> serde_json::Value {
        let mut anyof = inner;
        if let serde_json::Value::Object(_) = &anyof {
            anyof = serde_json::json!({"anyOf": [anyof, {"type": "null"}]});
        }
        anyof
    }

    #[test]
    fn emits_typed_null_for_int_column() {
        let schema = schema_for("n", nullable(serde_json::json!({"type": "integer"})));
        let (sql, values) = build_pg_value(
            &schema,
            "thing",
            "n",
            &serde_json::Value::Null,
            HashSet::new(),
        )
        .unwrap();

        assert!(matches!(values.0.as_slice(), [SeaValue::BigInt(None)]));
        assert!(!sql.contains("CAST"));
    }

    #[test]
    fn emits_typed_null_for_bool_column() {
        let schema = schema_for("flag", nullable(serde_json::json!({"type": "boolean"})));
        let (_, values) = build_pg_value(
            &schema,
            "thing",
            "flag",
            &serde_json::Value::Null,
            HashSet::new(),
        )
        .unwrap();

        assert!(matches!(values.0.as_slice(), [SeaValue::Bool(None)]));
    }

    #[test]
    fn emits_typed_null_for_float_column() {
        let schema = schema_for("ratio", nullable(serde_json::json!({"type": "number"})));
        let (_, values) = build_pg_value(
            &schema,
            "thing",
            "ratio",
            &serde_json::Value::Null,
            HashSet::new(),
        )
        .unwrap();

        assert!(matches!(values.0.as_slice(), [SeaValue::Double(None)]));
    }

    #[test]
    fn emits_typed_null_for_str_column() {
        let schema = schema_for("name", nullable(serde_json::json!({"type": "string"})));
        let (_, values) = build_pg_value(
            &schema,
            "thing",
            "name",
            &serde_json::Value::Null,
            HashSet::new(),
        )
        .unwrap();

        assert!(matches!(values.0.as_slice(), [SeaValue::String(None)]));
    }

    #[test]
    fn emits_typed_null_for_bytes_column() {
        let schema = schema_for(
            "blob",
            nullable(serde_json::json!({"type": "string", "format": "binary"})),
        );
        let (_, values) = build_pg_value(
            &schema,
            "thing",
            "blob",
            &serde_json::Value::Null,
            HashSet::new(),
        )
        .unwrap();

        assert!(matches!(values.0.as_slice(), [SeaValue::Bytes(None)]));
    }

    #[test]
    fn emits_typed_null_for_decimal_column() {
        // Decimal columns are recognized by the enriched `format: "decimal"`
        // (what registration always emits for Decimal annotations) — never by
        // sniffing the string pattern (F5).
        let schema = serde_json::json!({
            "properties": {
                "amount": {
                    "anyOf": [
                        {"type": "number"},
                        {"type": "string", "pattern": "^-?\\d+(\\.\\d+)?$"},
                        {"type": "null"}
                    ],
                    "format": "decimal"
                }
            }
        });
        let (sql, values) = build_pg_value(
            &schema,
            "thing",
            "amount",
            &serde_json::Value::Null,
            HashSet::new(),
        )
        .unwrap();

        assert!(matches!(values.0.as_slice(), [SeaValue::String(None)]));
        assert!(
            sql.to_ascii_lowercase().contains("numeric"),
            "expected numeric cast for decimal NULL, got {sql}"
        );
    }

    #[test]
    fn emits_typed_null_for_uuid_column_via_format() {
        let schema = schema_for(
            "id",
            nullable(serde_json::json!({"type": "string", "format": "uuid"})),
        );
        let (sql, values) = build_pg_value(
            &schema,
            "thing",
            "id",
            &serde_json::Value::Null,
            HashSet::new(),
        )
        .unwrap();

        assert!(
            matches!(values.0.as_slice(), [SeaValue::Uuid(None)]),
            "expected Uuid(None), got {:?}",
            values.0
        );
        assert!(
            !sql.contains("CAST"),
            "UUID null should no longer rely on CAST: {sql}"
        );
    }

    #[test]
    fn emits_typed_null_for_uuid_column_via_introspection_set() {
        let schema = schema_for("id", serde_json::json!({"type": "string"}));
        let mut uuid_cols = HashSet::new();
        uuid_cols.insert("id".to_string());

        let (sql, values) =
            build_pg_value(&schema, "thing", "id", &serde_json::Value::Null, uuid_cols).unwrap();

        assert!(matches!(values.0.as_slice(), [SeaValue::Uuid(None)]));
        assert!(!sql.contains("CAST"), "UUID null should not CAST: {sql}");
    }

    #[test]
    fn emits_typed_uuid_value_via_format() {
        let schema = schema_for(
            "id",
            nullable(serde_json::json!({"type": "string", "format": "uuid"})),
        );
        let uuid_str = "550e8400-e29b-41d4-a716-446655440000";
        let (sql, values) = build_pg_value(
            &schema,
            "thing",
            "id",
            &serde_json::Value::String(uuid_str.to_string()),
            HashSet::new(),
        )
        .unwrap();

        match values.0.as_slice() {
            [SeaValue::Uuid(Some(u))] => {
                assert_eq!(u.to_string(), uuid_str);
            }
            other => panic!("expected Uuid(Some(_)), got {other:?}"),
        }
        assert!(
            !sql.contains("CAST"),
            "UUID value should no longer rely on CAST: {sql}"
        );
    }

    #[test]
    fn rejects_invalid_uuid_with_pyvalueerror() {
        pyo3::Python::attach(|py| {
            let schema = schema_for(
                "id",
                nullable(serde_json::json!({"type": "string", "format": "uuid"})),
            );
            let _ = py;

            let err = schema_value_expr(
                &crate::state::RegisteredModel::new_for_test(schema.clone(), "test_table".to_string()),
                "thing",
                "id",
                &serde_json::Value::String("not-a-uuid".to_string()),
                &HashMap::new(),
                &HashSet::new(),
                &HashMap::new(),
                Dialect::Postgres,
            )
            .expect_err("invalid UUID should error");

            let msg = err.to_string();
            assert!(
                msg.contains("thing"),
                "error message should name model: {msg}"
            );
            assert!(
                msg.contains("id"),
                "error message should name column: {msg}"
            );
            assert!(
                msg.contains("not-a-uuid"),
                "error message should include offending value: {msg}"
            );
        });
    }

    #[test]
    fn temporal_null_keeps_cast_for_now() {
        // Temporal types are deferred to issue #40 (chrono vs time decision).
        // For now, date-time / date null still relies on cast_as("timestamptz")
        // / cast_as("date") to prevent regression.
        let schema = schema_for(
            "created_at",
            nullable(serde_json::json!({"type": "string", "format": "date-time"})),
        );
        let mut ts_cast = HashMap::new();
        ts_cast.insert("created_at".to_string(), "timestamptz".to_string());

        let expr = schema_value_expr(
            &crate::state::RegisteredModel::new_for_test(schema.clone(), "test_table".to_string()),
            "thing",
            "created_at",
            &serde_json::Value::Null,
            &HashMap::new(),
            &HashSet::new(),
            &ts_cast,
            Dialect::Postgres,
        )
        .unwrap();
        let (sql, _) = Query::insert()
            .into_table(Alias::new("thing"))
            .columns([Alias::new("created_at")])
            .values_panic([expr])
            .build(PostgresQueryBuilder);

        assert!(
            sql.contains("CAST"),
            "temporal null should still CAST until #40: {sql}"
        );
    }

    #[test]
    fn sqlite_uuid_passthrough_unchanged() {
        let schema = schema_for(
            "id",
            nullable(serde_json::json!({"type": "string", "format": "uuid"})),
        );
        let uuid_str = "550e8400-e29b-41d4-a716-446655440000";

        let expr = schema_value_expr(
            &crate::state::RegisteredModel::new_for_test(schema.clone(), "test_table".to_string()),
            "thing",
            "id",
            &serde_json::Value::String(uuid_str.to_string()),
            &HashMap::new(),
            &HashSet::new(),
            &HashMap::new(),
            Dialect::Sqlite,
        )
        .unwrap();
        let (_, values) = Query::insert()
            .into_table(Alias::new("thing"))
            .columns([Alias::new("id")])
            .values_panic([expr])
            .build(sea_query::SqliteQueryBuilder);

        // SQLite preserves text-based UUID handling; typed Uuid path is
        // Postgres-only (R2 was about Postgres OID enforcement).
        match values.0.as_slice() {
            [SeaValue::String(Some(s))] => assert_eq!(**s, *uuid_str),
            other => panic!("expected SQLite UUID to remain text, got {other:?}"),
        }
    }
}

#[cfg(test)]
mod engine_bind_tests {
    use super::engine_bind_values_from_sea;
    use crate::backend::{EngineBindValue, NullKind};
    use sea_query::Value as SeaValue;

    #[test]
    fn maps_typed_none_to_matching_null_kind() {
        let inputs = vec![
            SeaValue::Bool(None),
            SeaValue::Int(None),
            SeaValue::BigInt(None),
            SeaValue::Double(None),
            SeaValue::Float(None),
            SeaValue::String(None),
            SeaValue::Bytes(None),
            SeaValue::Uuid(None),
        ];

        let mapped = engine_bind_values_from_sea(&inputs);

        assert_eq!(
            mapped,
            vec![
                EngineBindValue::Null(NullKind::Bool),
                EngineBindValue::Null(NullKind::I64),
                EngineBindValue::Null(NullKind::I64),
                EngineBindValue::Null(NullKind::F64),
                EngineBindValue::Null(NullKind::F64),
                EngineBindValue::Null(NullKind::String),
                EngineBindValue::Null(NullKind::Bytes),
                EngineBindValue::Null(NullKind::Uuid),
            ]
        );
    }

    #[test]
    fn maps_typed_uuid_some_to_engine_uuid() {
        // Regression for the user-reported `column "id" is of type uuid but
        // expression is of type text` failure: prior to this arm, a non-null
        // SeaValue::Uuid fell through to the catch-all and was bound as a
        // text-typed null, silently corrupting every UUID INSERT/UPDATE/WHERE.
        let u = uuid::Uuid::parse_str("550e8400-e29b-41d4-a716-446655440000")
            .expect("static UUID parses");
        let inputs = vec![SeaValue::Uuid(Some(Box::new(u)))];

        assert_eq!(
            engine_bind_values_from_sea(&inputs),
            vec![EngineBindValue::Uuid(u)]
        );
    }

    #[test]
    fn falls_back_to_untyped_for_unmapped_variant() {
        // TinyUnsigned has no Some arm and no None arm in
        // engine_bind_values_from_sea, so the catch-all fires. This locks
        // in the documented Untyped fallback for any future SeaQuery variant
        // we have not explicitly mapped.
        let inputs = vec![SeaValue::TinyUnsigned(None)];

        assert_eq!(
            engine_bind_values_from_sea(&inputs),
            vec![EngineBindValue::Null(NullKind::Untyped)]
        );
    }

    #[test]
    fn sea_query_preserves_typed_none_through_build() {
        use sea_query::{Alias, Expr, PostgresQueryBuilder, Query};

        let (_, values) = Query::insert()
            .into_table(Alias::new("t"))
            .columns([Alias::new("n")])
            .values_panic([Expr::value(SeaValue::Int(None))])
            .build(PostgresQueryBuilder);

        // Confirm SeaQuery itself preserves the typed None through .build(),
        // so the mapping in engine_bind_values_from_sea is operating on
        // accurate input rather than a coerced text-typed null.
        assert!(matches!(values.0.as_slice(), [SeaValue::Int(None)]));
    }
}

#[cfg(test)]
mod raw_sql_tests {
    use super::python_to_engine_bind_value;
    use crate::backend::EngineBindValue;
    use pyo3::IntoPyObjectExt;
    use pyo3::prelude::*;
    use pyo3::types::{PyBool, PyBytes, PyFloat, PyString};

    #[test]
    fn extracts_none_as_null() {
        Python::attach(|py| {
            let val = py.None().into_bound(py);
            assert_eq!(
                python_to_engine_bind_value(&val).unwrap(),
                EngineBindValue::Null(crate::backend::NullKind::Untyped)
            );
        });
    }

    #[test]
    fn extracts_true_as_bool_not_int() {
        Python::attach(|py| {
            let val = PyBool::new(py, true).to_owned().into_any();
            assert_eq!(
                python_to_engine_bind_value(&val).unwrap(),
                EngineBindValue::Bool(true)
            );
        });
    }

    #[test]
    fn extracts_false_as_bool_not_int() {
        Python::attach(|py| {
            let val = PyBool::new(py, false).to_owned().into_any();
            assert_eq!(
                python_to_engine_bind_value(&val).unwrap(),
                EngineBindValue::Bool(false)
            );
        });
    }

    #[test]
    fn extracts_int() {
        Python::attach(|py| {
            let val = 42i64.into_py_any(py).unwrap().into_bound(py);
            assert_eq!(
                python_to_engine_bind_value(&val).unwrap(),
                EngineBindValue::I64(42)
            );
        });
    }

    #[test]
    fn extracts_float() {
        Python::attach(|py| {
            let val = PyFloat::new(py, 3.14).into_any();
            assert_eq!(
                python_to_engine_bind_value(&val).unwrap(),
                EngineBindValue::F64(3.14)
            );
        });
    }

    #[test]
    fn extracts_string() {
        Python::attach(|py| {
            let val = PyString::new(py, "ferro").into_any();
            assert_eq!(
                python_to_engine_bind_value(&val).unwrap(),
                EngineBindValue::String("ferro".to_string())
            );
        });
    }

    #[test]
    fn extracts_bytes() {
        Python::attach(|py| {
            let val = PyBytes::new(py, &[1u8, 2, 3]).into_any();
            assert_eq!(
                python_to_engine_bind_value(&val).unwrap(),
                EngineBindValue::Bytes(vec![1, 2, 3])
            );
        });
    }

    #[test]
    fn rejects_unsupported_type() {
        Python::attach(|py| {
            let dict = pyo3::types::PyDict::new(py).into_any();
            let err = python_to_engine_bind_value(&dict).unwrap_err();
            assert!(err.is_instance_of::<pyo3::exceptions::PyTypeError>(py));
            let msg = err.to_string();
            assert!(
                msg.contains("Unsupported raw SQL bind value"),
                "unexpected error message: {msg}"
            );
        });
    }
}

#[cfg(test)]
mod engine_value_to_rust_value_tests {
    use super::engine_value_to_rust_value;
    use crate::backend::EngineValue;
    use crate::state::RustValue;

    fn decimal_schema() -> serde_json::Value {
        serde_json::json!({
            "properties": {
                "hours": {
                    "format": "decimal",
                    "anyOf": [
                        {"type": "number"},
                        {"type": "string", "pattern": "^-?\\d+(\\.\\d+)?$"}
                    ]
                }
            }
        })
    }

    #[test]
    fn decimal_column_maps_sqlite_integer_affinity_to_decimal() {
        let schema = decimal_schema();
        let out = engine_value_to_rust_value(EngineValue::I64(3), &schema, "hours");
        assert!(matches!(out, RustValue::Decimal(ref s) if s == "3"));
    }

    #[test]
    fn decimal_column_maps_real_and_text() {
        let schema = decimal_schema();
        let from_real = engine_value_to_rust_value(EngineValue::F64(1.5), &schema, "hours");
        assert!(matches!(from_real, RustValue::Decimal(ref s) if s == "1.5"));
        let from_text =
            engine_value_to_rust_value(EngineValue::String("2.25".into()), &schema, "hours");
        assert!(matches!(from_text, RustValue::Decimal(ref s) if s == "2.25"));
    }

    fn datetime_schema() -> serde_json::Value {
        serde_json::json!({
            "properties": {
                "happened_at": {"type": "string", "format": "date-time"}
            }
        })
    }

    fn binary_schema() -> serde_json::Value {
        serde_json::json!({
            "properties": {
                "data": {"type": "string", "format": "binary"}
            }
        })
    }

    fn bool_schema() -> serde_json::Value {
        serde_json::json!({
            "properties": {
                "is_active": {"type": "boolean"}
            }
        })
    }

    fn json_schema() -> serde_json::Value {
        serde_json::json!({
            "properties": {
                "payload": {"type": "object"}
            }
        })
    }

    #[test]
    fn datetime_column_only_accepts_string_engine_values() {
        let schema = datetime_schema();
        let ok = engine_value_to_rust_value(
            EngineValue::String("2026-04-24T18:30:00+00:00".into()),
            &schema,
            "happened_at",
        );
        assert!(matches!(ok, RustValue::DateTime(_)));
        let from_int =
            engine_value_to_rust_value(EngineValue::I64(1713984600), &schema, "happened_at");
        assert!(matches!(from_int, RustValue::BigInt(1713984600)));
    }

    #[test]
    fn binary_column_maps_bytes_and_text() {
        let schema = binary_schema();
        let from_bytes =
            engine_value_to_rust_value(EngineValue::Bytes(vec![1, 2, 3]), &schema, "data");
        assert!(matches!(from_bytes, RustValue::Blob(v) if v == vec![1, 2, 3]));
        let from_text =
            engine_value_to_rust_value(EngineValue::String("abc".into()), &schema, "data");
        assert!(matches!(from_text, RustValue::Blob(v) if v == b"abc".to_vec()));
        let from_int = engine_value_to_rust_value(EngineValue::I64(1), &schema, "data");
        assert!(matches!(from_int, RustValue::None));
    }

    #[test]
    fn bool_column_maps_integer_and_bool() {
        let schema = bool_schema();
        assert!(matches!(
            engine_value_to_rust_value(EngineValue::I64(1), &schema, "is_active"),
            RustValue::Bool(true)
        ));
        assert!(matches!(
            engine_value_to_rust_value(EngineValue::Bool(false), &schema, "is_active"),
            RustValue::Bool(false)
        ));
    }

    #[test]
    fn json_column_parses_string_payload() {
        let schema = json_schema();
        let out = engine_value_to_rust_value(
            EngineValue::String(r#"{"k":"v"}"#.into()),
            &schema,
            "payload",
        );
        assert!(
            matches!(out, RustValue::Json(v) if v.get("k").and_then(|x| x.as_str()) == Some("v"))
        );
    }
}

#[cfg(test)]
mod mutation_pagination_guard_tests {
    use super::{query_plan_from_ir_json, reject_pagination_on_mutation};

    fn envelope_without_pagination_keys() -> String {
        serde_json::json!({
            "ir_kind": "query",
            "ir_version": 3,
            "payload": {
                "model_name": "Widget",
                "where": [{
                    "node_kind": "leaf",
                    "operator": "==",
                    "column": "name",
                    "value": {"kind": "string", "value": "a"},
                    "path": []
                }],
                "order_by": [],
                "m2m": null, "materialization": {"kind": "root_instances"},
                "joins": []
            }
        })
        .to_string()
    }

    #[test]
    fn query_ir_without_pagination_keys_parses_to_none() {
        let plan = query_plan_from_ir_json(&envelope_without_pagination_keys())
            .expect("mutating payloads omit limit/offset keys; parsing must succeed");
        assert_eq!(plan.limit, None);
        assert_eq!(plan.offset, None);
    }

    #[test]
    fn guard_allows_mutation_without_pagination() {
        let plan = query_plan_from_ir_json(&envelope_without_pagination_keys()).unwrap();
        assert!(reject_pagination_on_mutation(&plan, "delete").is_ok());
        assert!(reject_pagination_on_mutation(&plan, "update").is_ok());
    }

    #[test]
    fn guard_rejects_limit_with_pyvalueerror() {
        let mut plan = query_plan_from_ir_json(&envelope_without_pagination_keys()).unwrap();
        plan.limit = Some(5);
        pyo3::Python::attach(|py| {
            let err = reject_pagination_on_mutation(&plan, "delete")
                .expect_err("limit on a mutating query must be rejected");
            assert!(err.is_instance_of::<pyo3::exceptions::PyValueError>(py));
            let msg = err.to_string();
            assert!(
                msg.contains("delete") && msg.contains("limit"),
                "message should name the operation and limit: {msg}"
            );
        });
    }

    #[test]
    fn guard_rejects_offset_with_pyvalueerror() {
        let mut plan = query_plan_from_ir_json(&envelope_without_pagination_keys()).unwrap();
        plan.offset = Some(2);
        pyo3::Python::attach(|py| {
            let err = reject_pagination_on_mutation(&plan, "update")
                .expect_err("offset on a mutating query must be rejected");
            assert!(err.is_instance_of::<pyo3::exceptions::PyValueError>(py));
            let msg = err.to_string();
            assert!(
                msg.contains("update") && msg.contains("offset"),
                "message should name the operation and offset: {msg}"
            );
        });
    }
}

#[cfg(test)]
mod query_ir_version_gate_tests {
    use super::query_plan_from_ir_json;

    fn v3_envelope() -> serde_json::Value {
        serde_json::json!({
            "ir_kind": "query",
            "ir_version": 3,
            "payload": {
                "model_name": "Widget",
                "where": [],
                "order_by": [],
                "limit": null,
                "offset": null,
                "m2m": null,
                "joins": [],
                "materialization": {"kind": "root_instances"}
            }
        })
    }

    /// Assert a rejected envelope's message names the received version, this
    /// build's supported version (3), and the one-wheel fix — the actionable
    /// shape pinned since the v1-at-v2 bump (#269), re-pinned at v3 (#278).
    fn assert_actionable_version_rejection(err: pyo3::PyErr, received: char) {
        pyo3::Python::attach(|py| {
            assert!(err.is_instance_of::<pyo3::exceptions::PyValueError>(py));
        });
        let msg = err.to_string();
        assert!(
            msg.contains(received),
            "message should name the received version: {msg}"
        );
        assert!(
            msg.contains('3'),
            "message should name the supported version: {msg}"
        );
        assert!(
            msg.to_lowercase().contains("one wheel"),
            "message should explain Python/Rust ship in one wheel: {msg}"
        );
        assert!(
            msg.to_lowercase().contains("reinstall") || msg.to_lowercase().contains("rebuild"),
            "message should tell the caller how to fix a mismatch: {msg}"
        );
    }

    #[test]
    fn accepts_version_3() {
        query_plan_from_ir_json(&v3_envelope().to_string())
            .expect("a well-formed v3 envelope must be accepted");
    }

    /// Contract test (#278 acceptance criteria, mirroring the v1-at-v2
    /// precedent): a v2 envelope — with a real v2 payload that predates the
    /// `materialization` section entirely, as any real v2 emitter would
    /// produce — must be rejected on the version check with an actionable
    /// message, not on a serde "missing field" error from strict-parsing a
    /// payload shape this build no longer understands.
    #[test]
    fn rejects_v2_envelope_with_actionable_message() {
        let v2_envelope = serde_json::json!({
            "ir_kind": "query",
            "ir_version": 2,
            "payload": {
                "model_name": "Widget",
                "where": [],
                "order_by": [],
                "limit": null,
                "offset": null,
                "m2m": null,
                "joins": []
            }
        });

        let err = query_plan_from_ir_json(&v2_envelope.to_string())
            .expect_err("a v2 envelope must be rejected");
        assert_actionable_version_rejection(err, '2');
    }

    /// A v1 envelope (no `joins`/`path`/`materialization`) stays rejected the
    /// same way — the gate is "exactly one supported version", not a range.
    #[test]
    fn rejects_v1_envelope_with_actionable_message() {
        let v1_envelope = serde_json::json!({
            "ir_kind": "query",
            "ir_version": 1,
            "payload": {
                "model_name": "Widget",
                "where": [],
                "order_by": [],
                "limit": null,
                "offset": null,
                "m2m": null
            }
        });

        let err = query_plan_from_ir_json(&v1_envelope.to_string())
            .expect_err("a v1 envelope must be rejected");
        assert_actionable_version_rejection(err, '1');
    }

    #[test]
    fn rejects_unsupported_future_version() {
        let mut envelope = v3_envelope();
        envelope["ir_version"] = serde_json::json!(4);

        let err = query_plan_from_ir_json(&envelope.to_string())
            .expect_err("an unsupported future version must be rejected");
        let msg = err.to_string();
        assert!(
            msg.contains('4'),
            "message should name the received version: {msg}"
        );
    }

    /// An unknown materialization kind fails the strict payload parse loudly,
    /// naming the bad kind and the supported ones (#278).
    #[test]
    fn rejects_unknown_materialization_kind() {
        let mut envelope = v3_envelope();
        envelope["payload"]["materialization"] = serde_json::json!({"kind": "row_dicts"});

        let err = query_plan_from_ir_json(&envelope.to_string())
            .expect_err("an unknown materialization kind must be rejected");
        let msg = err.to_string();
        assert!(msg.contains("row_dicts"), "must name the bad kind: {msg}");
        assert!(
            msg.contains("root_instances"),
            "must name the supported kinds: {msg}"
        );
    }

    /// A v3 payload with NO materialization section fails the strict parse:
    /// the plan travels with the query as data (ADR-0007), never defaulted.
    #[test]
    fn rejects_v3_payload_missing_materialization() {
        let mut envelope = v3_envelope();
        envelope["payload"]
            .as_object_mut()
            .unwrap()
            .remove("materialization");

        let err = query_plan_from_ir_json(&envelope.to_string())
            .expect_err("a v3 payload without a materialization section must be rejected");
        let msg = err.to_string();
        assert!(
            msg.contains("materialization"),
            "must name the missing section: {msg}"
        );
    }
}

#[cfg(test)]
mod materialization_walker_gate_tests {
    //! Pin the runtime rejection of declared-but-unimplemented materialization
    //! kinds (#278): the types deserialize, the walkers refuse.

    use super::{query_plan_from_ir_json, reject_unsupported_materialization};

    fn plan_with_kind(kind: serde_json::Value) -> crate::query::QueryPlan {
        query_plan_from_ir_json(
            &serde_json::json!({
                "ir_kind": "query",
                "ir_version": 3,
                "payload": {
                    "model_name": "Widget",
                    "where": [],
                    "order_by": [],
                    "limit": null,
                    "offset": null,
                    "m2m": null,
                    "joins": [],
                    "materialization": kind
                }
            })
            .to_string(),
        )
        .expect("envelope parses")
    }

    #[test]
    fn root_instances_passes_every_walker_gate() {
        let plan = plan_with_kind(serde_json::json!({"kind": "root_instances"}));
        for operation in ["fetch_filtered", "count_filtered", "update", "delete"] {
            assert!(reject_unsupported_materialization(&plan, operation).is_ok());
        }
    }

    #[test]
    fn record_kind_is_rejected_loudly_by_non_projecting_walkers() {
        let plan = plan_with_kind(serde_json::json!({
            "kind": "record",
            "fields": [{"name": "id", "column": "id", "path": []}]
        }));
        pyo3::Python::attach(|py| {
            let err = reject_unsupported_materialization(&plan, "count_filtered")
                .expect_err("record must be rejected");
            assert!(err.is_instance_of::<pyo3::exceptions::PyValueError>(py));
            let msg = err.to_string();
            assert!(msg.contains("record"), "must name the kind: {msg}");
            assert!(
                msg.contains("count_filtered"),
                "must name the operation: {msg}"
            );
        });
    }

    #[test]
    fn instances_kind_is_rejected_loudly() {
        let plan = plan_with_kind(serde_json::json!({"kind": "instances"}));
        pyo3::Python::attach(|py| {
            let err = reject_unsupported_materialization(&plan, "fetch_filtered")
                .expect_err("instances must be rejected");
            assert!(err.is_instance_of::<pyo3::exceptions::PyValueError>(py));
            let msg = err.to_string();
            assert!(msg.contains("instances"), "must name the kind: {msg}");
            assert!(
                msg.contains("joined-row hydration"),
                "must name what the kind is reserved for: {msg}"
            );
        });
    }
}

#[cfg(test)]
mod mutation_qualification_tests {
    //! Pin root-alias qualification in mutating WHERE clauses (#269).
    //!
    //! `update_filtered`/`delete_filtered` assemble their statements inside async
    //! pyfunctions with no directly callable seam, so these tests rebuild the same
    //! statement shape (`Query::delete()` / `UpdateStatement` +
    //! `query_condition_for_backend(.., Some(&table_name))`) and assert the rendered
    //! SQL qualifies the WHERE column on both dialects.

    use super::{query_condition_for_backend, query_plan_from_ir_json};
    use crate::state::Dialect;
    use sea_query::{Alias, PostgresQueryBuilder, Query, SqliteQueryBuilder, UpdateStatement};

    fn widget_plan() -> crate::query::QueryPlan {
        query_plan_from_ir_json(
            &serde_json::json!({
                "ir_kind": "query",
                "ir_version": 3,
                "payload": {
                    "model_name": "Widget",
                    "where": [{
                        "node_kind": "leaf",
                        "operator": "==",
                        "column": "name",
                        "value": {"kind": "string", "value": "a"},
                        "path": []
                    }],
                    "order_by": [],
                    "m2m": null, "materialization": {"kind": "root_instances"},
                    "joins": []
                }
            })
            .to_string(),
        )
        .expect("v2 envelope parses")
    }

    #[test]
    fn delete_where_column_is_root_qualified_on_both_dialects() {
        let plan = widget_plan();
        for backend in [Dialect::Sqlite, Dialect::Postgres] {
            let mut delete = Query::delete();
            delete
                .from_table(Alias::new("widget"))
                .cond_where(query_condition_for_backend(&plan, backend, Some("widget")).unwrap());
            let sql = match backend {
                Dialect::Postgres => delete.to_string(PostgresQueryBuilder),
                Dialect::Sqlite => delete.to_string(SqliteQueryBuilder),
            };
            assert!(
                sql.contains("\"widget\".\"name\"") || sql.contains("`widget`.`name`"),
                "DELETE WHERE column must be root-qualified on {backend:?}: {sql}"
            );
        }
    }

    #[test]
    fn update_where_column_is_root_qualified_on_both_dialects() {
        let plan = widget_plan();
        for backend in [Dialect::Sqlite, Dialect::Postgres] {
            let mut update = UpdateStatement::new()
                .table(Alias::new("widget"))
                .cond_where(query_condition_for_backend(&plan, backend, Some("widget")).unwrap())
                .to_owned();
            update.value(Alias::new("name"), "b");
            let sql = match backend {
                Dialect::Postgres => update.to_string(PostgresQueryBuilder),
                Dialect::Sqlite => update.to_string(SqliteQueryBuilder),
            };
            assert!(
                sql.contains("\"widget\".\"name\" =") || sql.contains("`widget`.`name` ="),
                "UPDATE WHERE column must be root-qualified on {backend:?}: {sql}"
            );
            // SET assignment column must stay bare (SQL forbids qualified SET targets).
            assert!(
                sql.contains("SET \"name\"") || sql.contains("SET `name`"),
                "UPDATE SET column must stay unqualified on {backend:?}: {sql}"
            );
        }
    }
}

#[cfg(test)]
mod select_join_render_tests {
    //! Pin the FULL rendered SELECT SQL for a joined query (#271) — including the
    //! `INNER JOIN <table> AS <alias> ON <prev>.<from_column> = <alias>.<to_column>`
    //! clause text `apply_relation_joins` renders. Task 3 (#270) only pinned the
    //! `JoinPlan` struct's alias fields at the unit level; the reviewer flagged the
    //! missing rendered-SQL pin, closed here since this slice extends the exact
    //! same SELECT-walker seam to `ORDER BY`.

    use super::{
        apply_relation_joins, query_condition_with_joins, query_join_plan, query_plan_from_ir_json,
    };
    use crate::state::Dialect;
    use sea_query::{Alias, Order, PostgresQueryBuilder, SelectStatement};

    /// `Transaction -> account` traversed by BOTH a `where()` leaf and an
    /// `order_by` term on the same path — the one-join acceptance criterion.
    fn shared_path_plan() -> crate::query::QueryPlan {
        query_plan_from_ir_json(
            &serde_json::json!({
                "ir_kind": "query",
                "ir_version": 3,
                "payload": {
                    "model_name": "Transaction",
                    "where": [{
                        "node_kind": "leaf",
                        "operator": "==",
                        "column": "label",
                        "value": {"kind": "string", "value": "a1"},
                        "path": ["account"]
                    }],
                    "order_by": [{"column": "name", "direction": "asc", "path": ["account"]}],
                    "limit": null, "offset": null, "m2m": null, "materialization": {"kind": "root_instances"},
                    "joins": [
                        {"join_type": "inner", "path": [
                            {"relation": "account", "from_column": "account_id",
                             "to_table": "account", "to_column": "id"}
                        ]}
                    ]
                }
            })
            .to_string(),
        )
        .expect("v2 envelope parses")
    }

    #[test]
    fn rendered_select_pins_join_clause_and_dedups_shared_where_order_by_path() {
        let mut plan = shared_path_plan();
        // The walkers resolve each joined table's registration + enum catalog
        // before building conditions; mirror that for the traversed `label` leaf
        // (path [account] -> table "account").
        plan.hop_registrations.insert(
            "account".to_string(),
            crate::state::RegisteredModel::new_for_test(
                serde_json::json!({
                    "properties": {"label": {"type": "string"}, "name": {"type": "string"}}
                }),
                "account".to_string(),
            ),
        );
        plan.hop_enum_udt
            .insert("account".to_string(), std::collections::HashMap::new());
        let join_plan = query_join_plan(&plan, "transaction", &[]).expect("join plan");

        let mut select = SelectStatement::new();
        select
            .column((Alias::new("transaction"), sea_query::Asterisk))
            .from(Alias::new("transaction"));
        apply_relation_joins(&mut select, &join_plan).expect("joins render");
        select.cond_where(
            query_condition_with_joins(&plan, Dialect::Postgres, "transaction", &join_plan)
                .expect("condition builds"),
        );
        for order in &plan.order_by {
            let col = crate::query::qualify_column_with_joins(
                "transaction",
                &join_plan,
                &order.column,
                &order.path,
            )
            .expect("order_by column qualifies by its join alias");
            select.order_by_expr(col.into(), Order::Asc);
        }

        let sql = select.to_string(PostgresQueryBuilder);

        let expected_join = "INNER JOIN \"account\" AS \"j1_account\" ON \
             \"transaction\".\"account_id\" = \"j1_account\".\"id\"";
        assert!(
            sql.contains(expected_join),
            "expected the exact rendered JOIN clause, got: {sql}"
        );
        // Same relation path referenced by both the WHERE leaf and the order_by
        // term must still render exactly ONE join (#270/#271 acceptance).
        assert_eq!(
            sql.matches("INNER JOIN").count(),
            1,
            "shared where()/order_by path must render exactly one JOIN: {sql}"
        );
        assert!(
            sql.contains("WHERE \"j1_account\".\"label\""),
            "WHERE leaf must qualify by the shared join alias: {sql}"
        );
        assert!(
            sql.contains("ORDER BY \"j1_account\".\"name\""),
            "ORDER BY must qualify by the shared join alias: {sql}"
        );
    }

    /// Build a plain `SELECT * FROM <root>` with the plan's relation joins
    /// rendered — the exact seam both `fetch_filtered` and `count_filtered`
    /// call (`apply_relation_joins`), so a rendered-SQL pin here covers both.
    fn render_joined_select(plan: &crate::query::QueryPlan, root_table: &str) -> String {
        let join_plan = query_join_plan(plan, root_table, &[]).expect("join plan");
        let mut select = SelectStatement::new();
        select
            .column((Alias::new(root_table), sea_query::Asterisk))
            .from(Alias::new(root_table));
        apply_relation_joins(&mut select, &join_plan).expect("joins render");
        select.to_string(PostgresQueryBuilder)
    }

    fn joined_plan(joins: serde_json::Value, model_name: &str) -> crate::query::QueryPlan {
        query_plan_from_ir_json(
            &serde_json::json!({
                "ir_kind": "query",
                "ir_version": 3,
                "payload": {
                    "model_name": model_name,
                    "where": [], "order_by": [],
                    "limit": null, "offset": null, "m2m": null, "materialization": {"kind": "root_instances"},
                    "joins": joins
                }
            })
            .to_string(),
        )
        .expect("v2 envelope parses")
    }

    /// `t.account == instance` desugars to a shadow-FK leaf (`account_id`) with
    /// an EMPTY path and NO joins entry — the join-free proof (#273). The
    /// rendered SELECT must contain no JOIN and a WHERE qualified by the ROOT
    /// table's `account_id`, never a relation alias.
    #[test]
    fn rendered_select_instance_equality_is_join_free() {
        let plan = query_plan_from_ir_json(
            &serde_json::json!({
                "ir_kind": "query",
                "ir_version": 3,
                "payload": {
                    "model_name": "Transaction",
                    "where": [{
                        "node_kind": "leaf",
                        "operator": "==",
                        "column": "account_id",
                        "value": {"kind": "int", "value": 7},
                        "path": []
                    }],
                    "order_by": [], "limit": null, "offset": null, "m2m": null, "materialization": {"kind": "root_instances"},
                    "joins": []
                }
            })
            .to_string(),
        )
        .expect("v2 envelope parses");

        let join_plan = query_join_plan(&plan, "transaction", &[]).expect("join plan");
        let mut select = SelectStatement::new();
        select
            .column((Alias::new("transaction"), sea_query::Asterisk))
            .from(Alias::new("transaction"));
        apply_relation_joins(&mut select, &join_plan).expect("joins render");
        select.cond_where(
            query_condition_with_joins(&plan, Dialect::Postgres, "transaction", &join_plan)
                .expect("condition builds"),
        );
        let sql = select.to_string(PostgresQueryBuilder);

        assert!(
            !sql.contains("JOIN"),
            "instance equality must be join-free: {sql}"
        );
        assert!(
            sql.contains("WHERE \"transaction\".\"account_id\""),
            "WHERE must filter the ROOT-qualified shadow FK: {sql}"
        );
    }

    /// A `.left_join(t.account)` renders a LEFT JOIN clause (#272 NULL retention).
    #[test]
    fn rendered_select_pins_left_join_clause() {
        let plan = joined_plan(
            serde_json::json!([
                {"join_type": "left", "path": [
                    {"relation": "account", "from_column": "account_id",
                     "to_table": "account", "to_column": "id"}
                ]}
            ]),
            "Note",
        );
        let sql = render_joined_select(&plan, "note");
        let expected = "LEFT JOIN \"account\" AS \"j1_account\" ON \
             \"note\".\"account_id\" = \"j1_account\".\"id\"";
        assert!(
            sql.contains(expected),
            "expected LEFT JOIN clause, got: {sql}"
        );
        assert_eq!(sql.matches("LEFT JOIN").count(), 1, "one LEFT JOIN: {sql}");
        assert_eq!(sql.matches("INNER JOIN").count(), 0, "no INNER JOIN: {sql}");
    }

    /// Whole-path LEFT: a 2-hop `.left_join` renders BOTH edges LEFT (#272).
    #[test]
    fn rendered_select_whole_path_left_renders_both_hops_left() {
        let plan = joined_plan(
            serde_json::json!([
                {"join_type": "left", "path": [
                    {"relation": "account", "from_column": "account_id",
                     "to_table": "account", "to_column": "id"},
                    {"relation": "owner", "from_column": "owner_id",
                     "to_table": "owner", "to_column": "id"}
                ]}
            ]),
            "Transaction",
        );
        let sql = render_joined_select(&plan, "transaction");
        assert!(
            sql.contains("LEFT JOIN \"account\" AS \"j1_account\""),
            "hop-1 LEFT JOIN: {sql}"
        );
        assert!(
            sql.contains(
                "LEFT JOIN \"owner\" AS \"j2_owner\" ON \
                 \"j1_account\".\"owner_id\" = \"j2_owner\".\"id\""
            ),
            "hop-2 LEFT JOIN chained off hop-1 alias: {sql}"
        );
        assert_eq!(sql.matches("LEFT JOIN").count(), 2, "two LEFT JOINs: {sql}");
        assert_eq!(sql.matches("INNER JOIN").count(), 0, "no INNER JOIN: {sql}");
    }

    /// Mixed case (#272 pin): a `"left"` `[account]` entry and an `"inner"`
    /// `[account, owner]` entry render TWO joins (shared prefix dedups) — the
    /// account edge LEFT, the owner edge INNER — regardless of entry order.
    #[test]
    fn rendered_select_mixed_left_prefix_inner_suffix_is_order_independent() {
        let left_first = serde_json::json!([
            {"join_type": "left", "path": [
                {"relation": "account", "from_column": "account_id",
                 "to_table": "account", "to_column": "id"}
            ]},
            {"join_type": "inner", "path": [
                {"relation": "account", "from_column": "account_id",
                 "to_table": "account", "to_column": "id"},
                {"relation": "owner", "from_column": "owner_id",
                 "to_table": "owner", "to_column": "id"}
            ]}
        ]);
        let inner_first = serde_json::json!([
            {"join_type": "inner", "path": [
                {"relation": "account", "from_column": "account_id",
                 "to_table": "account", "to_column": "id"},
                {"relation": "owner", "from_column": "owner_id",
                 "to_table": "owner", "to_column": "id"}
            ]},
            {"join_type": "left", "path": [
                {"relation": "account", "from_column": "account_id",
                 "to_table": "account", "to_column": "id"}
            ]}
        ]);
        for joins in [left_first, inner_first] {
            let plan = joined_plan(joins, "Transaction");
            let sql = render_joined_select(&plan, "transaction");
            assert_eq!(
                sql.matches(" JOIN ").count(),
                2,
                "shared prefix dedups to exactly two joins: {sql}"
            );
            assert!(
                sql.contains("LEFT JOIN \"account\" AS \"j1_account\""),
                "account edge LEFT: {sql}"
            );
            assert!(
                sql.contains("INNER JOIN \"owner\" AS \"j2_owner\""),
                "owner edge INNER: {sql}"
            );
        }
    }
}

#[cfg(test)]
mod save_mode_sql_tests {
    use super::{
        BindInput, SaveMode, UpdateByPkSql, build_save_sql, build_update_by_pk_sql, parse_save_mode,
    };
    use crate::state::Dialect;
    use std::collections::{HashMap, HashSet};

    fn widget_schema() -> serde_json::Value {
        serde_json::json!({
            "properties": {
                "id": {"type": "integer", "primary_key": true, "autoincrement": true},
                "name": {"type": "string"}
            }
        })
    }

    fn widget_inputs(id: serde_json::Value, name: &str) -> Vec<(String, BindInput)> {
        vec![
            ("id".to_string(), BindInput::Json(id)),
            ("name".to_string(), BindInput::Json(serde_json::json!(name))),
        ]
    }

    fn build_save(
        inputs: &[(String, BindInput)],
        pk_is_auto: bool,
        mode: SaveMode,
        backend: Dialect,
    ) -> (String, sea_query::Values, bool) {
        build_save_sql(
            &crate::state::RegisteredModel::new_for_test(widget_schema(), "widget".to_string()),
            "widget",
            inputs,
            Some("id"),
            pk_is_auto,
            mode,
            backend,
            &HashMap::new(),
            &HashSet::new(),
            &HashMap::new(),
        )
        .expect("build_save_sql should succeed")
    }

    fn build_update(
        schema: &serde_json::Value,
        inputs: &[(String, BindInput)],
        pk_col: Option<&str>,
        backend: Dialect,
    ) -> super::PyResult<UpdateByPkSql> {
        build_update_by_pk_sql(
            &crate::state::RegisteredModel::new_for_test(schema.clone(), "test_table".to_string()),
            "widget",
            inputs,
            pk_col,
            backend,
            &HashMap::new(),
            &HashSet::new(),
            &HashMap::new(),
        )
    }

    #[test]
    fn parse_save_mode_accepts_insert_and_upsert() {
        assert_eq!(parse_save_mode("insert").unwrap(), SaveMode::Insert);
        assert_eq!(parse_save_mode("upsert").unwrap(), SaveMode::Upsert);
    }

    #[test]
    fn parse_save_mode_rejects_unknown_mode_with_pyvalueerror() {
        pyo3::Python::attach(|py| {
            let err = parse_save_mode("update").expect_err("unknown mode must be rejected");
            assert!(err.is_instance_of::<pyo3::exceptions::PyValueError>(py));
            let msg = err.to_string();
            assert!(
                msg.contains("insert") && msg.contains("upsert") && msg.contains("update"),
                "message should name the accepted modes and the offender: {msg}"
            );
        });
    }

    #[test]
    fn insert_mode_renders_no_on_conflict_even_with_pk_provided() {
        for backend in [Dialect::Sqlite, Dialect::Postgres] {
            let (sql, _, _) = build_save(
                &widget_inputs(serde_json::json!(1), "a"),
                true,
                SaveMode::Insert,
                backend,
            );
            assert!(
                !sql.contains("ON CONFLICT"),
                "insert mode must render a plain INSERT on {backend:?}: {sql}"
            );
        }
    }

    #[test]
    fn upsert_mode_renders_on_conflict_do_update_when_pk_provided() {
        for backend in [Dialect::Sqlite, Dialect::Postgres] {
            let (sql, _, _) = build_save(
                &widget_inputs(serde_json::json!(1), "a"),
                true,
                SaveMode::Upsert,
                backend,
            );
            let conflict_clause = sql.split("ON CONFLICT").nth(1).unwrap_or_else(|| {
                panic!("upsert with provided PK must ON CONFLICT on {backend:?}: {sql}")
            });
            assert!(
                conflict_clause.contains("\"name\""),
                "non-PK column must be in the update list on {backend:?}: {sql}"
            );
            assert!(
                !conflict_clause.contains("\"id\" ="),
                "PK must not be reassigned in the update list on {backend:?}: {sql}"
            );
        }
    }

    #[test]
    fn upsert_mode_without_pk_on_auto_pk_is_plain_insert() {
        let (sql, _, _) = build_save(
            &widget_inputs(serde_json::Value::Null, "a"),
            true,
            SaveMode::Upsert,
            Dialect::Sqlite,
        );
        assert!(
            !sql.contains("ON CONFLICT"),
            "unset auto PK has nothing to conflict on: {sql}"
        );
        assert!(
            !sql.contains("\"id\""),
            "unset auto PK must be omitted from the column list: {sql}"
        );
    }

    #[test]
    fn insert_mode_preserves_postgres_returning_for_auto_pk() {
        let (sql, _, needs_returning) = build_save(
            &widget_inputs(serde_json::Value::Null, "a"),
            true,
            SaveMode::Insert,
            Dialect::Postgres,
        );
        assert!(
            needs_returning,
            "auto PK left unset needs RETURNING on postgres"
        );
        assert!(sql.contains("RETURNING \"id\""), "sql: {sql}");

        let (sql, _, needs_returning) = build_save(
            &widget_inputs(serde_json::Value::Null, "a"),
            true,
            SaveMode::Insert,
            Dialect::Sqlite,
        );
        assert!(
            !needs_returning,
            "sqlite uses last_insert_id, not RETURNING"
        );
        assert!(!sql.contains("RETURNING"), "sql: {sql}");
    }

    #[test]
    fn update_by_pk_sets_non_pk_columns_and_filters_on_pk() {
        let result = build_update(
            &widget_schema(),
            &widget_inputs(serde_json::json!(1), "a"),
            Some("id"),
            Dialect::Sqlite,
        )
        .expect("update build should succeed");
        let UpdateByPkSql::Update(sql, values) = result else {
            panic!("expected an UPDATE statement");
        };
        assert_eq!(sql, "UPDATE \"widget\" SET \"name\" = ? WHERE \"id\" = ?");
        assert_eq!(values.0.len(), 2, "one SET bind + one WHERE bind");

        let result = build_update(
            &widget_schema(),
            &widget_inputs(serde_json::json!(1), "a"),
            Some("id"),
            Dialect::Postgres,
        )
        .expect("update build should succeed");
        let UpdateByPkSql::Update(sql, _) = result else {
            panic!("expected an UPDATE statement");
        };
        assert_eq!(sql, "UPDATE \"widget\" SET \"name\" = $1 WHERE \"id\" = $2");
    }

    #[test]
    fn update_by_pk_requires_pk_value() {
        pyo3::Python::attach(|py| {
            let err = build_update(
                &widget_schema(),
                &widget_inputs(serde_json::Value::Null, "a"),
                Some("id"),
                Dialect::Sqlite,
            )
            .expect_err("null PK value must be rejected");
            assert!(err.is_instance_of::<pyo3::exceptions::PyValueError>(py));
            assert!(err.to_string().contains("primary key"), "msg: {err}");
        });
    }

    #[test]
    fn update_by_pk_on_pk_only_model_renders_existence_check() {
        let schema = serde_json::json!({
            "properties": {
                "id": {"type": "integer", "primary_key": true, "autoincrement": false}
            }
        });
        let inputs = vec![("id".to_string(), BindInput::Json(serde_json::json!(7)))];
        let result = build_update(&schema, &inputs, Some("id"), Dialect::Sqlite)
            .expect("pk-only build should succeed");
        let UpdateByPkSql::ExistenceCheck(sql, values) = result else {
            panic!("expected an existence check for a PK-only model");
        };
        assert!(
            sql.starts_with("SELECT COUNT(") && sql.contains("WHERE \"id\" = ?"),
            "sql: {sql}"
        );
        assert_eq!(values.0.len(), 1);
    }

    #[test]
    fn update_by_pk_without_pk_column_errors() {
        pyo3::Python::attach(|py| {
            let schema = serde_json::json!({"properties": {"name": {"type": "string"}}});
            let inputs = vec![("name".to_string(), BindInput::Json(serde_json::json!("a")))];
            let err = build_update(&schema, &inputs, None, Dialect::Sqlite)
                .expect_err("a model without a PK cannot be updated by PK");
            assert!(err.is_instance_of::<pyo3::exceptions::PyValueError>(py));
            assert!(err.to_string().contains("primary key"), "msg: {err}");
        });
    }
}

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
