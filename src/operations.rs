//! Core database operations for Ferro models.
//!
//! This module implements high-performance CRUD operations, leveraging
//! GIL-free parsing and zero-copy Direct Injection into Python objects.

use crate::backend::{
    BackendKind, EngineBindValue, EngineHandle, EngineRow, EngineValue, NullKind,
};
use crate::query::{QueryDef, query_def_from_ir_payload};
use crate::state::{
    IDENTITY_MAP, MODEL_REGISTRY, SqlDialect, TRANSACTION_REGISTRY, TransactionConnection,
    TransactionHandle, connection_for_route, engine_for_connection,
};
use ferro_schema_ir::{IrEnvelope, QueryIrPayload};
use pyo3::prelude::*;
use sea_query::{
    Alias, Expr, Iden, InsertStatement, OnConflict, Order, PostgresQueryBuilder, Query, SimpleExpr,
    SqliteQueryBuilder, UpdateStatement, Value as SeaValue,
};
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use std::sync::Arc;

fn get_transaction_connection(tx_id: Option<String>) -> Option<TransactionConnection> {
    tx_id.and_then(|id| {
        TRANSACTION_REGISTRY
            .get(&id)
            .map(|tx| tx.value().conn.clone())
    })
}

fn get_transaction_route(tx_id: Option<String>) -> Option<(String, TransactionConnection)> {
    tx_id.and_then(|id| {
        TRANSACTION_REGISTRY
            .get(&id)
            .map(|tx| (tx.value().connection_name.clone(), tx.value().conn.clone()))
    })
}

fn active_engine_for_connection(using: Option<String>) -> PyResult<Arc<EngineHandle>> {
    engine_for_connection(using)
}

fn active_route_for_operation(
    tx_id: Option<String>,
    using: Option<String>,
) -> PyResult<(
    String,
    Arc<EngineHandle>,
    Option<TransactionConnection>,
    BackendKind,
)> {
    let tx_route = get_transaction_route(tx_id);
    let route_using = tx_route
        .as_ref()
        .map(|(connection_name, _)| connection_name.clone())
        .or(using);
    let (connection_name, engine) = active_connection_for_route(route_using)?;
    let backend = engine.backend();
    let tx_conn = tx_route.map(|(_, conn)| conn);
    Ok((connection_name, engine, tx_conn, backend))
}

fn active_connection_for_route(using: Option<String>) -> PyResult<(String, Arc<EngineHandle>)> {
    connection_for_route(using)
}

#[derive(Debug, Clone, Deserialize)]
struct QueryIrEnvelope {
    ir_kind: String,
    ir_version: u32,
    payload: QueryIrPayload,
}

fn query_def_from_ir_json(query_ir_json: &str) -> PyResult<QueryDef> {
    let envelope: QueryIrEnvelope = serde_json::from_str(query_ir_json).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("Invalid QueryIR JSON: {}", e))
    })?;
    if envelope.ir_kind != "query" {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "Invalid QueryIR envelope kind {:?}; expected \"query\"",
            envelope.ir_kind
        )));
    }
    if envelope.ir_version != 1 {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "Unsupported QueryIR version {}; expected 1",
            envelope.ir_version
        )));
    }
    query_def_from_ir_payload(envelope.payload)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("Invalid QueryIR: {e}")))
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

async fn execute_statement_with_optional_tx(
    engine: &EngineHandle,
    tx_conn: Option<TransactionConnection>,
    sql: &str,
    bind_values: &[SeaValue],
) -> Result<u64, sqlx::Error> {
    match tx_conn {
        Some(conn_arc) => {
            let engine_bind_values = engine_bind_values_from_sea(bind_values);
            let mut conn = conn_arc.lock().await;
            conn.execute_sql_with_binds(sql, &engine_bind_values).await
        }
        None => {
            let engine_bind_values = engine_bind_values_from_sea(bind_values);
            engine
                .execute_sql_with_binds(sql, &engine_bind_values)
                .await
        }
    }
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
    crate::codec::decode_engine_value(value, schema, col_name)
}

/// One parsed row: the primary-key value (when present) plus all column values.
type ParsedRow = crate::codec::ParsedRow;

fn typed_rows_to_parsed_data(
    rows: Vec<EngineRow>,
    schema: &serde_json::Value,
    pk_col: Option<&str>,
) -> Vec<ParsedRow> {
    crate::codec::typed_rows_to_parsed_data(rows, schema, pk_col)
}

fn engine_row_string(row: &EngineRow, column_name: &str) -> Option<String> {
    row.values
        .iter()
        .find(|(name, _)| name == column_name)
        .and_then(|(_, value)| match value {
            EngineValue::String(value) => Some(value.clone()),
            EngineValue::I64(value) => Some(value.to_string()),
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
        };
        dict.set_item(col_name, py_val)?;
    }
    Ok(dict)
}

async fn postgres_catalog_rows(
    engine: &EngineHandle,
    tx_conn: &Option<TransactionConnection>,
    sql: &str,
    table_name: &str,
    label: &str,
) -> PyResult<Vec<EngineRow>> {
    let values = [EngineBindValue::String(table_name.to_string())];
    let rows = match tx_conn {
        Some(conn_arc) => {
            let mut conn = conn_arc.lock().await;
            conn.fetch_all_sql_with_binds(sql, &values)
                .await
                .map_err(|e| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!(
                        "Failed to inspect {} for '{}': {}",
                        label, table_name, e
                    ))
                })?
        }
        None => engine
            .fetch_all_sql_with_binds(sql, &values)
            .await
            .map_err(|e| {
                pyo3::exceptions::PyRuntimeError::new_err(format!(
                    "Failed to inspect {} for '{}': {}",
                    label, table_name, e
                ))
            })?,
    };

    Ok(rows)
}

macro_rules! sea_query_build_for_backend {
    ($stmt:expr, $backend:expr) => {{
        match $backend {
            crate::state::SqlDialect::Sqlite => $stmt.build(SqliteQueryBuilder),
            crate::state::SqlDialect::Postgres => $stmt.build(PostgresQueryBuilder),
        }
    }};
}

macro_rules! sea_query_to_string_for_backend {
    ($stmt:expr, $backend:expr) => {{
        match $backend {
            crate::state::SqlDialect::Sqlite => $stmt.to_string(SqliteQueryBuilder),
            crate::state::SqlDialect::Postgres => $stmt.to_string(PostgresQueryBuilder),
        }
    }};
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
struct QueryPlanArtifact {
    operation: String,
    semantic_signature: Vec<String>,
    bind_semantics: Vec<String>,
}

fn bind_semantics(bind_values: &[SeaValue]) -> Vec<String> {
    engine_bind_values_from_sea(bind_values)
        .into_iter()
        .map(|value| format!("{value:?}"))
        .collect()
}

fn query_plan_artifact(
    operation: &str,
    query_def: &QueryDef,
    bind_values: &[SeaValue],
) -> QueryPlanArtifact {
    let mut semantic_signature = query_def
        .semantic_signature()
        .where_semantics
        .into_iter()
        .collect::<Vec<_>>();
    semantic_signature.sort();

    QueryPlanArtifact {
        operation: operation.to_string(),
        semantic_signature,
        bind_semantics: bind_semantics(bind_values),
    }
}

fn shadow_artifact_from_ir_roundtrip(
    operation: &str,
    query_def: &QueryDef,
    bind_values: &[SeaValue],
) -> Result<QueryPlanArtifact, String> {
    let ir_payload = query_def.to_ir_payload();
    let ir_roundtrip = query_def_from_ir_payload(ir_payload)?;
    Ok(query_plan_artifact(operation, &ir_roundtrip, bind_values))
}

fn compare_shadow_query_artifacts(
    operation: &str,
    query_def: &QueryDef,
    bind_values: &[SeaValue],
) -> Result<(), String> {
    let legacy = query_plan_artifact(operation, query_def, bind_values);
    let shadow = shadow_artifact_from_ir_roundtrip(operation, query_def, bind_values)?;
    if legacy == shadow {
        return Ok(());
    }
    let legacy_json = serde_json::to_string(&legacy).unwrap_or_else(|_| "<legacy>".to_string());
    let shadow_json = serde_json::to_string(&shadow).unwrap_or_else(|_| "<shadow>".to_string());
    Err(format!(
        "shadow planner mismatch for '{operation}': legacy={legacy_json} shadow={shadow_json}"
    ))
}

fn maybe_compare_shadow_query_artifacts(
    engine: &EngineHandle,
    operation: &str,
    query_def: &QueryDef,
    bind_values: &[SeaValue],
) -> PyResult<()> {
    if !engine.is_shadow_runtime_enabled() {
        return Ok(());
    }
    if let Err(diff) = compare_shadow_query_artifacts(operation, query_def, bind_values) {
        crate::log_debug(format!("⚠️ Ferro shadow runtime mismatch: {diff}"));
        if std::env::var("FERRO_SHADOW_RUNTIME_STRICT")
            .map(|value| value == "1" || value.eq_ignore_ascii_case("true"))
            .unwrap_or(false)
        {
            return Err(pyo3::exceptions::PyRuntimeError::new_err(diff));
        }
    }
    Ok(())
}

/// On Postgres, cast text-like special columns in SELECT output so Python hydration
/// sees the same string representation as SQLite.
fn apply_postgres_text_select_columns(
    select: &mut sea_query::SelectStatement,
    table_name: &str,
    schema: &serde_json::Value,
    pg_native_enum_columns: &HashSet<String>,
    backend: SqlDialect,
) {
    crate::codec::apply_postgres_text_select_columns(
        select,
        table_name,
        schema,
        pg_native_enum_columns,
        backend,
    );
}

/// Maps each table column to its PostgreSQL enum `typname` (``typtype = 'e'``) for the current schema.
async fn postgres_enum_udt_by_column(
    table_name: &str,
    engine: &EngineHandle,
    tx_conn: &Option<TransactionConnection>,
    backend: SqlDialect,
) -> PyResult<HashMap<String, String>> {
    if backend != SqlDialect::Postgres {
        return Ok(HashMap::new());
    }

    let sql = r#"
        SELECT a.attname::text AS column_name, t.typname::text AS udt_name
        FROM pg_attribute a
        JOIN pg_class c ON a.attrelid = c.oid
        JOIN pg_namespace n ON c.relnamespace = n.oid
        JOIN pg_type t ON a.atttypid = t.oid
        WHERE n.nspname = current_schema()
          AND c.relname = $1
          AND t.typtype = 'e'
          AND a.attnum > 0
          AND NOT a.attisdropped
        "#;

    let mut out = HashMap::new();
    for row in postgres_catalog_rows(engine, tx_conn, sql, table_name, "enum columns").await? {
        let column_name = engine_row_string(&row, "column_name").unwrap_or_default();
        let udt_name = engine_row_string(&row, "udt_name").unwrap_or_default();
        if !column_name.is_empty() && !udt_name.is_empty() {
            out.insert(column_name, udt_name);
        }
    }

    Ok(out)
}

/// Column names on `table_name` in the current schema whose SQL type is `uuid`.
async fn postgres_uuid_column_names(
    table_name: &str,
    engine: &EngineHandle,
    tx_conn: &Option<TransactionConnection>,
    backend: SqlDialect,
) -> PyResult<HashSet<String>> {
    if backend != SqlDialect::Postgres {
        return Ok(HashSet::new());
    }

    let sql = r#"
        SELECT column_name::text
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = $1
          AND (data_type = 'uuid' OR udt_name = 'uuid')
        "#;

    Ok(
        postgres_catalog_rows(engine, tx_conn, sql, table_name, "uuid columns")
            .await?
            .into_iter()
            .filter_map(|row| {
                engine_row_string(&row, "column_name").filter(|name| !name.is_empty())
            })
            .collect(),
    )
}

/// For each column whose SQL type is a date or timestamp family, the ``CAST ( … AS … )`` target
/// (``date``, ``timestamp``, ``timestamptz``) so parameters are not sent as untyped text.
async fn postgres_temporal_cast_by_column(
    table_name: &str,
    engine: &EngineHandle,
    tx_conn: &Option<TransactionConnection>,
    backend: SqlDialect,
) -> PyResult<HashMap<String, String>> {
    if backend != SqlDialect::Postgres {
        return Ok(HashMap::new());
    }

    let sql = r#"
        SELECT column_name::text,
               CASE data_type::text
                   WHEN 'timestamp without time zone' THEN 'timestamp'
                   WHEN 'timestamp with time zone' THEN 'timestamptz'
                   WHEN 'date' THEN 'date'
                   ELSE NULL
               END AS cast_type
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = $1
          AND data_type::text IN (
              'timestamp without time zone',
              'timestamp with time zone',
              'date'
          )
        "#;

    let mut out = HashMap::new();
    for row in postgres_catalog_rows(engine, tx_conn, sql, table_name, "temporal columns").await? {
        let column_name = engine_row_string(&row, "column_name").unwrap_or_default();
        let cast_type = engine_row_string(&row, "cast_type").unwrap_or_default();
        if !column_name.is_empty() && !cast_type.is_empty() {
            out.insert(column_name, cast_type);
        }
    }
    Ok(out)
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
    schema: &serde_json::Value,
    table_name: &str,
    col_name: &str,
    value: &serde_json::Value,
    enum_udt: &HashMap<String, String>,
    uuid_columns: &HashSet<String>,
    ts_cast: &HashMap<String, String>,
    backend: SqlDialect,
) -> PyResult<SimpleExpr> {
    crate::codec::schema_bind_expr(
        schema,
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
    backend: SqlDialect,
) -> SimpleExpr {
    crate::codec::m2m_bind_expr(col_name, value, uuid_columns, backend)
}

#[pyfunction]
#[pyo3(signature = (parent_tx_id=None, using=None))]
pub fn begin_transaction(
    py: Python<'_>,
    parent_tx_id: Option<String>,
    using: Option<String>,
) -> PyResult<Bound<'_, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let tx_id = uuid::Uuid::new_v4().to_string();
        if let Some(parent_tx_id) = parent_tx_id {
            if using.is_some() {
                return Err(pyo3::exceptions::PyValueError::new_err(
                    "Nested transactions inherit the parent connection",
                ));
            }

            let parent = TRANSACTION_REGISTRY.get(&parent_tx_id).ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err("Parent transaction not found")
            })?;
            let conn = parent.conn.clone();
            let connection_name = parent.connection_name.clone();
            drop(parent);

            let savepoint_name = format!("sp_{}", tx_id.replace('-', "_"));
            execute_transaction_sql(&conn, &format!("SAVEPOINT {savepoint_name}"))
                .await
                .map_err(|e| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!(
                        "Failed to create SAVEPOINT: {}",
                        e
                    ))
                })?;

            TRANSACTION_REGISTRY.insert(
                tx_id.clone(),
                TransactionHandle::nested(conn, savepoint_name, connection_name),
            );
        } else {
            let (connection_name, engine) = active_connection_for_route(using)?;
            let conn = engine.begin_transaction_connection().await.map_err(|e| {
                pyo3::exceptions::PyRuntimeError::new_err(format!("Failed to BEGIN: {}", e))
            })?;

            TRANSACTION_REGISTRY.insert(
                tx_id.clone(),
                TransactionHandle::root(conn, connection_name),
            );
        }

        Ok(tx_id)
    })
}

#[pyfunction]
pub fn commit_transaction(py: Python<'_>, tx_id: String) -> PyResult<Bound<'_, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let tx_handle = TRANSACTION_REGISTRY
            .remove(&tx_id)
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("Transaction not found"))?
            .1;

        if let Some(savepoint_name) = tx_handle.savepoint_name {
            execute_transaction_sql(
                &tx_handle.conn,
                &format!("RELEASE SAVEPOINT {savepoint_name}"),
            )
            .await
            .map_err(|e| {
                pyo3::exceptions::PyRuntimeError::new_err(format!(
                    "Failed to RELEASE SAVEPOINT: {}",
                    e
                ))
            })?;
        } else {
            execute_transaction_sql(&tx_handle.conn, "COMMIT")
                .await
                .map_err(|e| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!("Failed to COMMIT: {}", e))
                })?;
        }

        Ok(())
    })
}

#[pyfunction]
pub fn transaction_connection_name(tx_id: String) -> PyResult<String> {
    TRANSACTION_REGISTRY
        .get(&tx_id)
        .map(|tx| tx.value().connection_name.clone())
        .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("Transaction not found"))
}

#[pyfunction]
pub fn rollback_transaction(py: Python<'_>, tx_id: String) -> PyResult<Bound<'_, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let tx_handle = TRANSACTION_REGISTRY
            .remove(&tx_id)
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("Transaction not found"))?
            .1;

        if let Some(savepoint_name) = tx_handle.savepoint_name {
            execute_transaction_sql(
                &tx_handle.conn,
                &format!("ROLLBACK TO SAVEPOINT {savepoint_name}"),
            )
            .await
            .map_err(|e| {
                pyo3::exceptions::PyRuntimeError::new_err(format!(
                    "Failed to ROLLBACK TO SAVEPOINT: {}",
                    e
                ))
            })?;
            execute_transaction_sql(
                &tx_handle.conn,
                &format!("RELEASE SAVEPOINT {savepoint_name}"),
            )
            .await
            .map_err(|e| {
                pyo3::exceptions::PyRuntimeError::new_err(format!(
                    "Failed to RELEASE SAVEPOINT: {}",
                    e
                ))
            })?;
        } else {
            execute_transaction_sql(&tx_handle.conn, "ROLLBACK")
                .await
                .map_err(|e| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!("Failed to ROLLBACK: {}", e))
                })?;
        }

        IDENTITY_MAP.clear();
        Ok(())
    })
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
#[pyo3(signature = (cls, tx_id=None, using=None))]
pub fn fetch_all<'py>(
    py: Python<'py>,
    cls: Bound<'py, PyAny>,
    tx_id: Option<String>,
    using: Option<String>,
) -> PyResult<Bound<'py, PyAny>> {
    let name = cls.getattr("__name__")?.extract::<String>()?;
    let cls_py = cls.unbind();

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let (connection_name, engine, tx_conn, backend) = active_route_for_operation(tx_id, using)?;
        let use_identity_map = engine.is_identity_map_enabled();

        let table_name = name.to_lowercase();
        let pg_native_enum_cols: HashSet<String> = {
            let m = postgres_enum_udt_by_column(&table_name, &engine, &tx_conn, backend).await?;
            m.keys().cloned().collect()
        };
        // ... same sql generation ...
        let (sql, pk_col, schema_for_decode) = {
            let registry = MODEL_REGISTRY.read().map_err(|_| {
                pyo3::exceptions::PyRuntimeError::new_err("Failed to lock registry")
            })?;
            let schema = registry.get(&name).ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err(format!("Model '{}' not found", name))
            })?;
            let mut pk = None;
            if let Some(properties) = schema.get("properties").and_then(|p| p.as_object()) {
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
            apply_postgres_text_select_columns(
                &mut stmt,
                &table_name,
                schema,
                &pg_native_enum_cols,
                backend,
            );
            let s = sea_query_to_string_for_backend!(stmt.from(Alias::new(&table_name)), backend);
            (s, pk, schema.clone())
        };

        let parsed_data = match tx_conn {
            Some(conn_arc) => {
                let mut conn = conn_arc.lock().await;
                let rows = conn
                    .fetch_all_sql_with_binds(&sql, &[])
                    .await
                    .map_err(|e| {
                        pyo3::exceptions::PyRuntimeError::new_err(format!("Fetch failed: {}", e))
                    })?;
                typed_rows_to_parsed_data(rows, &schema_for_decode, pk_col.as_deref())
            }
            None => {
                let rows = engine
                    .fetch_all_sql_with_binds(&sql, &[])
                    .await
                    .map_err(|e| {
                        pyo3::exceptions::PyRuntimeError::new_err(format!("Fetch failed: {}", e))
                    })?;
                typed_rows_to_parsed_data(rows, &schema_for_decode, pk_col.as_deref())
            }
        };

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

            for (row_pk_val, fields) in parsed_data {
                if use_identity_map
                    && let Some(ref pk_val) = row_pk_val
                    && let Some(existing_obj) =
                        IDENTITY_MAP.get(&(connection_name.clone(), name.clone(), pk_val.clone()))
                {
                    results.append(existing_obj.value().clone_ref(py))?;
                    continue;
                }

                let instance = crate::hydration::hydrate_model_instance(
                    py,
                    cls,
                    &connection_name,
                    fields,
                    &py_col_names,
                )?;

                if use_identity_map && let Some(pk_val) = row_pk_val {
                    IDENTITY_MAP.insert(
                        (connection_name.clone(), name.clone(), pk_val),
                        instance.clone().unbind(),
                    );
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
#[pyo3(signature = (cls, pk_val, tx_id=None, using=None))]
pub fn fetch_one<'py>(
    py: Python<'py>,
    cls: Bound<'py, PyAny>,
    pk_val: String,
    tx_id: Option<String>,
    using: Option<String>,
) -> PyResult<Bound<'py, PyAny>> {
    let name = cls.getattr("__name__")?.extract::<String>()?;
    let cls_py = cls.unbind();
    let (connection_name, engine) = active_connection_for_route(using.clone())?;

    // Check Identity Map first (if no transaction, or even with transaction, IM is usually safe)
    if engine.is_identity_map_enabled()
        && let Some(existing_obj) =
            IDENTITY_MAP.get(&(connection_name.clone(), name.clone(), pk_val.clone()))
    {
        let obj = existing_obj.value().clone_ref(py);
        return pyo3_async_runtimes::tokio::future_into_py(py, async move { Ok(obj) });
    }

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let (connection_name, engine, tx_conn, backend) = active_route_for_operation(tx_id, using)?;
        let use_identity_map = engine.is_identity_map_enabled();

        let table_name = name.to_lowercase();
        let pg_native_enum_cols: HashSet<String> = {
            let m = postgres_enum_udt_by_column(&table_name, &engine, &tx_conn, backend).await?;
            m.keys().cloned().collect()
        };
        // ... sql logic ...
        let (sql, bind_values, _pk_col_name, schema_for_decode) = {
            let registry = MODEL_REGISTRY.read().map_err(|_| {
                pyo3::exceptions::PyRuntimeError::new_err("Failed to lock registry")
            })?;
            let schema = registry.get(&name).ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err(format!("Model '{}' not found", name))
            })?;
            let mut pk = None;
            if let Some(properties) = schema.get("properties").and_then(|p| p.as_object()) {
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
            let mut stmt = Query::select();
            apply_postgres_text_select_columns(
                &mut stmt,
                &table_name,
                schema,
                &pg_native_enum_cols,
                backend,
            );
            let no_enum_udt = HashMap::new();
            let no_uuid = HashSet::new();
            let no_ts: HashMap<String, String> = HashMap::new();
            let pk_expr = schema_value_expr(
                schema,
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

        let parsed_row = match tx_conn {
            Some(conn_arc) => {
                let engine_bind_values = engine_bind_values_from_sea(&bind_values.0);
                let mut conn = conn_arc.lock().await;
                let rows = conn
                    .fetch_all_sql_with_binds(&sql, &engine_bind_values)
                    .await
                    .map_err(|e| {
                        pyo3::exceptions::PyRuntimeError::new_err(format!("Fetch failed: {}", e))
                    })?;
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
                    .map_err(|e| {
                        pyo3::exceptions::PyRuntimeError::new_err(format!("Fetch failed: {}", e))
                    })?;
                typed_rows_to_parsed_data(rows, &schema_for_decode, None)
                    .into_iter()
                    .next()
                    .map(|(_, fields)| fields)
            }
        };

        match parsed_row {
            Some(fields) => Python::attach(|py| {
                let cls = cls_py.bind(py);
                let py_col_names = HashMap::new();
                let instance = crate::hydration::hydrate_model_instance(
                    py,
                    cls,
                    &connection_name,
                    fields,
                    &py_col_names,
                )?;
                if use_identity_map {
                    IDENTITY_MAP.insert(
                        (connection_name.clone(), name.clone(), pk_val),
                        instance.clone().unbind(),
                    );
                }
                Ok(instance.into_any().unbind())
            }),
            None => Python::attach(|py| Ok(py.None())),
        }
    })
}

/// Persists a model's data to the database.
///
/// Implements an upsert (INSERT ... ON CONFLICT DO UPDATE) strategy.
///
/// Args:
///     name (str): The model name.
///     data (str): Serialized JSON data of the model instance.
///
/// # Errors
/// Returns a `PyErr` if the engine is not initialized or if the save fails.
#[pyfunction]
#[pyo3(signature = (name, data, tx_id=None, using=None))]
pub fn save_record(
    py: Python<'_>,
    name: String,
    data: String,
    tx_id: Option<String>,
    using: Option<String>,
) -> PyResult<Bound<'_, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let (_connection_name, engine, tx_conn, backend) =
            active_route_for_operation(tx_id, using)?;

        // ... schema and record logic ...
        let (schema, record_obj) = {
            let registry = MODEL_REGISTRY.read().map_err(|_| {
                pyo3::exceptions::PyRuntimeError::new_err("Failed to lock registry")
            })?;
            let schema = registry.get(&name).cloned().ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err(format!("Model '{}' not found", name))
            })?;
            let record: serde_json::Value = serde_json::from_str(&data).map_err(|e| {
                pyo3::exceptions::PyValueError::new_err(format!("Invalid JSON: {}", e))
            })?;
            let record_obj = record.as_object().cloned().ok_or_else(|| {
                pyo3::exceptions::PyValueError::new_err("Record must be an object")
            })?;
            (schema, record_obj)
        };

        // ... (keep current sql generation logic) ...
        let mut pk_col = None;
        let mut pk_is_auto = true;
        if let Some(properties) = schema.get("properties").and_then(|p| p.as_object()) {
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

        let table_name = name.to_lowercase();
        let enum_udt = postgres_enum_udt_by_column(&table_name, &engine, &tx_conn, backend).await?;
        let uuid_columns =
            postgres_uuid_column_names(&table_name, &engine, &tx_conn, backend).await?;
        let ts_cast =
            postgres_temporal_cast_by_column(&table_name, &engine, &tx_conn, backend).await?;
        let (sql, bind_values, needs_postgres_returning) = {
            let mut columns = Vec::new();
            let mut values = Vec::new();
            let mut pk_provided = false;
            for (key, value) in &record_obj {
                let is_pk = if let Some(ref pk) = pk_col {
                    key == pk
                } else {
                    false
                };
                if is_pk && pk_is_auto && value.is_null() {
                    continue;
                }
                if is_pk && !value.is_null() {
                    pk_provided = true;
                }
                columns.push(Alias::new(key));
                values.push(schema_value_expr(
                    &schema,
                    &table_name,
                    key,
                    value,
                    &enum_udt,
                    &uuid_columns,
                    &ts_cast,
                    backend,
                )?);
            }
            let mut insert_stmt = InsertStatement::new()
                .into_table(Alias::new(&table_name))
                .columns(columns.clone())
                .values(values)
                .unwrap()
                .to_owned();
            if let Some(pk) = pk_col.as_ref()
                && (pk_provided || !pk_is_auto)
            {
                let mut on_conflict = OnConflict::column(Alias::new(pk));
                let mut update_cols = Vec::new();
                for col in &columns {
                    if col.to_string() != *pk {
                        update_cols.push(col.clone());
                    }
                }
                if !update_cols.is_empty() {
                    on_conflict.update_columns(update_cols);
                    insert_stmt.on_conflict(on_conflict);
                }
            }
            let needs_postgres_returning = backend == crate::state::SqlDialect::Postgres
                && pk_col.is_some()
                && pk_is_auto
                && !pk_provided;
            let (mut sql, values) = sea_query_build_for_backend!(insert_stmt, backend);
            if needs_postgres_returning && let Some(pk) = pk_col.as_ref() {
                sql.push_str(&format!(" RETURNING \"{}\"", pk));
            }
            (sql, values, needs_postgres_returning)
        };

        match tx_conn {
            Some(conn_arc) => {
                let engine_bind_values = engine_bind_values_from_sea(&bind_values.0);
                let mut conn = conn_arc.lock().await;
                if needs_postgres_returning {
                    let rows = conn
                        .fetch_all_sql_with_binds(&sql, &engine_bind_values)
                        .await
                        .map_err(|e| {
                            pyo3::exceptions::PyRuntimeError::new_err(format!("Save failed: {}", e))
                        })?;
                    let id = rows
                        .first()
                        .and_then(|row| row.values.first())
                        .and_then(|(_, value)| value.as_i64())
                        .unwrap_or(0);
                    Ok((id > 0).then_some(id))
                } else {
                    let exec_res = conn
                        .execute_sql_with_binds_result(&sql, &engine_bind_values)
                        .await
                        .map_err(|e| {
                            pyo3::exceptions::PyRuntimeError::new_err(format!("Save failed: {}", e))
                        })?;
                    Ok(exec_res.last_insert_id)
                }
            }
            None => {
                let engine_bind_values = engine_bind_values_from_sea(&bind_values.0);
                if needs_postgres_returning {
                    let rows = engine
                        .fetch_all_sql_with_binds(&sql, &engine_bind_values)
                        .await
                        .map_err(|e| {
                            pyo3::exceptions::PyRuntimeError::new_err(format!("Save failed: {}", e))
                        })?;
                    let id = rows
                        .first()
                        .and_then(|row| row.values.first())
                        .and_then(|(_, value)| value.as_i64())
                        .unwrap_or(0);
                    Ok((id > 0).then_some(id))
                } else {
                    let exec_res = engine
                        .execute_sql_with_binds_result(&sql, &engine_bind_values)
                        .await
                        .map_err(|e| {
                            pyo3::exceptions::PyRuntimeError::new_err(format!("Save failed: {}", e))
                        })?;
                    Ok(exec_res.last_insert_id)
                }
            }
        }
    })
}

/// Persists multiple model instances in a single batch operation.
#[pyfunction]
#[pyo3(signature = (name, data_list_json, tx_id=None, using=None))]
pub fn save_bulk_records(
    py: Python<'_>,
    name: String,
    data_list_json: String,
    tx_id: Option<String>,
    using: Option<String>,
) -> PyResult<Bound<'_, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let (_connection_name, engine, tx_conn, backend) =
            active_route_for_operation(tx_id, using)?;

        let schema = {
            let registry = MODEL_REGISTRY.read().map_err(|_| {
                pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Model Registry")
            })?;
            registry.get(&name).cloned().ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err(format!(
                    "Model '{}' not found in registry",
                    name
                ))
            })?
        };

        let records: Vec<serde_json::Value> =
            serde_json::from_str(&data_list_json).map_err(|e| {
                pyo3::exceptions::PyValueError::new_err(format!("Invalid bulk record JSON: {}", e))
            })?;

        if records.is_empty() {
            return Ok(0);
        }

        let mut pk_col = None;
        let mut pk_is_auto = true;
        if let Some(properties) = schema.get("properties").and_then(|p| p.as_object()) {
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

        let table_name = name.to_lowercase();
        let enum_udt = postgres_enum_udt_by_column(&table_name, &engine, &tx_conn, backend).await?;
        let uuid_columns =
            postgres_uuid_column_names(&table_name, &engine, &tx_conn, backend).await?;
        let ts_cast =
            postgres_temporal_cast_by_column(&table_name, &engine, &tx_conn, backend).await?;
        let (sql, bind_values) = {
            let mut insert_stmt = InsertStatement::new()
                .into_table(Alias::new(&table_name))
                .to_owned();

            let mut column_names = Vec::new();
            for (i, record) in records.iter().enumerate() {
                let record_obj = record.as_object().ok_or_else(|| {
                    pyo3::exceptions::PyValueError::new_err("Each record must be a JSON object")
                })?;

                let mut row_values = Vec::new();
                if i == 0 {
                    for (key, value) in record_obj {
                        let is_pk = if let Some(ref pk) = pk_col {
                            key == pk
                        } else {
                            false
                        };
                        if is_pk && pk_is_auto && value.is_null() {
                            continue;
                        }
                        column_names.push(Alias::new(key));
                    }
                    insert_stmt.columns(column_names.clone());
                }

                for key in &column_names {
                    let value = record_obj
                        .get(key.to_string().as_str())
                        .unwrap_or(&serde_json::Value::Null);
                    row_values.push(schema_value_expr(
                        &schema,
                        &table_name,
                        key.to_string().as_str(),
                        value,
                        &enum_udt,
                        &uuid_columns,
                        &ts_cast,
                        backend,
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
            execute_statement_with_optional_tx(&engine, tx_conn, &sql, &bind_values.0)
                .await
                .map_err(|e| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!(
                        "Bulk save failed for '{}': {}",
                        name, e
                    ))
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
#[pyo3(signature = (cls, query_ir_json, tx_id=None, using=None))]
pub fn fetch_filtered<'py>(
    py: Python<'py>,
    cls: Bound<'py, PyAny>,
    query_ir_json: String,
    tx_id: Option<String>,
    using: Option<String>,
) -> PyResult<Bound<'py, PyAny>> {
    let name = cls.getattr("__name__")?.extract::<String>()?;
    let cls_py = cls.unbind();

    let mut query_def = query_def_from_ir_json(&query_ir_json)?;

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let (connection_name, engine, tx_conn, backend) = active_route_for_operation(tx_id, using)?;
        let use_identity_map = engine.is_identity_map_enabled();

        let table_name = name.to_lowercase();
        let postgres_enum_udt =
            postgres_enum_udt_by_column(&table_name, &engine, &tx_conn, backend).await?;
        query_def.postgres_enum_udt = postgres_enum_udt.clone();
        let pg_native_enum_cols: HashSet<String> = postgres_enum_udt.keys().cloned().collect();
        // ...
        let (sql, bind_values, pk_col, schema_for_decode) = {
            let registry = MODEL_REGISTRY.read().map_err(|_| {
                pyo3::exceptions::PyRuntimeError::new_err("Failed to lock registry")
            })?;
            let schema = registry.get(&name).ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err(format!("Model '{}' not found", name))
            })?;
            let mut pk = None;
            if let Some(properties) = schema.get("properties").and_then(|p| p.as_object()) {
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
            apply_postgres_text_select_columns(
                &mut select,
                &table_name,
                schema,
                &pg_native_enum_cols,
                backend,
            );
            select.from(Alias::new(&table_name));

            if let Some(m2m) = &query_def.m2m {
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
                    query_def.value_rhs_simple_expr_for_backend(
                        &m2m.source_col,
                        &m2m.source_id,
                        true,
                        backend,
                    ),
                ));
            }

            select.cond_where(query_def.to_condition_for_backend(backend));
            if let Some(ref orders) = query_def.order_by {
                for order in orders {
                    let col = Alias::new(&order.column);
                    let dir = if order.direction.to_lowercase() == "desc" {
                        Order::Desc
                    } else {
                        Order::Asc
                    };
                    select.order_by(col, dir);
                }
            }
            if let Some(limit) = query_def.limit {
                select.limit(limit);
            }
            if let Some(offset) = query_def.offset {
                select.offset(offset);
            }
            let (s, values) = sea_query_build_for_backend!(select, backend);
            (s, values, pk, schema.clone())
        };
        maybe_compare_shadow_query_artifacts(
            &engine,
            "fetch_filtered",
            &query_def,
            &bind_values.0,
        )?;

        let parsed_data = match tx_conn {
            Some(conn_arc) => {
                let engine_bind_values = engine_bind_values_from_sea(&bind_values.0);
                let mut conn = conn_arc.lock().await;
                let rows = conn
                    .fetch_all_sql_with_binds(&sql, &engine_bind_values)
                    .await
                    .map_err(|e| {
                        pyo3::exceptions::PyRuntimeError::new_err(format!("Fetch failed: {}", e))
                    })?;
                typed_rows_to_parsed_data(rows, &schema_for_decode, pk_col.as_deref())
            }
            None => {
                let engine_bind_values = engine_bind_values_from_sea(&bind_values.0);
                let rows = engine
                    .fetch_all_sql_with_binds(&sql, &engine_bind_values)
                    .await
                    .map_err(|e| {
                        pyo3::exceptions::PyRuntimeError::new_err(format!("Fetch failed: {}", e))
                    })?;
                typed_rows_to_parsed_data(rows, &schema_for_decode, pk_col.as_deref())
            }
        };

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

            for (row_pk_val, fields) in parsed_data {
                if use_identity_map
                    && let Some(ref pk_val) = row_pk_val
                    && let Some(existing_obj) =
                        IDENTITY_MAP.get(&(connection_name.clone(), name.clone(), pk_val.clone()))
                {
                    results.append(existing_obj.value().clone_ref(py))?;
                    continue;
                }

                let instance = crate::hydration::hydrate_model_instance(
                    py,
                    cls,
                    &connection_name,
                    fields,
                    &py_col_names,
                )?;

                if use_identity_map && let Some(pk_val) = row_pk_val {
                    IDENTITY_MAP.insert(
                        (connection_name.clone(), name.clone(), pk_val),
                        instance.clone().unbind(),
                    );
                }

                results.append(instance)?;
            }
            Ok(results.into_any().unbind())
        })
    })
}

/// Returns the number of records matching a filtered query.
#[pyfunction]
#[pyo3(signature = (name, query_ir_json, tx_id=None, using=None))]
pub fn count_filtered(
    py: Python<'_>,
    name: String,
    query_ir_json: String,
    tx_id: Option<String>,
    using: Option<String>,
) -> PyResult<Bound<'_, PyAny>> {
    let mut query_def = query_def_from_ir_json(&query_ir_json)?;

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let (_, engine, tx_conn, backend) = active_route_for_operation(tx_id, using)?;

        let table_name = name.to_lowercase();
        query_def.postgres_enum_udt =
            postgres_enum_udt_by_column(&table_name, &engine, &tx_conn, backend).await?;
        // ... sql ...
        let (sql, bind_values) = {
            let mut select = Query::select();
            select.expr(Expr::cust("COUNT(*)"));

            if let Some(m2m) = &query_def.m2m {
                let join_table = Alias::new(&m2m.join_table);
                let source_col = Alias::new(&m2m.source_col);
                let target_col = Alias::new(&m2m.target_col);

                // We need the PK name of the target table to join
                let registry = MODEL_REGISTRY.read().map_err(|_| {
                    pyo3::exceptions::PyRuntimeError::new_err("Failed to lock registry")
                })?;
                let schema = registry.get(&name).ok_or_else(|| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!("Model '{}' not found", name))
                })?;
                let mut pk = None;
                if let Some(properties) = schema.get("properties").and_then(|p| p.as_object()) {
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

                select.from(Alias::new(&table_name));
                select.inner_join(
                    join_table.clone(),
                    Expr::col((Alias::new(&table_name), Alias::new(pk_name)))
                        .equals((join_table.clone(), target_col.clone())),
                );
                select.and_where(Expr::col((join_table.clone(), source_col.clone())).eq(
                    query_def.value_rhs_simple_expr_for_backend(
                        &m2m.source_col,
                        &m2m.source_id,
                        true,
                        backend,
                    ),
                ));
            } else {
                select.from(Alias::new(&table_name));
            }

            select.cond_where(query_def.to_condition_for_backend(backend));
            sea_query_build_for_backend!(select, backend)
        };
        maybe_compare_shadow_query_artifacts(
            &engine,
            "count_filtered",
            &query_def,
            &bind_values.0,
        )?;

        let engine_bind_values = engine_bind_values_from_sea(&bind_values.0);
        let count = match tx_conn {
            Some(conn_arc) => {
                let mut conn = conn_arc.lock().await;
                let rows = conn
                    .fetch_all_sql_with_binds(&sql, &engine_bind_values)
                    .await
                    .map_err(|e| {
                        pyo3::exceptions::PyRuntimeError::new_err(format!("Count failed: {}", e))
                    })?;
                rows.first()
                    .and_then(|row| row.values.first())
                    .and_then(|(_, value)| value.as_i64())
                    .unwrap_or(0)
            }
            None => {
                let rows = engine
                    .fetch_all_sql_with_binds(&sql, &engine_bind_values)
                    .await
                    .map_err(|e| {
                        pyo3::exceptions::PyRuntimeError::new_err(format!("Count failed: {}", e))
                    })?;
                rows.first()
                    .and_then(|row| row.values.first())
                    .and_then(|(_, value)| value.as_i64())
                    .unwrap_or(0)
            }
        };

        Ok(count)
    })
}

/// Registers a live Python object in the global Identity Map.
#[pyfunction]
#[pyo3(signature = (name, pk, obj, using=None))]
pub fn register_instance(
    name: String,
    pk: String,
    obj: Py<PyAny>,
    using: Option<String>,
) -> PyResult<()> {
    let (connection_name, engine) = active_connection_for_route(using)?;
    if engine.is_identity_map_enabled() {
        IDENTITY_MAP.insert((connection_name, name, pk), obj);
    }
    Ok(())
}

/// Evicts a specific model instance from the global Identity Map.
#[pyfunction]
#[pyo3(signature = (name, pk, using=None))]
pub fn evict_instance(name: String, pk: String, using: Option<String>) -> PyResult<()> {
    let (connection_name, engine) = active_connection_for_route(using)?;
    if engine.is_identity_map_enabled() {
        IDENTITY_MAP.remove(&(connection_name, name, pk));
    }
    Ok(())
}

/// Deletes a record by its primary key.
#[pyfunction]
#[pyo3(signature = (name, pk_val, tx_id=None, using=None))]
pub fn delete_record(
    py: Python<'_>,
    name: String,
    pk_val: String,
    tx_id: Option<String>,
    using: Option<String>,
) -> PyResult<Bound<'_, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let (_, engine, tx_conn, backend) = active_route_for_operation(tx_id, using)?;

        let table_name = name.to_lowercase();
        // ... sql ...
        let (sql, bind_values) = {
            let registry = MODEL_REGISTRY.read().map_err(|_| {
                pyo3::exceptions::PyRuntimeError::new_err("Failed to lock registry")
            })?;
            let schema = registry.get(&name).ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err(format!("Model '{}' not found", name))
            })?;
            let mut pk = None;
            if let Some(properties) = schema.get("properties").and_then(|p| p.as_object()) {
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
            let no_enum_udt = HashMap::new();
            let no_uuid = HashSet::new();
            let no_ts: HashMap<String, String> = HashMap::new();
            let pk_expr = schema_value_expr(
                schema,
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

        execute_statement_with_optional_tx(&engine, tx_conn, &sql, &bind_values.0)
            .await
            .map_err(|e| {
                pyo3::exceptions::PyRuntimeError::new_err(format!("Delete failed: {}", e))
            })?;

        Ok(true)
    })
}

/// Deletes records matching a filtered query.
#[pyfunction]
#[pyo3(signature = (name, query_ir_json, tx_id=None, using=None))]
pub fn delete_filtered(
    py: Python<'_>,
    name: String,
    query_ir_json: String,
    tx_id: Option<String>,
    using: Option<String>,
) -> PyResult<Bound<'_, PyAny>> {
    let mut query_def = query_def_from_ir_json(&query_ir_json)?;

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let (_, engine, tx_conn, backend) = active_route_for_operation(tx_id, using)?;

        let table_name = name.to_lowercase();
        query_def.postgres_enum_udt =
            postgres_enum_udt_by_column(&table_name, &engine, &tx_conn, backend).await?;
        // ... sql ...
        let (sql, bind_values) = {
            let mut delete = Query::delete();
            delete
                .from_table(Alias::new(&table_name))
                .cond_where(query_def.to_condition_for_backend(backend));
            sea_query_build_for_backend!(delete, backend)
        };
        maybe_compare_shadow_query_artifacts(
            &engine,
            "delete_filtered",
            &query_def,
            &bind_values.0,
        )?;

        let rows_affected =
            execute_statement_with_optional_tx(&engine, tx_conn, &sql, &bind_values.0)
                .await
                .map_err(|e| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!("Delete failed: {}", e))
                })?;

        // After bulk delete, we MUST clear the Identity Map for this model to avoid stale objects
        if engine.is_identity_map_enabled() {
            IDENTITY_MAP.retain(|(_, m_name, _), _| m_name != &name);
        }

        Ok(rows_affected)
    })
}

/// Updates records matching a filtered query with provided values.
#[pyfunction]
#[pyo3(signature = (name, query_ir_json, update_json, tx_id=None, using=None))]
pub fn update_filtered(
    py: Python<'_>,
    name: String,
    query_ir_json: String,
    update_json: String,
    tx_id: Option<String>,
    using: Option<String>,
) -> PyResult<Bound<'_, PyAny>> {
    let mut query_def = query_def_from_ir_json(&query_ir_json)?;

    let update_values: serde_json::Value = serde_json::from_str(&update_json).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("Invalid update JSON: {}", e))
    })?;

    let update_map = update_values.as_object().cloned().ok_or_else(|| {
        pyo3::exceptions::PyValueError::new_err("Update values must be a JSON object")
    })?;

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let (_, engine, tx_conn, backend) = active_route_for_operation(tx_id, using)?;

        let table_name = name.to_lowercase();
        let enum_udt = postgres_enum_udt_by_column(&table_name, &engine, &tx_conn, backend).await?;
        query_def.postgres_enum_udt = enum_udt.clone();
        let uuid_columns =
            postgres_uuid_column_names(&table_name, &engine, &tx_conn, backend).await?;
        let ts_cast =
            postgres_temporal_cast_by_column(&table_name, &engine, &tx_conn, backend).await?;
        // ... sql ...
        let (sql, bind_values) = {
            let registry = MODEL_REGISTRY.read().map_err(|_| {
                pyo3::exceptions::PyRuntimeError::new_err("Failed to lock registry")
            })?;
            let schema = registry.get(&name).ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err(format!("Model '{}' not found", name))
            })?;
            let mut update = UpdateStatement::new()
                .table(Alias::new(&table_name))
                .cond_where(query_def.to_condition_for_backend(backend))
                .to_owned();
            for (key, value) in update_map {
                update.value(
                    Alias::new(&key),
                    schema_value_expr(
                        schema,
                        &table_name,
                        &key,
                        &value,
                        &enum_udt,
                        &uuid_columns,
                        &ts_cast,
                        backend,
                    )?,
                );
            }
            sea_query_build_for_backend!(update, backend)
        };
        maybe_compare_shadow_query_artifacts(
            &engine,
            "update_filtered",
            &query_def,
            &bind_values.0,
        )?;

        let rows_affected =
            execute_statement_with_optional_tx(&engine, tx_conn, &sql, &bind_values.0)
                .await
                .map_err(|e| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!("Update failed: {}", e))
                })?;

        // After bulk update, we MUST clear the Identity Map for this model to avoid stale objects
        if engine.is_identity_map_enabled() {
            IDENTITY_MAP.retain(|(_, m_name, _), _| m_name != &name);
        }

        Ok(rows_affected)
    })
}

#[pyfunction]
#[pyo3(signature = (join_table, source_col, target_col, source_id, target_ids, tx_id=None, using=None))]
#[allow(clippy::too_many_arguments)]
pub fn add_m2m_links<'py>(
    py: Python<'py>,
    join_table: String,
    source_col: String,
    target_col: String,
    source_id: Bound<'py, PyAny>,
    target_ids: Vec<Bound<'py, PyAny>>,
    tx_id: Option<String>,
    using: Option<String>,
) -> PyResult<Bound<'py, PyAny>> {
    let s_id = python_to_sea_value(source_id)?;
    let t_ids: Vec<sea_query::Value> = target_ids
        .into_iter()
        .map(|id| python_to_sea_value(id))
        .collect::<PyResult<Vec<_>>>()?;

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let (_, engine, tx_conn, backend) = active_route_for_operation(tx_id, using)?;
        let uuid_columns =
            postgres_uuid_column_names(&join_table, &engine, &tx_conn, backend).await?;

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
                            &uuid_columns,
                            backend,
                        ),
                        backend_column_value_expr(&target_col, t_id, &uuid_columns, backend),
                    ])
                    .unwrap();
            }
            sea_query_build_for_backend!(insert, backend)
        };

        execute_statement_with_optional_tx(&engine, tx_conn, &sql, &bind_values.0)
            .await
            .map_err(|e| {
                pyo3::exceptions::PyRuntimeError::new_err(format!("Add M2M links failed: {}", e))
            })?;

        Ok(())
    })
}

#[pyfunction]
#[pyo3(signature = (join_table, source_col, target_col, source_id, target_ids, tx_id=None, using=None))]
#[allow(clippy::too_many_arguments)]
pub fn remove_m2m_links<'py>(
    py: Python<'py>,
    join_table: String,
    source_col: String,
    target_col: String,
    source_id: Bound<'py, PyAny>,
    target_ids: Vec<Bound<'py, PyAny>>,
    tx_id: Option<String>,
    using: Option<String>,
) -> PyResult<Bound<'py, PyAny>> {
    let s_id = python_to_sea_value(source_id)?;
    let t_ids: Vec<sea_query::Value> = target_ids
        .into_iter()
        .map(|id| python_to_sea_value(id))
        .collect::<PyResult<Vec<_>>>()?;

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let (_, engine, tx_conn, backend) = active_route_for_operation(tx_id, using)?;
        let uuid_columns =
            postgres_uuid_column_names(&join_table, &engine, &tx_conn, backend).await?;

        let (sql, bind_values) = sea_query_build_for_backend!(
            Query::delete()
                .from_table(Alias::new(&join_table))
                .and_where(
                    Expr::col(Alias::new(&source_col)).eq(backend_column_value_expr(
                        &source_col,
                        s_id,
                        &uuid_columns,
                        backend
                    ))
                )
                .and_where(
                    Expr::col(Alias::new(&target_col)).is_in(
                        t_ids
                            .into_iter()
                            .map(|t_id| {
                                backend_column_value_expr(&target_col, t_id, &uuid_columns, backend)
                            })
                            .collect::<Vec<_>>()
                    )
                ),
            backend
        );

        execute_statement_with_optional_tx(&engine, tx_conn, &sql, &bind_values.0)
            .await
            .map_err(|e| {
                pyo3::exceptions::PyRuntimeError::new_err(format!("Remove M2M links failed: {}", e))
            })?;

        Ok(())
    })
}

#[pyfunction]
#[pyo3(signature = (join_table, source_col, source_id, tx_id=None, using=None))]
pub fn clear_m2m_links<'py>(
    py: Python<'py>,
    join_table: String,
    source_col: String,
    source_id: Bound<'py, PyAny>,
    tx_id: Option<String>,
    using: Option<String>,
) -> PyResult<Bound<'py, PyAny>> {
    let s_id = python_to_sea_value(source_id)?;

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let (engine, tx_conn, backend) = {
            let engine = active_engine_for_connection(using)?;
            let backend = engine.backend();
            let tx_conn = get_transaction_connection(tx_id);
            (engine, tx_conn, backend)
        };
        let uuid_columns =
            postgres_uuid_column_names(&join_table, &engine, &tx_conn, backend).await?;

        let (sql, bind_values) = sea_query_build_for_backend!(
            Query::delete()
                .from_table(Alias::new(&join_table))
                .and_where(
                    Expr::col(Alias::new(&source_col)).eq(backend_column_value_expr(
                        &source_col,
                        s_id,
                        &uuid_columns,
                        backend
                    ))
                ),
            backend
        );

        execute_statement_with_optional_tx(&engine, tx_conn, &sql, &bind_values.0)
            .await
            .map_err(|e| {
                pyo3::exceptions::PyRuntimeError::new_err(format!("Clear M2M links failed: {}", e))
            })?;

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
fn get_raw_tx_conn(tx_id: Option<String>) -> PyResult<Option<TransactionConnection>> {
    match tx_id {
        Some(id) => {
            let conn = TRANSACTION_REGISTRY
                .get(&id)
                .map(|tx| tx.value().conn.clone())
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
#[pyo3(signature = (sql, args, tx_id=None, using=None))]
pub fn raw_execute<'py>(
    py: Python<'py>,
    sql: String,
    args: Vec<Bound<'py, PyAny>>,
    tx_id: Option<String>,
    using: Option<String>,
) -> PyResult<Bound<'py, PyAny>> {
    let bind_values: Vec<EngineBindValue> = args
        .iter()
        .map(python_to_engine_bind_value)
        .collect::<PyResult<_>>()?;
    let tx_conn = get_raw_tx_conn(tx_id)?;

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let rows_affected = match tx_conn {
            Some(conn_arc) => {
                let mut conn = conn_arc.lock().await;
                conn.execute_sql_with_binds(&sql, &bind_values).await
            }
            None => {
                let engine = active_engine_for_connection(using)?;
                engine.execute_sql_with_binds(&sql, &bind_values).await
            }
        }
        .map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("Raw SQL execute failed: {e}"))
        })?;

        Ok(rows_affected as i64)
    })
}

/// Run a raw SQL query and return all rows as a list of dicts.
///
/// Values are wire-close primitives (`str | int | float | bool | bytes | None`).
/// UUID/datetime/JSON columns come back as strings — Ferro does not decode them
/// for raw SQL. If you want typed rows, use the ORM.
#[pyfunction]
#[pyo3(signature = (sql, args, tx_id=None, using=None))]
pub fn raw_fetch_all<'py>(
    py: Python<'py>,
    sql: String,
    args: Vec<Bound<'py, PyAny>>,
    tx_id: Option<String>,
    using: Option<String>,
) -> PyResult<Bound<'py, PyAny>> {
    let bind_values: Vec<EngineBindValue> = args
        .iter()
        .map(python_to_engine_bind_value)
        .collect::<PyResult<_>>()?;
    let tx_conn = get_raw_tx_conn(tx_id)?;

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let rows = match tx_conn {
            Some(conn_arc) => {
                let mut conn = conn_arc.lock().await;
                conn.fetch_all_sql_with_binds(&sql, &bind_values).await
            }
            None => {
                let engine = active_engine_for_connection(using)?;
                engine.fetch_all_sql_with_binds(&sql, &bind_values).await
            }
        }
        .map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("Raw SQL fetch_all failed: {e}"))
        })?;

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
#[pyfunction]
#[pyo3(signature = (sql, args, tx_id=None, using=None))]
pub fn raw_fetch_one<'py>(
    py: Python<'py>,
    sql: String,
    args: Vec<Bound<'py, PyAny>>,
    tx_id: Option<String>,
    using: Option<String>,
) -> PyResult<Bound<'py, PyAny>> {
    let bind_values: Vec<EngineBindValue> = args
        .iter()
        .map(python_to_engine_bind_value)
        .collect::<PyResult<_>>()?;
    let tx_conn = get_raw_tx_conn(tx_id)?;

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let rows = match tx_conn {
            Some(conn_arc) => {
                let mut conn = conn_arc.lock().await;
                conn.fetch_all_sql_with_binds(&sql, &bind_values).await
            }
            None => {
                let engine = active_engine_for_connection(using)?;
                engine.fetch_all_sql_with_binds(&sql, &bind_values).await
            }
        }
        .map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("Raw SQL fetch_one failed: {e}"))
        })?;

        Python::attach(|py| match rows.into_iter().next() {
            Some(row) => Ok(engine_row_to_pydict(py, row)?.into_any().unbind()),
            None => Ok(py.None()),
        })
    })
}

#[pyfunction]
#[pyo3(name = "_shadow_compare_query_plan_for_test")]
#[pyo3(signature = (query_payload_json, dialect, operation="select".to_string()))]
pub fn _shadow_compare_query_plan_for_test(
    query_payload_json: String,
    dialect: String,
    operation: String,
) -> PyResult<String> {
    let backend = match dialect.as_str() {
        "postgres" => SqlDialect::Postgres,
        "sqlite" => SqlDialect::Sqlite,
        other => {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "Unknown dialect {:?}; expected 'postgres' or 'sqlite'",
                other
            )));
        }
    };
    let query_def: QueryDef = if let Ok(ir_envelope) =
        serde_json::from_str::<IrEnvelope<QueryIrPayload>>(&query_payload_json)
    {
        if ir_envelope.ir_kind != "query" {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "Invalid QueryIR envelope kind {:?}; expected \"query\"",
                ir_envelope.ir_kind
            )));
        }
        if ir_envelope.ir_version != 1 {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "Unsupported QueryIR version {}; expected 1",
                ir_envelope.ir_version
            )));
        }
        query_def_from_ir_payload(ir_envelope.payload).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("Invalid QueryIR payload: {e}"))
        })?
    } else {
        serde_json::from_str(&query_payload_json).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("Invalid query payload JSON: {}", e))
        })?
    };
    let mut select_legacy = Query::select();
    select_legacy.from(Alias::new(query_def.model_name.to_lowercase()));
    select_legacy.column((
        Alias::new(query_def.model_name.to_lowercase()),
        sea_query::Asterisk,
    ));
    select_legacy.cond_where(query_def.to_condition_for_backend(backend));
    if let Some(ref orders) = query_def.order_by {
        for order in orders {
            let dir = if order.direction.to_lowercase() == "desc" {
                Order::Desc
            } else {
                Order::Asc
            };
            select_legacy.order_by(Alias::new(&order.column), dir);
        }
    }
    if let Some(limit) = query_def.limit {
        select_legacy.limit(limit);
    }
    if let Some(offset) = query_def.offset {
        select_legacy.offset(offset);
    }
    let (legacy_sql, legacy_values) = sea_query_build_for_backend!(select_legacy, backend);
    let legacy = query_plan_artifact(&operation, &query_def, &legacy_values.0);

    let shadow = shadow_artifact_from_ir_roundtrip(&operation, &query_def, &legacy_values.0)
        .map_err(pyo3::exceptions::PyRuntimeError::new_err)?;
    let payload = serde_json::json!({
        "matches": legacy == shadow,
        "legacy": {
            "sql": legacy_sql,
            "artifact": legacy,
        },
        "shadow": {
            "artifact": shadow,
        },
    });
    serde_json::to_string(&payload).map_err(|e| {
        pyo3::exceptions::PyRuntimeError::new_err(format!("Failed to encode JSON: {e}"))
    })
}

#[cfg(test)]
mod m2m_value_tests {
    use super::{backend_column_value_expr, python_to_sea_value};
    use crate::state::SqlDialect;
    use pyo3::Python;
    use sea_query::{Alias, PostgresQueryBuilder, Query, SqliteQueryBuilder, Value as SeaValue};
    use std::collections::HashSet;

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
        let expr = backend_column_value_expr("user_id", value, &uuid_cols, SqlDialect::Postgres);

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
            SqlDialect::Postgres,
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
        let expr = backend_column_value_expr("user_id", value, &uuid_cols, SqlDialect::Sqlite);

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
        let expr = backend_column_value_expr("user_id", value, &uuid_cols, SqlDialect::Postgres);
        let sql = Query::select().expr(expr).to_string(PostgresQueryBuilder);
        assert!(
            sql.contains("AS uuid"),
            "fallback CAST expected for unparseable UUID: {sql}"
        );
    }
}

#[cfg(test)]
mod schema_value_expr_tests {
    use super::schema_value_expr;
    use crate::state::SqlDialect;
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
        let expr = schema_value_expr(
            schema,
            table,
            col,
            value,
            &enum_udt,
            &uuid_columns,
            &ts_cast,
            SqlDialect::Postgres,
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
        let schema = serde_json::json!({
            "properties": {
                "amount": {
                    "anyOf": [
                        {"type": "string", "pattern": "^-?\\d+(\\.\\d+)?$"},
                        {"type": "null"}
                    ]
                }
            }
        });
        let (_, values) = build_pg_value(
            &schema,
            "thing",
            "amount",
            &serde_json::Value::Null,
            HashSet::new(),
        )
        .unwrap();

        // Decimal binds as float8-typed null; native numeric is deferred.
        assert!(matches!(values.0.as_slice(), [SeaValue::Double(None)]));
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
                &schema,
                "thing",
                "id",
                &serde_json::Value::String("not-a-uuid".to_string()),
                &HashMap::new(),
                &HashSet::new(),
                &HashMap::new(),
                SqlDialect::Postgres,
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
            &schema,
            "thing",
            "created_at",
            &serde_json::Value::Null,
            &HashMap::new(),
            &HashSet::new(),
            &ts_cast,
            SqlDialect::Postgres,
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
            &schema,
            "thing",
            "id",
            &serde_json::Value::String(uuid_str.to_string()),
            &HashMap::new(),
            &HashSet::new(),
            &HashMap::new(),
            SqlDialect::Sqlite,
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
