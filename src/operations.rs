//! Core database operations for Ferro models.
//!
//! This module implements high-performance CRUD operations, leveraging
//! GIL-free parsing and zero-copy Direct Injection into Python objects.

use crate::state::{ENGINE, IDENTITY_MAP, MODEL_REGISTRY, RustValue};
use pyo3::prelude::*;
use sea_query::{Alias, Expr, Iden, InsertStatement, OnConflict, Query, SqliteQueryBuilder};
use sqlx::{Column, Row};
use std::collections::HashMap;

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
pub fn fetch_all<'py>(py: Python<'py>, cls: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let name = cls.getattr("__name__")?.extract::<String>()?;
    let cls_py = cls.unbind();

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let pool = {
            let engine_lock = ENGINE
                .read()
                .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Engine"))?;
            engine_lock.as_ref().cloned().ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err(
                    "Engine not initialized. Call connect() first.",
                )
            })?
        };

        let table_name = name.to_lowercase();
        let (sql, pk_col) = {
            let registry = MODEL_REGISTRY.read().map_err(|_| {
                pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Model Registry")
            })?;
            let schema = registry.get(&name).ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err(format!("Model '{}' not found", name))
            })?;

            let mut pk = None;
            if let Some(properties) = schema.get("properties").and_then(|p| p.as_object()) {
                for (col_name, col_info) in properties {
                    let is_pk = col_info
                        .get("primary_key")
                        .and_then(|pk| pk.as_bool())
                        .or_else(|| {
                            col_info
                                .get("json_schema_extra")
                                .and_then(|extra| extra.get("primary_key"))
                                .and_then(|pk| pk.as_bool())
                        })
                        .unwrap_or(false);

                    if is_pk {
                        pk = Some(col_name.clone());
                        break;
                    }
                }
            }

            let s = Query::select()
                .column(sea_query::Asterisk)
                .from(Alias::new(&table_name))
                .to_string(SqliteQueryBuilder);
            (s, pk)
        };

        let rows = sqlx::query(&sql)
            .fetch_all(pool.as_ref())
            .await
            .map_err(|e| {
                pyo3::exceptions::PyRuntimeError::new_err(format!(
                    "Fetch all failed for '{}': {}",
                    name, e
                ))
            })?;

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
                let val = if let Ok(val) = row.try_get::<i64, _>(col_name) {
                    RustValue::BigInt(val)
                } else if let Ok(val) = row.try_get::<f64, _>(col_name) {
                    RustValue::Double(val)
                } else if let Ok(val) = row.try_get::<String, _>(col_name) {
                    RustValue::String(val)
                } else if let Ok(val) = row.try_get::<bool, _>(col_name) {
                    RustValue::Bool(val)
                } else if let Ok(val) = row.try_get::<i32, _>(col_name) {
                    RustValue::Bool(val != 0)
                } else {
                    RustValue::None
                };
                fields.push((col_name.to_string(), val));
            }
            parsed_data.push((row_pk_val, fields));
        }

        Python::with_gil(|py| {
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
                if let Some(ref pk_val) = row_pk_val {
                    if let Some(existing_obj) = IDENTITY_MAP.get(&(name.clone(), pk_val.clone())) {
                        results.append(existing_obj.value().clone_ref(py))?;
                        continue;
                    }
                }

                let instance = cls.call_method1(new_str, (cls,))?;
                let dict = instance
                    .getattr(dict_str)?
                    .downcast_into::<pyo3::types::PyDict>()?;
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
pub fn fetch_one<'py>(
    py: Python<'py>,
    cls: Bound<'py, PyAny>,
    pk_val: String,
) -> PyResult<Bound<'py, PyAny>> {
    let name = cls.getattr("__name__")?.extract::<String>()?;
    let cls_py = cls.unbind();

    if let Some(existing_obj) = IDENTITY_MAP.get(&(name.clone(), pk_val.clone())) {
        let obj = existing_obj.value().clone_ref(py);
        return pyo3_async_runtimes::tokio::future_into_py(py, async move { Ok(obj) });
    }

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let pool = {
            let engine_lock = ENGINE
                .read()
                .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Engine"))?;
            engine_lock.as_ref().cloned().ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err(
                    "Engine not initialized. Call connect() first.",
                )
            })?
        };

        let table_name = name.to_lowercase();
        let (sql, _pk_col_name) = {
            let registry = MODEL_REGISTRY.read().map_err(|_| {
                pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Model Registry")
            })?;
            let schema = registry.get(&name).ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err(format!("Model '{}' not found", name))
            })?;

            let mut pk = None;
            if let Some(properties) = schema.get("properties").and_then(|p| p.as_object()) {
                for (col_name, col_info) in properties {
                    let is_pk = col_info
                        .get("primary_key")
                        .and_then(|pk| pk.as_bool())
                        .or_else(|| {
                            col_info
                                .get("json_schema_extra")
                                .and_then(|extra| extra.get("primary_key"))
                                .and_then(|pk| pk.as_bool())
                        })
                        .unwrap_or(false);

                    if is_pk {
                        pk = Some(col_name.clone());
                        break;
                    }
                }
            }

            let pk_name = pk.ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err(format!(
                    "Model '{}' has no primary key",
                    name
                ))
            })?;

            let s = Query::select()
                .column(sea_query::Asterisk)
                .from(Alias::new(&table_name))
                .and_where(Expr::col(Alias::new(&pk_name)).eq(pk_val.clone()))
                .to_string(SqliteQueryBuilder);
            (s, pk_name)
        };

        let row = sqlx::query(&sql)
            .fetch_optional(pool.as_ref())
            .await
            .map_err(|e| {
                pyo3::exceptions::PyRuntimeError::new_err(format!(
                    "Fetch one failed for '{}': {}",
                    name, e
                ))
            })?;

        let parsed_row = match row {
            Some(row) => {
                let mut fields = Vec::with_capacity(row.columns().len());
                for col in row.columns() {
                    let col_name = col.name();
                    let val = if let Ok(val) = row.try_get::<i64, _>(col_name) {
                        RustValue::BigInt(val)
                    } else if let Ok(val) = row.try_get::<f64, _>(col_name) {
                        RustValue::Double(val)
                    } else if let Ok(val) = row.try_get::<String, _>(col_name) {
                        RustValue::String(val)
                    } else if let Ok(val) = row.try_get::<bool, _>(col_name) {
                        RustValue::Bool(val)
                    } else if let Ok(val) = row.try_get::<i32, _>(col_name) {
                        RustValue::Bool(val != 0)
                    } else {
                        RustValue::None
                    };
                    fields.push((col_name.to_string(), val));
                }
                Some(fields)
            }
            None => None,
        };

        match parsed_row {
            Some(fields) => Python::with_gil(|py| {
                let cls = cls_py.bind(py);
                let instance = cls.call_method1("__new__", (cls,))?;
                let dict = instance
                    .getattr(pyo3::intern!(py, "__dict__"))?
                    .downcast_into::<pyo3::types::PyDict>()?;
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
            None => Ok(Python::with_gil(|py| py.None())),
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
pub fn save_record(py: Python<'_>, name: String, data: String) -> PyResult<Bound<'_, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let pool = {
            let engine_lock = ENGINE
                .read()
                .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Engine"))?;
            engine_lock.as_ref().cloned().ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err(
                    "Engine not initialized. Call connect() first.",
                )
            })?
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

        let record: serde_json::Value = serde_json::from_str(&data).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("Invalid record JSON: {}", e))
        })?;

        let record_obj = record.as_object().ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err("Record data must be a JSON object")
        })?;

        let mut pk_col = None;
        if let Some(properties) = schema.get("properties").and_then(|p| p.as_object()) {
            for (col_name, col_info) in properties {
                let is_pk = col_info
                    .get("primary_key")
                    .and_then(|pk| pk.as_bool())
                    .or_else(|| {
                        col_info
                            .get("json_schema_extra")
                            .and_then(|extra| extra.get("primary_key"))
                            .and_then(|pk| pk.as_bool())
                    })
                    .unwrap_or(false);

                if is_pk {
                    pk_col = Some(col_name.clone());
                    break;
                }
            }
        }

        let table_name = name.to_lowercase();
        let (sql, bind_values) = {
            let mut columns = Vec::new();
            let mut values = Vec::new();

            for (key, value) in record_obj {
                columns.push(Alias::new(key));
                let val = match value {
                    serde_json::Value::Number(n) => {
                        if let Some(i) = n.as_i64() {
                            sea_query::Value::BigInt(Some(i))
                        } else if let Some(f) = n.as_f64() {
                            sea_query::Value::Double(Some(f))
                        } else {
                            sea_query::Value::String(None)
                        }
                    }
                    serde_json::Value::String(s) => {
                        sea_query::Value::String(Some(Box::new(s.clone())))
                    }
                    serde_json::Value::Bool(b) => sea_query::Value::Bool(Some(*b)),
                    serde_json::Value::Null => sea_query::Value::String(None),
                    _ => sea_query::Value::String(Some(Box::new(value.to_string()))),
                };
                values.push(Expr::value(val));
            }

            let mut insert_stmt = InsertStatement::new()
                .into_table(Alias::new(&table_name))
                .columns(columns.clone())
                .values(values)
                .map_err(|e| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!(
                        "Statement build failed: {}",
                        e
                    ))
                })?
                .to_owned();

            if let Some(pk) = pk_col {
                let mut on_conflict = OnConflict::column(Alias::new(&pk));
                let mut update_cols = Vec::new();
                for col in &columns {
                    if col.to_string() != pk {
                        update_cols.push(col.clone());
                    }
                }
                on_conflict.update_columns(update_cols);
                insert_stmt.on_conflict(on_conflict);
            }

            insert_stmt.build(SqliteQueryBuilder)
        };

        let mut query = sqlx::query(&sql);
        for val in bind_values.iter() {
            query = match val {
                sea_query::Value::Bool(Some(b)) => query.bind(*b),
                sea_query::Value::BigInt(Some(i)) => query.bind(*i),
                sea_query::Value::Double(Some(f)) => query.bind(*f),
                sea_query::Value::String(Some(s)) => query.bind(s.as_ref().clone()),
                _ => query.bind(Option::<String>::None),
            };
        }

        query.execute(pool.as_ref()).await.map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("Save failed for '{}': {}", name, e))
        })?;

        Ok(())
    })
}

/// Registers a live Python object in the global Identity Map.
#[pyfunction]
pub fn register_instance(name: String, pk: String, obj: PyObject) -> PyResult<()> {
    IDENTITY_MAP.insert((name, pk), obj);
    Ok(())
}
