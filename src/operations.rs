//! Core database operations for Ferro models.
//!
//! This module implements high-performance CRUD operations, leveraging
//! GIL-free parsing and zero-copy Direct Injection into Python objects.

use crate::backend::{EngineBindValue, EngineHandle, EngineRow, EngineValue};
use crate::query::QueryDef;
use crate::state::{
    IDENTITY_MAP, MODEL_REGISTRY, RustValue, SqlDialect, TRANSACTION_REGISTRY,
    TransactionConnection, TransactionHandle, engine_handle,
};
use pyo3::prelude::*;
use sea_query::{
    Alias, Expr, Iden, InsertStatement, OnConflict, Order, PostgresQueryBuilder, Query, SimpleExpr,
    SqliteQueryBuilder, UpdateStatement, Value as SeaValue,
};
use std::collections::{HashMap, HashSet};
use std::sync::Arc;

fn get_transaction_connection(tx_id: Option<String>) -> Option<TransactionConnection> {
    tx_id.and_then(|id| {
        TRANSACTION_REGISTRY
            .get(&id)
            .map(|tx| tx.value().conn.clone())
    })
}

fn active_engine() -> PyResult<Arc<EngineHandle>> {
    let engine = engine_handle()
        .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("Engine not initialized"))?;
    Ok(engine)
}

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
            _ => EngineBindValue::Null,
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

fn engine_value_to_rust_value(
    value: EngineValue,
    schema: &serde_json::Value,
    col_name: &str,
) -> RustValue {
    let prop = schema
        .get("properties")
        .and_then(|p| p.get(col_name))
        .map(|col_info| resolve_ref(schema, col_info));

    let format = prop.and_then(property_format);
    let is_decimal = prop
        .and_then(|p| p.get("anyOf"))
        .and_then(|a| a.as_array())
        .map(|types| {
            let has_number = types
                .iter()
                .any(|t| t.get("type").and_then(|ty| ty.as_str()) == Some("number"));
            let has_patterned_string = types.iter().any(|t| {
                t.get("type").and_then(|ty| ty.as_str()) == Some("string")
                    && t.get("pattern").is_some()
            });
            has_number && has_patterned_string
        })
        .unwrap_or(false);
    let json_type = prop.and_then(property_json_type);

    if is_decimal {
        return match value {
            EngineValue::F64(v) => RustValue::Decimal(v.to_string()),
            EngineValue::String(v) => RustValue::Decimal(v),
            _ => RustValue::None,
        };
    }

    if format == Some("binary") {
        return match value {
            EngineValue::Bytes(v) => RustValue::Blob(v),
            EngineValue::String(v) => RustValue::Blob(v.into_bytes()),
            _ => RustValue::None,
        };
    }

    match value {
        EngineValue::I64(v) if json_type == Some("boolean") => RustValue::Bool(v != 0),
        EngineValue::I64(v) => RustValue::BigInt(v),
        EngineValue::F64(v) => RustValue::Double(v),
        EngineValue::Bytes(v) => RustValue::Blob(v),
        EngineValue::String(v) => match (json_type, format) {
            (_, Some("date-time")) => RustValue::DateTime(v),
            (_, Some("date")) => RustValue::Date(v),
            (_, Some("uuid")) => RustValue::Uuid(v),
            (Some("object"), _) | (Some("array"), _) => {
                if let Ok(json_val) = serde_json::from_str(&v) {
                    RustValue::Json(json_val)
                } else {
                    RustValue::String(v)
                }
            }
            _ => RustValue::String(v),
        },
        EngineValue::Bool(v) => RustValue::Bool(v),
        EngineValue::Null => RustValue::None,
    }
}

fn typed_rows_to_parsed_data(
    rows: Vec<EngineRow>,
    schema: &serde_json::Value,
    pk_col: Option<&str>,
) -> Vec<(Option<String>, Vec<(String, RustValue)>)> {
    rows.into_iter()
        .map(|row| {
            let mut row_pk_val = None;
            let mut fields = Vec::with_capacity(row.values.len());

            for (col_name, value) in row.values {
                if pk_col == Some(col_name.as_str()) {
                    row_pk_val = match &value {
                        EngineValue::I64(v) => Some(v.to_string()),
                        EngineValue::String(v) => Some(v.clone()),
                        _ => None,
                    };
                }
                let value = engine_value_to_rust_value(value, schema, &col_name);
                fields.push((col_name, value));
            }

            (row_pk_val, fields)
        })
        .collect()
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

/// On Postgres, cast text-like special columns in SELECT output so Python hydration
/// sees the same string representation as SQLite.
fn property_json_type(col_info: &serde_json::Value) -> Option<&str> {
    col_info.get("type").and_then(|t| t.as_str()).or_else(|| {
        col_info
            .get("anyOf")
            .and_then(|a| a.as_array())
            .and_then(|types| {
                types.iter().find_map(|t| {
                    let s = t.get("type")?.as_str()?;
                    if s != "null" { None.or(Some(s)) } else { None }
                })
            })
    })
}

fn property_format(col_info: &serde_json::Value) -> Option<&str> {
    col_info.get("format").and_then(|f| f.as_str()).or_else(|| {
        col_info
            .get("anyOf")
            .and_then(|a| a.as_array())
            .and_then(|types| {
                types.iter().find_map(|t| {
                    let ty = t.get("type")?.as_str()?;
                    if ty == "null" {
                        None
                    } else {
                        t.get("format").and_then(|f| f.as_str())
                    }
                })
            })
    })
}

fn property_is_enum(col_info: &serde_json::Value) -> bool {
    col_info.get("enum").and_then(|e| e.as_array()).is_some()
}

fn apply_postgres_text_select_columns(
    select: &mut sea_query::SelectStatement,
    table_name: &str,
    schema: &serde_json::Value,
    pg_native_enum_columns: &HashSet<String>,
    backend: SqlDialect,
) {
    use sea_query::{Alias, Expr};

    let tbl = Alias::new(table_name);
    if backend != SqlDialect::Postgres {
        select.column((tbl.clone(), sea_query::Asterisk));
        return;
    }
    let Some(properties) = schema.get("properties").and_then(|p| p.as_object()) else {
        select.column((tbl.clone(), sea_query::Asterisk));
        return;
    };
    let need_text_from_schema = properties.values().any(|col_info| {
        let resolved = resolve_ref(schema, col_info);
        matches!(
            property_format(resolved),
            Some("uuid" | "date-time" | "date" | "decimal")
        ) || matches!(property_json_type(resolved), Some("object" | "array"))
            || property_is_enum(resolved)
    });
    let need_text_from_native_enum = properties
        .keys()
        .any(|k| pg_native_enum_columns.contains(k.as_str()));
    if !need_text_from_schema && !need_text_from_native_enum {
        select.column((tbl.clone(), sea_query::Asterisk));
        return;
    }
    for (col_name, col_info) in properties {
        let col_iden = Alias::new(col_name.as_str());
        let col_info = resolve_ref(schema, col_info);
        if matches!(
            property_format(col_info),
            Some("uuid" | "date-time" | "date" | "decimal")
        ) || matches!(property_json_type(col_info), Some("object" | "array"))
            || property_is_enum(col_info)
            || pg_native_enum_columns.contains(col_name.as_str())
        {
            let expr = Expr::cast_as(
                Expr::col((tbl.clone(), col_iden.clone())),
                Alias::new("text"),
            );
            select.expr_as(expr, col_iden);
        } else {
            select.column((tbl.clone(), col_iden));
        }
    }
}

fn resolve_ref<'a>(
    schema: &'a serde_json::Value,
    col_info: &'a serde_json::Value,
) -> &'a serde_json::Value {
    if let Some(ref_path) = col_info.get("$ref").and_then(|r| r.as_str())
        && let Some(def_name) = ref_path.strip_prefix("#/$defs/")
        && let Some(def) = schema.get("$defs").and_then(|defs| defs.get(def_name))
    {
        return def;
    }
    col_info
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

fn postgres_enum_type_name_for_column(
    col_name: &str,
    enum_udt: &HashMap<String, String>,
    col_info: Option<&serde_json::Value>,
) -> Option<String> {
    enum_udt.get(col_name).cloned().or_else(|| {
        col_info?
            .get("enum_type_name")?
            .as_str()
            .map(std::string::ToString::to_string)
    })
}

fn schema_property<'a>(
    schema: &'a serde_json::Value,
    col_name: &str,
) -> Option<&'a serde_json::Value> {
    schema
        .get("properties")
        .and_then(|p| p.get(col_name))
        .map(|prop| resolve_ref(schema, prop))
}

fn schema_value_expr(
    schema: &serde_json::Value,
    col_name: &str,
    value: &serde_json::Value,
    enum_udt: &HashMap<String, String>,
    uuid_columns: &HashSet<String>,
    ts_cast: &HashMap<String, String>,
    backend: SqlDialect,
) -> SimpleExpr {
    let col_info = schema_property(schema, col_name);
    if let serde_json::Value::String(s) = value
        && backend == SqlDialect::Postgres
        && let Some(tn) = postgres_enum_type_name_for_column(col_name, enum_udt, col_info)
    {
        return Expr::value(sea_query::Value::String(Some(Box::new(s.clone()))))
            .cast_as(Alias::new(tn.as_str()));
    }
    if value.is_null() && backend == SqlDialect::Postgres && uuid_columns.contains(col_name) {
        return Expr::value(sea_query::Value::String(None)).cast_as("uuid");
    }
    if let serde_json::Value::String(s) = value
        && backend == SqlDialect::Postgres
        && uuid_columns.contains(col_name)
        && uuid::Uuid::parse_str(s).is_ok()
    {
        return Expr::value(sea_query::Value::String(Some(Box::new(s.clone())))).cast_as("uuid");
    }
    if value.is_null()
        && backend == SqlDialect::Postgres
        && let Some(cast) = ts_cast.get(col_name)
    {
        return Expr::value(sea_query::Value::String(None)).cast_as(Alias::new(cast.as_str()));
    }
    if let serde_json::Value::String(s) = value
        && backend == SqlDialect::Postgres
        && let Some(cast) = ts_cast.get(col_name)
    {
        return Expr::value(sea_query::Value::String(Some(Box::new(s.clone()))))
            .cast_as(Alias::new(cast.as_str()));
    }
    let format = col_info.and_then(property_format);
    let json_type = col_info.and_then(property_json_type);
    let is_decimal = col_info
        .and_then(|prop| prop.get("anyOf"))
        .and_then(|a| a.as_array())
        .map(|items| items.iter().any(|item| item.get("pattern").is_some()))
        .unwrap_or(false);

    match value {
        value
            if backend == SqlDialect::Postgres && matches!(json_type, Some("object" | "array")) =>
        {
            if value.is_null() {
                Expr::value(sea_query::Value::String(None)).cast_as("json")
            } else {
                Expr::value(sea_query::Value::String(Some(Box::new(value.to_string()))))
                    .cast_as("json")
            }
        }
        serde_json::Value::String(s)
            if backend == SqlDialect::Postgres && format == Some("uuid") =>
        {
            Expr::value(sea_query::Value::String(Some(Box::new(s.clone())))).cast_as("uuid")
        }
        serde_json::Value::String(s)
            if backend == SqlDialect::Postgres && format == Some("date-time") =>
        {
            Expr::value(sea_query::Value::String(Some(Box::new(s.clone())))).cast_as("timestamptz")
        }
        serde_json::Value::String(s)
            if backend == SqlDialect::Postgres && format == Some("date") =>
        {
            Expr::value(sea_query::Value::String(Some(Box::new(s.clone())))).cast_as("date")
        }
        serde_json::Value::String(s) if json_type == Some("integer") => {
            if let Ok(parsed) = s.parse::<i64>() {
                Expr::value(sea_query::Value::BigInt(Some(parsed)))
            } else {
                Expr::value(sea_query::Value::String(Some(Box::new(s.clone()))))
            }
        }
        serde_json::Value::String(s) if json_type == Some("number") => {
            if let Ok(parsed) = s.parse::<f64>() {
                Expr::value(sea_query::Value::Double(Some(parsed)))
            } else {
                Expr::value(sea_query::Value::String(Some(Box::new(s.clone()))))
            }
        }
        serde_json::Value::String(s) if format == Some("binary") => Expr::value(
            sea_query::Value::Bytes(Some(Box::new(s.as_bytes().to_vec()))),
        ),
        serde_json::Value::String(s) if is_decimal => {
            if let Ok(parsed) = s.parse::<f64>() {
                Expr::value(sea_query::Value::Double(Some(parsed)))
            } else {
                Expr::value(sea_query::Value::String(Some(Box::new(s.clone()))))
            }
        }
        serde_json::Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                Expr::value(sea_query::Value::BigInt(Some(i)))
            } else if let Some(f) = n.as_f64() {
                Expr::value(sea_query::Value::Double(Some(f)))
            } else {
                Expr::value(sea_query::Value::String(None))
            }
        }
        serde_json::Value::String(s) => {
            Expr::value(sea_query::Value::String(Some(Box::new(s.clone()))))
        }
        serde_json::Value::Bool(b)
            if json_type == Some("boolean") && backend == SqlDialect::Sqlite =>
        {
            Expr::value(sea_query::Value::BigInt(Some(if *b { 1 } else { 0 })))
        }
        serde_json::Value::Bool(b) => Expr::value(sea_query::Value::Bool(Some(*b))),
        serde_json::Value::Null => Expr::value(sea_query::Value::String(None)),
        _ => Expr::value(sea_query::Value::String(Some(Box::new(value.to_string())))),
    }
}

fn backend_column_value_expr(
    col_name: &str,
    value: sea_query::Value,
    uuid_columns: &HashSet<String>,
    backend: SqlDialect,
) -> SimpleExpr {
    let expr = Expr::value(value);
    if backend == SqlDialect::Postgres && uuid_columns.contains(col_name) {
        expr.cast_as("uuid")
    } else {
        expr
    }
}

#[pyfunction]
#[pyo3(signature = (parent_tx_id=None))]
pub fn begin_transaction(
    py: Python<'_>,
    parent_tx_id: Option<String>,
) -> PyResult<Bound<'_, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let tx_id = uuid::Uuid::new_v4().to_string();
        if let Some(parent_tx_id) = parent_tx_id {
            let parent = TRANSACTION_REGISTRY.get(&parent_tx_id).ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err("Parent transaction not found")
            })?;
            let conn = parent.conn.clone();
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
                TransactionHandle::nested(conn, savepoint_name),
            );
        } else {
            let engine = active_engine()?;
            let conn = engine.begin_transaction_connection().await.map_err(|e| {
                pyo3::exceptions::PyRuntimeError::new_err(format!("Failed to BEGIN: {}", e))
            })?;

            TRANSACTION_REGISTRY.insert(tx_id.clone(), TransactionHandle::root(conn));
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
#[pyo3(signature = (cls, tx_id=None))]
pub fn fetch_all<'py>(
    py: Python<'py>,
    cls: Bound<'py, PyAny>,
    tx_id: Option<String>,
) -> PyResult<Bound<'py, PyAny>> {
    let name = cls.getattr("__name__")?.extract::<String>()?;
    let cls_py = cls.unbind();

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let (engine, tx_conn, backend) = {
            let engine = active_engine()?;
            let backend = engine.backend();
            let tx_conn = get_transaction_connection(tx_id);
            (engine, tx_conn, backend)
        };

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

            let dict_str = pyo3::intern!(py, "__dict__");
            let pydantic_fields_set_str = pyo3::intern!(py, "__pydantic_fields_set__");
            let new_str = pyo3::intern!(py, "__new__");

            for (row_pk_val, fields) in parsed_data {
                if let Some(ref pk_val) = row_pk_val
                    && let Some(existing_obj) = IDENTITY_MAP.get(&(name.clone(), pk_val.clone()))
                {
                    results.append(existing_obj.value().clone_ref(py))?;
                    continue;
                }

                let instance = cls.call_method1(new_str, (cls,))?;
                let dict_attr = instance.getattr(dict_str)?;
                let dict = dict_attr.cast::<pyo3::types::PyDict>()?;
                let fields_set = pyo3::types::PySet::empty(py)?;

                for (col_name, val) in fields {
                    let py_val = val.into_py_any(py)?;
                    let py_name = py_col_names.get(&col_name).unwrap().bind(py);
                    dict.set_item(py_name, py_val)?;
                    fields_set.add(py_name)?;
                }

                let _ = instance.setattr(pydantic_fields_set_str, fields_set);

                if let Some(pk_val) = row_pk_val {
                    IDENTITY_MAP.insert((name.clone(), pk_val), instance.clone().unbind());
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
#[pyo3(signature = (cls, pk_val, tx_id=None))]
pub fn fetch_one<'py>(
    py: Python<'py>,
    cls: Bound<'py, PyAny>,
    pk_val: String,
    tx_id: Option<String>,
) -> PyResult<Bound<'py, PyAny>> {
    let name = cls.getattr("__name__")?.extract::<String>()?;
    let cls_py = cls.unbind();

    // Check Identity Map first (if no transaction, or even with transaction, IM is usually safe)
    if let Some(existing_obj) = IDENTITY_MAP.get(&(name.clone(), pk_val.clone())) {
        let obj = existing_obj.value().clone_ref(py);
        return pyo3_async_runtimes::tokio::future_into_py(py, async move { Ok(obj) });
    }

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let (engine, tx_conn, backend) = {
            let engine = active_engine()?;
            let backend = engine.backend();
            let tx_conn = get_transaction_connection(tx_id);
            (engine, tx_conn, backend)
        };

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
                &pk_name,
                &serde_json::Value::String(pk_val.clone()),
                &no_enum_udt,
                &no_uuid,
                &no_ts,
                backend,
            );
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
                let instance = cls.call_method1("__new__", (cls,))?;
                let dict_attr = instance.getattr(pyo3::intern!(py, "__dict__"))?;
                let dict = dict_attr.cast::<pyo3::types::PyDict>()?;
                let fields_set = pyo3::types::PySet::empty(py)?;

                for (col_name, val) in fields {
                    let py_val = val.into_py_any(py)?;
                    let py_name = pyo3::types::PyString::new(py, &col_name);
                    dict.set_item(&py_name, py_val)?;
                    fields_set.add(&py_name)?;
                }

                let _ = instance.setattr(pyo3::intern!(py, "__pydantic_fields_set__"), fields_set);
                IDENTITY_MAP.insert((name.clone(), pk_val), instance.clone().unbind());
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
#[pyo3(signature = (name, data, tx_id=None))]
pub fn save_record(
    py: Python<'_>,
    name: String,
    data: String,
    tx_id: Option<String>,
) -> PyResult<Bound<'_, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let (engine, tx_conn, backend) = {
            let engine = active_engine()?;
            let backend = engine.backend();
            let tx_conn = get_transaction_connection(tx_id);
            (engine, tx_conn, backend)
        };

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
                    key,
                    value,
                    &enum_udt,
                    &uuid_columns,
                    &ts_cast,
                    backend,
                ));
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
pub fn save_bulk_records(
    py: Python<'_>,
    name: String,
    data_list_json: String,
    tx_id: Option<String>,
) -> PyResult<Bound<'_, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let (engine, tx_conn, backend) = {
            let engine = active_engine()?;
            let backend = engine.backend();
            let tx_conn = get_transaction_connection(tx_id);
            (engine, tx_conn, backend)
        };

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
                        key.to_string().as_str(),
                        value,
                        &enum_udt,
                        &uuid_columns,
                        &ts_cast,
                        backend,
                    ));
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

/// Fetches records for a given model class based on a JSON-defined query.
///
/// Args:
///     cls (PyAny): The Python model class.
///     query_json (str): The serialized QueryDef JSON.
///
/// Returns:
///     list[PyAny]: A list of hydrated model instances.
#[pyfunction]
#[pyo3(signature = (cls, query_json, tx_id=None))]
pub fn fetch_filtered<'py>(
    py: Python<'py>,
    cls: Bound<'py, PyAny>,
    query_json: String,
    tx_id: Option<String>,
) -> PyResult<Bound<'py, PyAny>> {
    let name = cls.getattr("__name__")?.extract::<String>()?;
    let cls_py = cls.unbind();

    let query_def: QueryDef = serde_json::from_str(&query_json).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("Invalid query JSON: {}", e))
    })?;

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let (engine, tx_conn, backend) = {
            let engine = active_engine()?;
            let backend = engine.backend();
            let tx_conn = get_transaction_connection(tx_id);
            (engine, tx_conn, backend)
        };

        let table_name = name.to_lowercase();
        let pg_native_enum_cols: HashSet<String> = {
            let m = postgres_enum_udt_by_column(&table_name, &engine, &tx_conn, backend).await?;
            m.keys().cloned().collect()
        };
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

            let dict_str = pyo3::intern!(py, "__dict__");
            let pydantic_fields_set_str = pyo3::intern!(py, "__pydantic_fields_set__");
            let new_str = pyo3::intern!(py, "__new__");

            for (row_pk_val, fields) in parsed_data {
                if let Some(ref pk_val) = row_pk_val
                    && let Some(existing_obj) = IDENTITY_MAP.get(&(name.clone(), pk_val.clone()))
                {
                    results.append(existing_obj.value().clone_ref(py))?;
                    continue;
                }

                let instance = cls.call_method1(new_str, (cls,))?;
                let dict_attr = instance.getattr(dict_str)?;
                let dict = dict_attr.cast::<pyo3::types::PyDict>()?;
                let fields_set = pyo3::types::PySet::empty(py)?;

                for (col_name, val) in fields {
                    let py_val = val.into_py_any(py)?;
                    let py_name = py_col_names.get(&col_name).unwrap().bind(py);
                    dict.set_item(py_name, py_val)?;
                    fields_set.add(py_name)?;
                }

                let _ = instance.setattr(pydantic_fields_set_str, fields_set);

                if let Some(pk_val) = row_pk_val {
                    IDENTITY_MAP.insert((name.clone(), pk_val), instance.clone().unbind());
                }

                results.append(instance)?;
            }
            Ok(results.into_any().unbind())
        })
    })
}

/// Returns the number of records matching a filtered query.
#[pyfunction]
#[pyo3(signature = (name, query_json, tx_id=None))]
pub fn count_filtered(
    py: Python<'_>,
    name: String,
    query_json: String,
    tx_id: Option<String>,
) -> PyResult<Bound<'_, PyAny>> {
    let query_def: QueryDef = serde_json::from_str(&query_json).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("Invalid query JSON: {}", e))
    })?;

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let (engine, tx_conn, backend) = {
            let engine = active_engine()?;
            let backend = engine.backend();
            let tx_conn = tx_id.and_then(|id| {
                TRANSACTION_REGISTRY
                    .get(&id)
                    .map(|tx| tx.value().conn.clone())
            });
            (engine, tx_conn, backend)
        };

        let table_name = name.to_lowercase();
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
pub fn register_instance(name: String, pk: String, obj: Py<PyAny>) -> PyResult<()> {
    IDENTITY_MAP.insert((name, pk), obj);
    Ok(())
}

/// Evicts a specific model instance from the global Identity Map.
#[pyfunction]
pub fn evict_instance(name: String, pk: String) -> PyResult<()> {
    IDENTITY_MAP.remove(&(name, pk));
    Ok(())
}

/// Deletes a record by its primary key.
#[pyfunction]
#[pyo3(signature = (name, pk_val, tx_id=None))]
pub fn delete_record(
    py: Python<'_>,
    name: String,
    pk_val: String,
    tx_id: Option<String>,
) -> PyResult<Bound<'_, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let (engine, tx_conn, backend) = {
            let engine = active_engine()?;
            let backend = engine.backend();
            let tx_conn = tx_id.and_then(|id| {
                TRANSACTION_REGISTRY
                    .get(&id)
                    .map(|tx| tx.value().conn.clone())
            });
            (engine, tx_conn, backend)
        };

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
                &schema,
                &pk_name,
                &serde_json::Value::String(pk_val),
                &no_enum_udt,
                &no_uuid,
                &no_ts,
                backend,
            );
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
#[pyo3(signature = (name, query_json, tx_id=None))]
pub fn delete_filtered(
    py: Python<'_>,
    name: String,
    query_json: String,
    tx_id: Option<String>,
) -> PyResult<Bound<'_, PyAny>> {
    let query_def: QueryDef = serde_json::from_str(&query_json).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("Invalid query JSON: {}", e))
    })?;

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let (engine, tx_conn, backend) = {
            let engine = active_engine()?;
            let backend = engine.backend();
            let tx_conn = tx_id.and_then(|id| {
                TRANSACTION_REGISTRY
                    .get(&id)
                    .map(|tx| tx.value().conn.clone())
            });
            (engine, tx_conn, backend)
        };

        let table_name = name.to_lowercase();
        // ... sql ...
        let (sql, bind_values) = {
            let mut delete = Query::delete();
            delete
                .from_table(Alias::new(&table_name))
                .cond_where(query_def.to_condition_for_backend(backend));
            sea_query_build_for_backend!(delete, backend)
        };

        let rows_affected =
            execute_statement_with_optional_tx(&engine, tx_conn, &sql, &bind_values.0)
                .await
                .map_err(|e| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!("Delete failed: {}", e))
                })?;

        // After bulk delete, we MUST clear the Identity Map for this model to avoid stale objects
        IDENTITY_MAP.retain(|(m_name, _), _| m_name != &name);

        Ok(rows_affected)
    })
}

/// Updates records matching a filtered query with provided values.
#[pyfunction]
#[pyo3(signature = (name, query_json, update_json, tx_id=None))]
pub fn update_filtered(
    py: Python<'_>,
    name: String,
    query_json: String,
    update_json: String,
    tx_id: Option<String>,
) -> PyResult<Bound<'_, PyAny>> {
    let query_def: QueryDef = serde_json::from_str(&query_json).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("Invalid query JSON: {}", e))
    })?;

    let update_values: serde_json::Value = serde_json::from_str(&update_json).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("Invalid update JSON: {}", e))
    })?;

    let update_map = update_values.as_object().cloned().ok_or_else(|| {
        pyo3::exceptions::PyValueError::new_err("Update values must be a JSON object")
    })?;

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let (engine, tx_conn, backend) = {
            let engine = active_engine()?;
            let backend = engine.backend();
            let tx_conn = get_transaction_connection(tx_id);
            (engine, tx_conn, backend)
        };

        let table_name = name.to_lowercase();
        let enum_udt = postgres_enum_udt_by_column(&table_name, &engine, &tx_conn, backend).await?;
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
                        &schema,
                        &key,
                        &value,
                        &enum_udt,
                        &uuid_columns,
                        &ts_cast,
                        backend,
                    ),
                );
            }
            sea_query_build_for_backend!(update, backend)
        };

        let rows_affected =
            execute_statement_with_optional_tx(&engine, tx_conn, &sql, &bind_values.0)
                .await
                .map_err(|e| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!("Update failed: {}", e))
                })?;

        // After bulk update, we MUST clear the Identity Map for this model to avoid stale objects
        IDENTITY_MAP.retain(|(m_name, _), _| m_name != &name);

        Ok(rows_affected)
    })
}

#[pyfunction]
#[pyo3(signature = (join_table, source_col, target_col, source_id, target_ids, tx_id=None))]
pub fn add_m2m_links<'py>(
    py: Python<'py>,
    join_table: String,
    source_col: String,
    target_col: String,
    source_id: Bound<'py, PyAny>,
    target_ids: Vec<Bound<'py, PyAny>>,
    tx_id: Option<String>,
) -> PyResult<Bound<'py, PyAny>> {
    let s_id = python_to_sea_value(source_id)?;
    let t_ids: Vec<sea_query::Value> = target_ids
        .into_iter()
        .map(|id| python_to_sea_value(id))
        .collect::<PyResult<Vec<_>>>()?;

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let (engine, tx_conn, backend) = {
            let engine = active_engine()?;
            let backend = engine.backend();
            let tx_conn = tx_id.and_then(|id| {
                TRANSACTION_REGISTRY
                    .get(&id)
                    .map(|tx| tx.value().conn.clone())
            });
            (engine, tx_conn, backend)
        };
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
#[pyo3(signature = (join_table, source_col, target_col, source_id, target_ids, tx_id=None))]
pub fn remove_m2m_links<'py>(
    py: Python<'py>,
    join_table: String,
    source_col: String,
    target_col: String,
    source_id: Bound<'py, PyAny>,
    target_ids: Vec<Bound<'py, PyAny>>,
    tx_id: Option<String>,
) -> PyResult<Bound<'py, PyAny>> {
    let s_id = python_to_sea_value(source_id)?;
    let t_ids: Vec<sea_query::Value> = target_ids
        .into_iter()
        .map(|id| python_to_sea_value(id))
        .collect::<PyResult<Vec<_>>>()?;

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let (engine, tx_conn, backend) = {
            let engine = active_engine()?;
            let backend = engine.backend();
            let tx_conn = tx_id.and_then(|id| {
                TRANSACTION_REGISTRY
                    .get(&id)
                    .map(|tx| tx.value().conn.clone())
            });
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
#[pyo3(signature = (join_table, source_col, source_id, tx_id=None))]
pub fn clear_m2m_links<'py>(
    py: Python<'py>,
    join_table: String,
    source_col: String,
    source_id: Bound<'py, PyAny>,
    tx_id: Option<String>,
) -> PyResult<Bound<'py, PyAny>> {
    let s_id = python_to_sea_value(source_id)?;

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let (engine, tx_conn, backend) = {
            let engine = active_engine()?;
            let backend = engine.backend();
            let tx_conn = tx_id.and_then(|id| {
                TRANSACTION_REGISTRY
                    .get(&id)
                    .map(|tx| tx.value().conn.clone())
            });
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

fn python_to_sea_value(val: Bound<'_, PyAny>) -> PyResult<sea_query::Value> {
    if val.is_none() {
        Ok(sea_query::Value::String(None))
    } else if let Ok(i) = val.extract::<i64>() {
        Ok(sea_query::Value::BigInt(Some(i)))
    } else if let Ok(f) = val.extract::<f64>() {
        Ok(sea_query::Value::Double(Some(f)))
    } else if let Ok(b) = val.extract::<bool>() {
        Ok(sea_query::Value::Bool(Some(b)))
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
fn python_to_engine_bind_value(val: &Bound<'_, PyAny>) -> PyResult<crate::backend::EngineBindValue> {
    use crate::backend::EngineBindValue;

    if val.is_none() {
        return Ok(EngineBindValue::Null);
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
        val.repr()?.to_string()
    )))
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
                EngineBindValue::Null
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
