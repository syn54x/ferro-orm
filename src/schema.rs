//! Schema translation and registration logic.
//!
//! This module handles converting Pydantic JSON schemas into Sea-Query
//! table definitions and managing the model registry.

use pyo3::prelude::*;
use sea_query::{ColumnDef, Alias, Table, SqliteQueryBuilder};
use sqlx::{Any, Pool};
use std::sync::Arc;
use crate::state::{MODEL_REGISTRY, ENGINE};

/// Maps a JSON schema type string to a Sea-Query `ColumnDef`.
pub fn json_type_to_sea_query(col_def: &mut ColumnDef, json_type: &str) {
    match json_type {
        "integer" => { col_def.big_integer(); },
        "string" => { col_def.string(); },
        "number" => { col_def.double(); },
        "boolean" => { col_def.boolean(); },
        _ => { col_def.string(); },
    }
}

/// Internal utility to create all registered tables in the database.
///
/// This is used by both the `connect(auto_migrate=True)` flow and the
/// manual `create_tables()` function.
///
/// # Errors
/// Returns a `PyErr` if the SQL execution fails.
pub async fn internal_create_tables(pool: Arc<Pool<Any>>) -> PyResult<()> {
    let schemas = {
        let registry = MODEL_REGISTRY.read().map_err(|_| {
            pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Model Registry")
        })?;
        registry.clone()
    };

    for (name, schema) in schemas {
        let sql = {
            let mut table_stmt = Table::create()
                .table(Alias::new(name.to_lowercase()))
                .if_not_exists()
                .to_owned();

            if let Some(properties) = schema.get("properties").and_then(|p| p.as_object()) {
                for (col_name, col_info) in properties {
                    let mut col_def = ColumnDef::new(Alias::new(col_name));

                    if let Some(json_type) = col_info.get("type").and_then(|t| t.as_str()) {
                        json_type_to_sea_query(&mut col_def, json_type);
                    }

                    // Check for primary key
                    let is_pk = col_info.get("primary_key")
                        .and_then(|pk| pk.as_bool())
                        .or_else(|| {
                            col_info.get("json_schema_extra")
                                .and_then(|extra| extra.get("primary_key"))
                                .and_then(|pk| pk.as_bool())
                        })
                        .unwrap_or(false);

                    if is_pk {
                        col_def.primary_key().auto_increment();
                    }

                    table_stmt.col(&mut col_def);
                }
            }
            table_stmt.build(SqliteQueryBuilder)
        };

        sqlx::query(&sql)
            .execute(pool.as_ref())
            .await
            .map_err(|e| {
                pyo3::exceptions::PyRuntimeError::new_err(format!(
                    "SQL Execution failed for '{}': {}",
                    name, e
                ))
            })?;

        println!("✅ Ferro Engine: Table '{}' created", name);
    }

    Ok(())
}

/// Registers a model's JSON schema with the Rust core.
///
/// This is typically called automatically by the `ModelMetaclass` when
/// a Pydantic model is defined.
///
/// # Errors
/// Returns a `PyErr` if the schema is invalid or if the registry is locked.
#[pyfunction]
#[pyo3(signature = (name, schema))]
pub fn register_model_schema(name: String, schema: String) -> PyResult<()> {
    let parsed_schema: serde_json::Value = serde_json::from_str(&schema).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("Invalid JSON schema: {}", e))
    })?;

    let mut registry = MODEL_REGISTRY
        .write()
        .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Model Registry"))?;

    registry.insert(name.clone(), parsed_schema);
    println!("⚙️  Ferro Engine: Map generated for '{}'", name);
    Ok(())
}

/// Manually triggers table creation for all registered models.
///
/// Returns an awaitable object (Python coroutine).
///
/// # Errors
/// Returns a `PyErr` if the engine is not initialized or if SQL execution fails.
#[pyfunction]
pub fn create_tables(py: Python<'_>) -> PyResult<Bound<'_, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let pool = {
            let engine_lock = ENGINE.read().map_err(|_| {
                pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Engine")
            })?;
            engine_lock.as_ref().cloned().ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err("Engine not initialized. Call connect() first.")
            })?
        };

        internal_create_tables(pool).await
    })
}
