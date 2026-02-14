//! Schema translation and registration logic.
//!
//! This module handles converting Pydantic JSON schemas into Sea-Query
//! table definitions and managing the model registry.

use crate::state::{ENGINE, MODEL_REGISTRY};
use pyo3::prelude::*;
use sea_query::{Alias, ColumnDef, Index, SqliteQueryBuilder, Table, ForeignKey, ForeignKeyAction};
use sqlx::{Any, Pool};
use std::sync::Arc;

/// Maps a JSON schema type string to a Sea-Query `ColumnDef`.
pub fn json_type_to_sea_query(col_def: &mut ColumnDef, json_type: &str) {
    match json_type {
        "integer" => {
            col_def.integer();
        }
        "string" => {
            col_def.string();
        }
        "number" => {
            col_def.double();
        }
        "boolean" => {
            // CX Choice: Use integer for booleans to satisfy SQLx Any driver quirks with SQLite.
            col_def.integer();
        }
        "object" | "array" => {
            col_def.text(); // SQLite stores JSON as text
        }
        _ => {
            col_def.string();
        }
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

    let mut conn = pool.acquire().await.map_err(|e| {
        pyo3::exceptions::PyRuntimeError::new_err(format!("Failed to acquire connection: {}", e))
    })?;

    for (name, schema) in schemas {
        let (sql, index_sqls) = {
            let table_lower = name.to_lowercase();
            let mut table_stmt = Table::create()
                .table(Alias::new(&table_lower))
                .if_not_exists()
                .to_owned();

            let mut index_sqls = Vec::new();

            if let Some(properties) = schema.get("properties").and_then(|p| p.as_object()) {
                for (col_name, col_info) in properties {
                    let mut col_def = ColumnDef::new(Alias::new(col_name));

                    let json_type = col_info.get("type").and_then(|t| t.as_str()).or_else(|| {
                        // Check anyOf for a type (common in Pydantic V2 for optional fields)
                        col_info.get("anyOf").and_then(|a| a.as_array()).and_then(|types| {
                            types.iter().find_map(|t| {
                                let s = t.get("type")?.as_str()?;
                                if s != "null" {
                                    Some(s)
                                } else {
                                    None
                                }
                            })
                        })
                    });

                    let format = col_info.get("format").and_then(|f| f.as_str());

                    if let Some(t) = json_type {
                        match (t, format) {
                            ("string", Some("date-time")) => {
                                col_def.date_time();
                            }
                            ("string", Some("date")) => {
                                col_def.date();
                            }
                            ("string", Some("uuid")) => {
                                col_def.uuid();
                            }
                            ("string", Some("binary")) => {
                                col_def.blob();
                            }
                            _ => json_type_to_sea_query(&mut col_def, t),
                        }
                    }

                    // Check for primary key and autoincrement from our custom metadata
                    let is_pk = col_info
                        .get("primary_key")
                        .and_then(|pk| pk.as_bool())
                        .unwrap_or(false);

                    let is_auto = col_info
                        .get("autoincrement")
                        .and_then(|auto| auto.as_bool())
                        .unwrap_or(true);

                    if is_pk {
                        col_def.primary_key();
                        if is_auto {
                            col_def.auto_increment();
                        }
                    }

                    if col_info
                        .get("unique")
                        .and_then(|u| u.as_bool())
                        .unwrap_or(false)
                    {
                        col_def.unique_key();
                    }

                    if col_info
                        .get("index")
                        .and_then(|i| i.as_bool())
                        .unwrap_or(false)
                    {
                        let index_name = format!("idx_{}_{}", table_lower, col_name);
                        let index_sql = Index::create()
                            .name(&index_name)
                            .table(Alias::new(&table_lower))
                            .col(Alias::new(col_name))
                            .if_not_exists()
                            .to_string(SqliteQueryBuilder);
                        index_sqls.push(index_sql);
                    }

                    table_stmt.col(&mut col_def);

                    // Check for Foreign Key from metadata
                    if let Some(fk_info) = col_info.get("foreign_key").and_then(|fk| fk.as_object()) {
                        let to_table = fk_info.get("to_table").and_then(|t| t.as_str()).unwrap_or("");
                        let on_delete_str = fk_info.get("on_delete").and_then(|o| o.as_str()).unwrap_or("CASCADE");
                        
                        let action = match on_delete_str.to_uppercase().as_str() {
                            "RESTRICT" => ForeignKeyAction::Restrict,
                            "SET NULL" => ForeignKeyAction::SetNull,
                            "SET DEFAULT" => ForeignKeyAction::SetDefault,
                            "NO ACTION" => ForeignKeyAction::NoAction,
                            _ => ForeignKeyAction::Cascade, // Default
                        };

                        let mut fk_stmt = ForeignKey::create();
                        fk_stmt
                            .from(Alias::new(&table_lower), Alias::new(col_name))
                            .to(Alias::new(to_table), Alias::new("id")) // CX Choice: Assume target PK is 'id' for now
                            .on_delete(action);
                        
                        table_stmt.foreign_key(&mut fk_stmt);
                    }
                }
            }
            (
                table_stmt.build(SqliteQueryBuilder),
                index_sqls,
            )
        };

        sqlx::query(&sql)
            .execute(&mut *conn)
            .await
            .map_err(|e| {
                pyo3::exceptions::PyRuntimeError::new_err(format!(
                    "SQL Execution failed for '{}' table: {}",
                    name, e
                ))
            })?;

        for index_sql in index_sqls {
            sqlx::query(&index_sql)
                .execute(&mut *conn)
                .await
                .map_err(|e| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!(
                        "SQL Execution failed for '{}' index: {}",
                        name, e
                    ))
                })?;
        }

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
            let engine_lock = ENGINE
                .read()
                .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Engine"))?;
            engine_lock.as_ref().cloned().ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err(
                    "Engine not initialized. Call connect() first.",
                )
            })?
        };

        internal_create_tables(pool).await
    })
}
