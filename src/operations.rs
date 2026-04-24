//! Core database operations for Ferro models.
//!
//! This module implements high-performance CRUD operations, leveraging
//! GIL-free parsing and zero-copy Direct Injection into Python objects.

use crate::query::QueryDef;
use crate::state::{
    engine_pool, IDENTITY_MAP, MODEL_REGISTRY, RustValue, TRANSACTION_REGISTRY, TransactionHandle,
};
use pyo3::prelude::*;
use sea_query::{
    Alias, Expr, Iden, InsertStatement, OnConflict, Order, PostgresQueryBuilder, Query,
    SimpleExpr, SqliteQueryBuilder, UpdateStatement, Value as SeaValue,
};
use sqlx::{Column, Row};
use std::collections::HashMap;

macro_rules! get_conn {
    ($pool:expr, $tx_id:expr) => {{
        if let Some(tx_id) = $tx_id {
            if let Some(tx_handle) = TRANSACTION_REGISTRY.get(&tx_id) {
                let conn = tx_handle.value().conn.clone();
                Some(conn)
            } else {
                None
            }
        } else {
            None
        }
    }};
}

/// Build SQL with the dialect set at `connect()` time (`?` for SQLite, `$n` for Postgres).
macro_rules! sea_query_build {
    ($stmt:expr) => {{
        match crate::state::sql_dialect() {
            crate::state::SqlDialect::Sqlite => $stmt.build(SqliteQueryBuilder),
            crate::state::SqlDialect::Postgres => $stmt.build(PostgresQueryBuilder),
        }
    }};
}

macro_rules! sea_query_to_string {
    ($stmt:expr) => {{
        match crate::state::sql_dialect() {
            crate::state::SqlDialect::Sqlite => $stmt.to_string(SqliteQueryBuilder),
            crate::state::SqlDialect::Postgres => $stmt.to_string(PostgresQueryBuilder),
        }
    }};
}

macro_rules! decode_column {
    ($row:expr, $name:expr, $col_name:expr) => {{
        let registry = MODEL_REGISTRY.read().unwrap();
        let prop = registry
            .get($name)
            .and_then(|s| s.get("properties"))
            .and_then(|p| p.get($col_name));

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
            if let Ok(v) = $row.try_get::<f64, _>($col_name) {
                RustValue::Decimal(v.to_string())
            } else if let Ok(v) = $row.try_get::<String, _>($col_name) {
                RustValue::Decimal(v)
            } else {
                RustValue::None
            }
        } else if format == Some("binary") {
            if let Ok(v) = $row.try_get::<Vec<u8>, _>($col_name) {
                RustValue::Blob(v)
            } else if let Ok(v) = $row.try_get::<String, _>($col_name) {
                RustValue::Blob(v.into_bytes())
            } else {
                RustValue::None
            }
        } else if let Ok(val) = $row.try_get::<i64, _>($col_name) {
            let is_bool = json_type == Some("boolean");
            if is_bool {
                RustValue::Bool(val != 0)
            } else {
                RustValue::BigInt(val)
            }
        } else if let Ok(val) = $row.try_get::<f64, _>($col_name) {
            RustValue::Double(val)
        } else if let Ok(val) = $row.try_get::<Vec<u8>, _>($col_name) {
            RustValue::Blob(val)
        } else if let Ok(val) = $row.try_get::<String, _>($col_name) {
            match (json_type, format) {
                (_, Some("date-time")) => RustValue::DateTime(val),
                (_, Some("date")) => RustValue::Date(val),
                (_, Some("uuid")) => RustValue::Uuid(val),
                (Some("object"), _) | (Some("array"), _) => {
                    if let Ok(json_val) = serde_json::from_str(&val) {
                        RustValue::Json(json_val)
                    } else {
                        RustValue::String(val)
                    }
                }
                _ => RustValue::String(val),
            }
        } else if let Ok(val) = $row.try_get::<bool, _>($col_name) {
            RustValue::Bool(val)
        } else {
            RustValue::None
        }
    }};
}

/// On Postgres, `sqlx::Any` cannot decode native `uuid` columns. When the model schema marks
/// UUID fields, expand `SELECT *` into explicit columns with `::text` casts so decoding uses
/// text (same representation as SQLite).
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
) {
    use sea_query::{Alias, Expr};

    let tbl = Alias::new(table_name);
    if crate::state::sql_dialect() != crate::state::SqlDialect::Postgres {
        select.column((tbl.clone(), sea_query::Asterisk));
        return;
    }
    let Some(properties) = schema.get("properties").and_then(|p| p.as_object()) else {
        select.column((tbl.clone(), sea_query::Asterisk));
        return;
    };
    if !properties.values().any(|col_info| {
        let resolved = resolve_ref(schema, col_info);
        matches!(
            property_format(resolved),
            Some("uuid" | "date-time" | "date")
        ) || property_is_enum(resolved)
    })
    {
        select.column((tbl.clone(), sea_query::Asterisk));
        return;
    }
    for (col_name, col_info) in properties {
        let col_iden = Alias::new(col_name.as_str());
        let col_info = resolve_ref(schema, col_info);
        if matches!(property_format(col_info), Some("uuid" | "date-time" | "date"))
            || property_is_enum(col_info)
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

/// Helper to bind Sea-Query values to a SQLx Any query.
fn bind_query<'a>(
    mut query: sqlx::query::Query<'a, sqlx::Any, sqlx::any::AnyArguments<'a>>,
    values: &'a [SeaValue],
) -> sqlx::query::Query<'a, sqlx::Any, sqlx::any::AnyArguments<'a>> {
    for val in values {
        query = match val {
            SeaValue::Bool(Some(b)) => query.bind(*b),
            SeaValue::TinyInt(Some(i)) => query.bind(*i as i64),
            SeaValue::SmallInt(Some(i)) => query.bind(*i as i64),
            SeaValue::Int(Some(i)) => query.bind(*i as i64),
            SeaValue::BigInt(Some(i)) => query.bind(*i),
            SeaValue::BigUnsigned(Some(i)) => query.bind(*i as i64),
            SeaValue::Float(Some(f)) => query.bind(*f as f64),
            SeaValue::Double(Some(f)) => query.bind(*f),
            SeaValue::String(Some(s)) => query.bind(s.as_ref().clone()),
            SeaValue::Char(Some(c)) => query.bind(c.to_string()),
            SeaValue::Bytes(Some(b)) => query.bind(b.as_ref().clone()),
            _ => query.bind(Option::<String>::None),
        };
    }
    query
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
) -> SimpleExpr {
    let col_info = schema_property(schema, col_name);
    let format = col_info.and_then(property_format);
    let json_type = col_info.and_then(property_json_type);
    let is_decimal = col_info
        .and_then(|prop| prop.get("anyOf"))
        .and_then(|a| a.as_array())
        .map(|items| items.iter().any(|item| item.get("pattern").is_some()))
        .unwrap_or(false);

    match value {
        serde_json::Value::String(s)
            if crate::state::sql_dialect() == crate::state::SqlDialect::Postgres
                && format == Some("uuid") =>
        {
            Expr::value(sea_query::Value::String(Some(Box::new(s.clone())))).cast_as("uuid")
        }
        serde_json::Value::String(s)
            if crate::state::sql_dialect() == crate::state::SqlDialect::Postgres
                && format == Some("date-time") =>
        {
            Expr::value(sea_query::Value::String(Some(Box::new(s.clone()))))
                .cast_as("timestamptz")
        }
        serde_json::Value::String(s)
            if crate::state::sql_dialect() == crate::state::SqlDialect::Postgres
                && format == Some("date") =>
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
        serde_json::Value::String(s) if format == Some("binary") => {
            Expr::value(sea_query::Value::Bytes(Some(Box::new(
                s.as_bytes().to_vec(),
            ))))
        }
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
        serde_json::Value::Bool(b) if json_type == Some("boolean") => {
            Expr::value(sea_query::Value::BigInt(Some(if *b { 1 } else { 0 })))
        }
        serde_json::Value::Bool(b) => Expr::value(sea_query::Value::Bool(Some(*b))),
        serde_json::Value::Null => Expr::value(sea_query::Value::String(None)),
        _ => Expr::value(sea_query::Value::String(Some(Box::new(value.to_string())))),
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
            let mut locked_conn = conn.lock().await;
            sqlx::query(&format!("SAVEPOINT {savepoint_name}"))
                .execute(&mut *locked_conn)
                .await
                .map_err(|e| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!(
                        "Failed to create SAVEPOINT: {}",
                        e
                    ))
                })?;
            drop(locked_conn);

            TRANSACTION_REGISTRY.insert(
                tx_id.clone(),
                TransactionHandle::nested(conn, savepoint_name),
            );
        } else {
            let pool = engine_pool().ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err("Engine not initialized")
            })?;

            let mut conn = pool.acquire().await.map_err(|e| {
                pyo3::exceptions::PyRuntimeError::new_err(format!(
                    "Failed to acquire connection: {}",
                    e
                ))
            })?;

            sqlx::query("BEGIN")
                .execute(&mut *conn)
                .await
                .map_err(|e| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!("Failed to BEGIN: {}", e))
                })?;

            TRANSACTION_REGISTRY.insert(tx_id.clone(), TransactionHandle::root(conn.detach()));
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

        let mut conn = tx_handle.conn.lock().await;
        if let Some(savepoint_name) = tx_handle.savepoint_name {
            sqlx::query(&format!("RELEASE SAVEPOINT {savepoint_name}"))
                .execute(&mut *conn)
                .await
                .map_err(|e| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!(
                        "Failed to RELEASE SAVEPOINT: {}",
                        e
                    ))
                })?;
        } else {
            sqlx::query("COMMIT")
                .execute(&mut *conn)
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

        let mut conn = tx_handle.conn.lock().await;
        if let Some(savepoint_name) = tx_handle.savepoint_name {
            sqlx::query(&format!("ROLLBACK TO SAVEPOINT {savepoint_name}"))
                .execute(&mut *conn)
                .await
                .map_err(|e| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!(
                        "Failed to ROLLBACK TO SAVEPOINT: {}",
                        e
                    ))
                })?;
            sqlx::query(&format!("RELEASE SAVEPOINT {savepoint_name}"))
                .execute(&mut *conn)
                .await
                .map_err(|e| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!(
                        "Failed to RELEASE SAVEPOINT: {}",
                        e
                    ))
                })?;
        } else {
            sqlx::query("ROLLBACK")
                .execute(&mut *conn)
                .await
                .map_err(|e| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!(
                        "Failed to ROLLBACK: {}",
                        e
                    ))
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
        let (pool, tx_conn) = {
            let pool = engine_pool().ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err("Engine not initialized")
            })?;
            let tx_conn = get_conn!(pool, tx_id);
            (pool, tx_conn)
        };

        let table_name = name.to_lowercase();
        // ... same sql generation ...
        let (sql, pk_col) = {
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
            apply_postgres_text_select_columns(&mut stmt, &table_name, schema);
            let s = sea_query_to_string!(stmt.from(Alias::new(&table_name)));
            (s, pk)
        };

        let rows = if let Some(conn_arc) = tx_conn {
            let mut conn = conn_arc.lock().await;
            sqlx::query(&sql).fetch_all(&mut *conn).await
        } else {
            sqlx::query(&sql).fetch_all(pool.as_ref()).await
        }
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("Fetch failed: {}", e)))?;

        let mut parsed_data = Vec::with_capacity(rows.len());
        for row in rows {
            let mut row_pk_val = None;
            if let Some(ref pk_name) = pk_col {
                if let Ok(val) = row.try_get::<i64, _>(pk_name.as_str()) {
                    row_pk_val = Some(val.to_string());
                } else if let Ok(val) = row.try_get::<String, _>(pk_name.as_str()) {
                    row_pk_val = Some(val);
                }
            }

            let mut fields = Vec::with_capacity(row.columns().len());
            for col in row.columns() {
                let col_name = col.name();
                let val = decode_column!(row, &name, col_name);
                fields.push((col_name.to_string(), val));
            }
            parsed_data.push((row_pk_val, fields));
        }

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
        let (pool, tx_conn) = {
            let pool = engine_pool().ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err("Engine not initialized")
            })?;
            let tx_conn = get_conn!(pool, tx_id);
            (pool, tx_conn)
        };

        let table_name = name.to_lowercase();
        // ... sql logic ...
        let (sql, bind_values, _pk_col_name) = {
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
            apply_postgres_text_select_columns(&mut stmt, &table_name, schema);
            let pk_expr = schema_value_expr(schema, &pk_name, &serde_json::Value::String(pk_val.clone()));
            let (s, values) = sea_query_build!(stmt
                .from(Alias::new(&table_name))
                .and_where(Expr::col(Alias::new(&pk_name)).eq(pk_expr)));
            (s, values, pk_name)
        };

        let query = bind_query(sqlx::query(&sql), &bind_values.0);
        let row = if let Some(conn_arc) = tx_conn {
            let mut conn = conn_arc.lock().await;
            query.fetch_optional(&mut *conn).await
        } else {
            query.fetch_optional(pool.as_ref()).await
        }
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("Fetch failed: {}", e)))?;

        let parsed_row = match row {
            Some(row) => {
                let mut fields = Vec::with_capacity(row.columns().len());
                for col in row.columns() {
                    let col_name = col.name();
                    let val = decode_column!(row, &name, col_name);
                    fields.push((col_name.to_string(), val));
                }
                Some(fields)
            }
            None => None,
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
        let (pool, tx_conn) = {
            let pool = engine_pool().ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err("Engine not initialized")
            })?;
            let tx_conn = get_conn!(pool, tx_id);
            (pool, tx_conn)
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
                values.push(schema_value_expr(&schema, key, value));
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
            let needs_postgres_returning = crate::state::sql_dialect()
                == crate::state::SqlDialect::Postgres
                && pk_col.is_some()
                && pk_is_auto
                && !pk_provided;
            let (mut sql, values) = sea_query_build!(insert_stmt);
            if needs_postgres_returning
                && let Some(pk) = pk_col.as_ref()
            {
                sql.push_str(&format!(" RETURNING \"{}\"", pk));
            }
            (sql, values, needs_postgres_returning)
        };

        let query = bind_query(sqlx::query(&sql), &bind_values.0);
        if let Some(conn_arc) = tx_conn {
            let mut conn = conn_arc.lock().await;
            if needs_postgres_returning {
                let row = query.fetch_one(&mut *conn).await.map_err(|e| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!("Save failed: {}", e))
                })?;
                let id: i64 = row.try_get(0).unwrap_or(0);
                Ok((id > 0).then_some(id))
            } else {
                let res = query.execute(&mut *conn).await;
                if res.is_err() {
                    return Err(pyo3::exceptions::PyRuntimeError::new_err(format!(
                        "Save failed: {}",
                        res.err().unwrap()
                    )));
                }
                let exec_res = res.unwrap();
                let mut lid = exec_res.last_insert_id();
                if crate::state::sql_dialect() == crate::state::SqlDialect::Sqlite
                    && (lid.is_none() || lid == Some(0))
                    && let Ok(row) = sqlx::query("SELECT last_insert_rowid()")
                        .fetch_one(&mut *conn)
                        .await
                {
                    let id: i64 = row.try_get(0).unwrap_or(0);
                    if id > 0 {
                        lid = Some(id);
                    }
                }
                Ok(lid)
            }
        } else {
            let mut conn = pool.acquire().await.map_err(|e| {
                pyo3::exceptions::PyRuntimeError::new_err(format!("Pool acquire failed: {}", e))
            })?;
            if needs_postgres_returning {
                let row = query.fetch_one(&mut *conn).await.map_err(|e| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!("Save failed: {}", e))
                })?;
                let id: i64 = row.try_get(0).unwrap_or(0);
                Ok((id > 0).then_some(id))
            } else {
                let res = query.execute(&mut *conn).await;
                if res.is_err() {
                    return Err(pyo3::exceptions::PyRuntimeError::new_err(format!(
                        "Save failed: {}",
                        res.err().unwrap()
                    )));
                }
                let exec_res = res.unwrap();
                let mut lid = exec_res.last_insert_id();
                if crate::state::sql_dialect() == crate::state::SqlDialect::Sqlite
                    && (lid.is_none() || lid == Some(0))
                    && let Ok(row) = sqlx::query("SELECT last_insert_rowid()")
                        .fetch_one(&mut *conn)
                        .await
                {
                    let id: i64 = row.try_get(0).unwrap_or(0);
                    if id > 0 {
                        lid = Some(id);
                    }
                }
                Ok(lid)
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
        let (pool, tx_conn) = {
            let pool = engine_pool().ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err(
                    "Engine not initialized. Call connect() first.",
                )
            })?;
            let tx_conn = get_conn!(pool, tx_id);
            (pool, tx_conn)
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
                    row_values.push(schema_value_expr(&schema, key.to_string().as_str(), value));
                }
                insert_stmt.values(row_values).map_err(|e| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!(
                        "Statement build failed: {}",
                        e
                    ))
                })?;
            }

            let (s, values) = sea_query_build!(insert_stmt);
            (s, values)
        };

        let query = bind_query(sqlx::query(&sql), &bind_values.0);
        let result = if let Some(conn_arc) = tx_conn {
            let mut conn = conn_arc.lock().await;
            query.execute(&mut *conn).await
        } else {
            query.execute(pool.as_ref()).await
        }
        .map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!(
                "Bulk save failed for '{}': {}",
                name, e
            ))
        })?;

        Ok(result.rows_affected())
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
        let (pool, tx_conn) = {
            let pool = engine_pool().ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err("Engine not initialized")
            })?;
            let tx_conn = get_conn!(pool, tx_id);
            (pool, tx_conn)
        };

        let table_name = name.to_lowercase();
        // ...
        let (sql, bind_values, pk_col) = {
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
            apply_postgres_text_select_columns(&mut select, &table_name, schema);
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
                select.and_where(
                    Expr::col((join_table.clone(), source_col.clone()))
                        .eq(query_def.value_rhs_simple_expr(&m2m.source_col, &m2m.source_id, true)),
                );
            }

            select.cond_where(query_def.to_condition());
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
            let (s, values) = sea_query_build!(select);
            (s, values, pk)
        };

        let query = bind_query(sqlx::query(&sql), &bind_values.0);
        let rows = if let Some(conn_arc) = tx_conn {
            let mut conn = conn_arc.lock().await;
            query.fetch_all(&mut *conn).await
        } else {
            query.fetch_all(pool.as_ref()).await
        }
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("Fetch failed: {}", e)))?;

        let mut parsed_data = Vec::with_capacity(rows.len());
        for row in rows {
            let mut row_pk_val = None;
            if let Some(ref pk_name) = pk_col {
                if let Ok(val) = row.try_get::<i64, _>(pk_name.as_str()) {
                    row_pk_val = Some(val.to_string());
                } else if let Ok(val) = row.try_get::<String, _>(pk_name.as_str()) {
                    row_pk_val = Some(val);
                }
            }

            let mut fields = Vec::with_capacity(row.columns().len());
            for col in row.columns() {
                let col_name = col.name();
                let val = decode_column!(row, &name, col_name);
                fields.push((col_name.to_string(), val));
            }
            parsed_data.push((row_pk_val, fields));
        }

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
        let (pool, tx_conn) = {
            let pool = engine_pool().ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err("Engine not initialized")
            })?;
            let tx_conn = get_conn!(pool, tx_id);
            (pool, tx_conn)
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
                select.and_where(
                    Expr::col((join_table.clone(), source_col.clone()))
                        .eq(query_def.value_rhs_simple_expr(&m2m.source_col, &m2m.source_id, true)),
                );
            } else {
                select.from(Alias::new(&table_name));
            }

            select.cond_where(query_def.to_condition());
            sea_query_build!(select)
        };

        let query = bind_query(sqlx::query(&sql), &bind_values.0);
        let row = if let Some(conn_arc) = tx_conn {
            let mut conn = conn_arc.lock().await;
            query.fetch_one(&mut *conn).await
        } else {
            query.fetch_one(pool.as_ref()).await
        }
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("Count failed: {}", e)))?;

        let count: i64 = row.try_get(0).unwrap_or(0);
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
        let (pool, tx_conn) = {
            let pool = engine_pool().ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err("Engine not initialized")
            })?;
            let tx_conn = get_conn!(pool, tx_id);
            (pool, tx_conn)
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
            let pk_expr = schema_value_expr(&schema, &pk_name, &serde_json::Value::String(pk_val));
            let (s, values) = sea_query_build!(Query::delete()
                .from_table(Alias::new(&table_name))
                .and_where(Expr::col(Alias::new(&pk_name)).eq(pk_expr)));
            (s, values)
        };

        let query = bind_query(sqlx::query(&sql), &bind_values.0);
        if let Some(conn_arc) = tx_conn {
            let mut conn = conn_arc.lock().await;
            query.execute(&mut *conn).await
        } else {
            query.execute(pool.as_ref()).await
        }
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("Delete failed: {}", e)))?;

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
        let (pool, tx_conn) = {
            let pool = engine_pool().ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err("Engine not initialized")
            })?;
            let tx_conn = get_conn!(pool, tx_id);
            (pool, tx_conn)
        };

        let table_name = name.to_lowercase();
        // ... sql ...
        let (sql, bind_values) = {
            let mut delete = Query::delete();
            delete
                .from_table(Alias::new(&table_name))
                .cond_where(query_def.to_condition());
            sea_query_build!(delete)
        };

        let query = bind_query(sqlx::query(&sql), &bind_values.0);
        let result = if let Some(conn_arc) = tx_conn {
            let mut conn = conn_arc.lock().await;
            query.execute(&mut *conn).await
        } else {
            query.execute(pool.as_ref()).await
        }
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("Delete failed: {}", e)))?;

        // After bulk delete, we MUST clear the Identity Map for this model to avoid stale objects
        IDENTITY_MAP.retain(|(m_name, _), _| m_name != &name);

        Ok(result.rows_affected())
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
        let (pool, tx_conn) = {
            let pool = engine_pool().ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err("Engine not initialized")
            })?;
            let tx_conn = get_conn!(pool, tx_id);
            (pool, tx_conn)
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
            let mut update = UpdateStatement::new()
                .table(Alias::new(&table_name))
                .cond_where(query_def.to_condition())
                .to_owned();
            for (key, value) in update_map {
                update.value(Alias::new(&key), schema_value_expr(&schema, &key, &value));
            }
            sea_query_build!(update)
        };

        let query = bind_query(sqlx::query(&sql), &bind_values.0);
        let result = if let Some(conn_arc) = tx_conn {
            let mut conn = conn_arc.lock().await;
            query.execute(&mut *conn).await
        } else {
            query.execute(pool.as_ref()).await
        }
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("Update failed: {}", e)))?;

        // After bulk update, we MUST clear the Identity Map for this model to avoid stale objects
        IDENTITY_MAP.retain(|(m_name, _), _| m_name != &name);

        Ok(result.rows_affected())
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

    let (sql, bind_values) = {
        let mut insert = InsertStatement::new()
            .into_table(Alias::new(&join_table))
            .columns(vec![Alias::new(&source_col), Alias::new(&target_col)])
            .to_owned();

        for t_id in t_ids {
            insert
                .values(vec![Expr::value(s_id.clone()), Expr::value(t_id)])
                .unwrap();
        }
        sea_query_build!(insert)
    };

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let (pool, tx_conn) = {
            let pool = engine_pool().ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err("Engine not initialized")
            })?;
            let tx_conn = get_conn!(pool, tx_id);
            (pool, tx_conn)
        };

        let query = bind_query(sqlx::query(&sql), &bind_values.0);
        if let Some(conn_arc) = tx_conn {
            let mut conn = conn_arc.lock().await;
            query.execute(&mut *conn).await
        } else {
            query.execute(pool.as_ref()).await
        }
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

    let (sql, bind_values) = sea_query_build!(Query::delete()
        .from_table(Alias::new(&join_table))
        .and_where(Expr::col(Alias::new(&source_col)).eq(s_id))
        .and_where(Expr::col(Alias::new(&target_col)).is_in(t_ids)));

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let (pool, tx_conn) = {
            let pool = engine_pool().ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err("Engine not initialized")
            })?;
            let tx_conn = get_conn!(pool, tx_id);
            (pool, tx_conn)
        };

        let query = bind_query(sqlx::query(&sql), &bind_values.0);
        if let Some(conn_arc) = tx_conn {
            let mut conn = conn_arc.lock().await;
            query.execute(&mut *conn).await
        } else {
            query.execute(pool.as_ref()).await
        }
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

    let (sql, bind_values) = sea_query_build!(Query::delete()
        .from_table(Alias::new(&join_table))
        .and_where(Expr::col(Alias::new(&source_col)).eq(s_id)));

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let (pool, tx_conn) = {
            let pool = engine_pool().ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err("Engine not initialized")
            })?;
            let tx_conn = get_conn!(pool, tx_id);
            (pool, tx_conn)
        };

        let query = bind_query(sqlx::query(&sql), &bind_values.0);
        if let Some(conn_arc) = tx_conn {
            let mut conn = conn_arc.lock().await;
            query.execute(&mut *conn).await
        } else {
            query.execute(pool.as_ref()).await
        }
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
