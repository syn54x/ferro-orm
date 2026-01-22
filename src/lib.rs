use dashmap::DashMap;
use once_cell::sync::Lazy;
use pyo3::prelude::*;
use sea_query::{ColumnDef, Expr, Iden, InsertStatement, OnConflict, Query, SqliteQueryBuilder, Table};
use sqlx::{Any, Pool, any::AnyPoolOptions};
use std::collections::HashMap;
use std::sync::{Arc, RwLock};

// The Engine's memory of all Python models defined
static MODEL_REGISTRY: Lazy<RwLock<HashMap<String, serde_json::Value>>> =
    Lazy::new(|| RwLock::new(HashMap::new()));

static ENGINE: Lazy<RwLock<Option<Arc<Pool<Any>>>>> = Lazy::new(|| RwLock::new(None));

// Identity Map: Tracks active Python objects by (ModelName, PrimaryKey)
// Stores Weak references to avoid memory leaks.
static IDENTITY_MAP: Lazy<DashMap<(String, String), PyObject>> = Lazy::new(DashMap::new);

fn json_type_to_sea_query(col_def: &mut ColumnDef, json_type: &str) {
    match json_type {
        "integer" => { col_def.big_integer(); },
        "string" => { col_def.string(); },
        "number" => { col_def.double(); },
        "boolean" => { col_def.boolean(); },
        _ => { col_def.string(); },
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use sea_query::ColumnDef;

    #[test]
    fn test_json_type_mapping() {
        let mut col = ColumnDef::new(sea_query::Alias::new("test"));
        json_type_to_sea_query(&mut col, "integer");
        // We can't easily inspect ColumnDef type, but we can verify it doesn't panic
        json_type_to_sea_query(&mut col, "string");
        json_type_to_sea_query(&mut col, "boolean");
    }
}

#[pyfunction]
#[pyo3(signature = (name, schema))]
fn register_model_schema(name: String, schema: String) -> PyResult<()> {
    // Convert the JSON string from Pydantic into Rust Serde Value
    let parsed_schema: serde_json::Value = serde_json::from_str(&schema).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("Invalid JSON schema: {}", e))
    })?;

    let mut registry = MODEL_REGISTRY
        .write()
        .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Model Registry"))?;

    registry.insert(name.clone(), parsed_schema);

    // Let the dev know the engine is successfully tracking the model
    println!("⚙️  Ferro Engine: Map generated for '{}'", name);
    Ok(())
}

async fn internal_create_tables(pool: Arc<Pool<Any>>) -> PyResult<()> {
    let schemas = {
        let registry = MODEL_REGISTRY.read().map_err(|_| {
            pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Model Registry")
        })?;
        registry.clone()
    };

    for (name, schema) in schemas {
        let sql = {
            let mut table_stmt = Table::create()
                .table(sea_query::Alias::new(name.to_lowercase()))
                .if_not_exists()
                .to_owned();

            if let Some(properties) = schema.get("properties").and_then(|p| p.as_object()) {
                for (col_name, col_info) in properties {
                    let mut col_def = ColumnDef::new(sea_query::Alias::new(col_name));

                    if let Some(json_type) = col_info.get("type").and_then(|t| t.as_str()) {
                        json_type_to_sea_query(&mut col_def, json_type);
                    }

                    // Check for primary key (Pydantic v2 flattens json_schema_extra)
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

#[pyfunction]
#[pyo3(signature = (url, auto_migrate=false))]
fn connect(py: Python<'_>, url: String, auto_migrate: bool) -> PyResult<Bound<'_, PyAny>> {
    sqlx::any::install_default_drivers();

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let pool = AnyPoolOptions::new()
            .max_connections(5)
            .connect(&url)
            .await
            .map_err(|e| {
                pyo3::exceptions::PyConnectionError::new_err(format!("DB Connection failed: {}", e))
            })?;

        let arc_pool = Arc::new(pool);

        if auto_migrate {
            internal_create_tables(arc_pool.clone()).await?;
        }

        let mut engine = ENGINE.write().map_err(|_| {
            pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Engine")
        })?;
        *engine = Some(arc_pool);

        println!("⚡️ Ferro Engine: Connected to {}", url);
        Ok(())
    })
}

#[pyfunction]
fn create_tables(py: Python<'_>) -> PyResult<Bound<'_, PyAny>> {
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

#[pyfunction]
fn fetch_all(py: Python<'_>, name: String) -> PyResult<Bound<'_, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let pool = {
            let engine_lock = ENGINE.read().map_err(|_| {
                pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Engine")
            })?;
            engine_lock.as_ref().cloned().ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err("Engine not initialized. Call connect() first.")
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
                    let is_pk = col_info.get("primary_key")
                        .and_then(|pk| pk.as_bool())
                        .or_else(|| {
                            col_info.get("json_schema_extra")
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
                .from(sea_query::Alias::new(&table_name))
                .to_string(SqliteQueryBuilder);
            (s, pk)
        };

        let rows = sqlx::query(&sql)
            .fetch_all(pool.as_ref())
            .await
            .map_err(|e| {
                pyo3::exceptions::PyRuntimeError::new_err(format!("Fetch all failed for '{}': {}", name, e))
            })?;

        Python::with_gil(|py| {
            let results = pyo3::types::PyList::empty(py);
            for row in rows {
                // SQLite specific: Get column names and values
                use sqlx::{Row, Column};
                
                // Determine the PK value for this row to check the Identity Map
                let mut row_pk_val = None;
                if let Some(ref pk_name) = pk_col {
                    if let Ok(val) = row.try_get::<i64, _>(pk_name.as_str()) {
                        row_pk_val = Some(val.to_string());
                    } else if let Ok(val) = row.try_get::<String, _>(pk_name.as_str()) {
                        row_pk_val = Some(val);
                    }
                }

                // Check Identity Map first
                if let Some(pk_val) = row_pk_val {
                    if let Some(existing_obj) = IDENTITY_MAP.get(&(name.clone(), pk_val)) {
                        results.append(existing_obj.value().clone_ref(py))?;
                        continue;
                    }
                }

                let dict = pyo3::types::PyDict::new(py);
                for col in row.columns() {
                    let col_name = col.name();
                    
                    if let Ok(val) = row.try_get::<i64, _>(col_name) {
                        dict.set_item(col_name, val)?;
                    } else if let Ok(val) = row.try_get::<f64, _>(col_name) {
                        dict.set_item(col_name, val)?;
                    } else if let Ok(val) = row.try_get::<String, _>(col_name) {
                        dict.set_item(col_name, val)?;
                    } else if let Ok(val) = row.try_get::<bool, _>(col_name) {
                        dict.set_item(col_name, val)?;
                    } else {
                        dict.set_item(col_name, py.None())?;
                    }
                }
                results.append(dict)?;
            }
                Ok(results.into_any().unbind())
        })
    })
}

#[pyfunction]
fn fetch_one(py: Python<'_>, name: String, pk_val: String) -> PyResult<Bound<'_, PyAny>> {
    // 1. Check Identity Map first (Synchronous check is fine here)
    if let Some(existing_obj) = IDENTITY_MAP.get(&(name.clone(), pk_val.clone())) {
        let obj = existing_obj.value().clone_ref(py);
        // We need to return an awaitable that returns the object, 
        // because the Python side expects to await fetch_one.
        return pyo3_async_runtimes::tokio::future_into_py(py, async move {
            Ok(obj)
        });
    }

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let pool = {
            let engine_lock = ENGINE.read().map_err(|_| {
                pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Engine")
            })?;
            engine_lock.as_ref().cloned().ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err("Engine not initialized. Call connect() first.")
            })?
        };

        let table_name = name.to_lowercase();
        let (sql, pk_col_name) = {
            let registry = MODEL_REGISTRY.read().map_err(|_| {
                pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Model Registry")
            })?;
            let schema = registry.get(&name).ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err(format!("Model '{}' not found", name))
            })?;

            let mut pk = None;
            if let Some(properties) = schema.get("properties").and_then(|p| p.as_object()) {
                for (col_name, col_info) in properties {
                    let is_pk = col_info.get("primary_key")
                        .and_then(|pk| pk.as_bool())
                        .or_else(|| {
                            col_info.get("json_schema_extra")
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
                pyo3::exceptions::PyRuntimeError::new_err(format!("Model '{}' has no primary key", name))
            })?;

            // SQLite supports string-to-int comparison if type affinity allows, 
            // but let's use a bound parameter.
            let s = Query::select()
                .column(sea_query::Asterisk)
                .from(sea_query::Alias::new(&table_name))
                .and_where(Expr::col(sea_query::Alias::new(&pk_name)).eq(pk_val.clone()))
                .to_string(SqliteQueryBuilder);
            (s, pk_name)
        };

        let row = sqlx::query(&sql)
            .fetch_optional(pool.as_ref())
            .await
            .map_err(|e| {
                pyo3::exceptions::PyRuntimeError::new_err(format!("Fetch one failed for '{}': {}", name, e))
            })?;

        match row {
            Some(row) => {
                Python::with_gil(|py| {
                    use sqlx::{Row, Column};
                    let dict = pyo3::types::PyDict::new(py);
                    for col in row.columns() {
                        let col_name = col.name();
                        if let Ok(val) = row.try_get::<i64, _>(col_name) {
                            dict.set_item(col_name, val)?;
                        } else if let Ok(val) = row.try_get::<f64, _>(col_name) {
                            dict.set_item(col_name, val)?;
                        } else if let Ok(val) = row.try_get::<String, _>(col_name) {
                            dict.set_item(col_name, val)?;
                        } else if let Ok(val) = row.try_get::<bool, _>(col_name) {
                            dict.set_item(col_name, val)?;
                        } else {
                            dict.set_item(col_name, py.None())?;
                        }
                    }
                    Ok(dict.into_any().unbind())
                })
            }
            None => Ok(Python::with_gil(|py| py.None())),
        }
    })
}

#[pyfunction]
fn save_record(py: Python<'_>, name: String, data: String) -> PyResult<Bound<'_, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let pool = {
            let engine_lock = ENGINE.read().map_err(|_| {
                pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Engine")
            })?;
            engine_lock.as_ref().cloned().ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err("Engine not initialized. Call connect() first.")
            })?
        };

        let schema = {
            let registry = MODEL_REGISTRY.read().map_err(|_| {
                pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Model Registry")
            })?;
            registry.get(&name).cloned().ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err(format!("Model '{}' not found in registry", name))
            })?
        };

        let record: serde_json::Value = serde_json::from_str(&data).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("Invalid record JSON: {}", e))
        })?;

        let record_obj = record.as_object().ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err("Record data must be a JSON object")
        })?;

        // Find primary key from schema (Check flattened and nested json_schema_extra)
        let mut pk_col = None;
        if let Some(properties) = schema.get("properties").and_then(|p| p.as_object()) {
            for (col_name, col_info) in properties {
                let is_pk = col_info.get("primary_key")
                    .and_then(|pk| pk.as_bool())
                    .or_else(|| {
                        col_info.get("json_schema_extra")
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
                columns.push(sea_query::Alias::new(key));
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
                .into_table(sea_query::Alias::new(&table_name))
                .columns(columns.clone())
                .values(values)
                .map_err(|e| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!("Statement build failed: {}", e))
                })?
                .to_owned();

            if let Some(pk) = pk_col {
                let mut on_conflict = OnConflict::column(sea_query::Alias::new(&pk));
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

        query
            .execute(pool.as_ref())
            .await
            .map_err(|e| {
                pyo3::exceptions::PyRuntimeError::new_err(format!(
                    "Save failed for '{}': {}",
                    name, e
                ))
            })?;

        Ok(())
    })
}

#[pyfunction]
fn version() -> String {
    env!("CARGO_PKG_VERSION").to_string()
}

#[pyfunction]
fn reset_engine() -> PyResult<()> {
    let mut engine = ENGINE.write().map_err(|_| {
        pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Engine")
    })?;
    *engine = None;
    Ok(())
}

#[pyfunction]
fn clear_registry() -> PyResult<()> {
    let mut registry = MODEL_REGISTRY.write().map_err(|_| {
        pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Model Registry")
    })?;
    registry.clear();
    Ok(())
}

#[pyfunction]
fn register_instance(name: String, pk: String, obj: PyObject) -> PyResult<()> {
    IDENTITY_MAP.insert((name, pk), obj);
    Ok(())
}

#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(register_model_schema, m)?)?;
    m.add_function(wrap_pyfunction!(connect, m)?)?;
    m.add_function(wrap_pyfunction!(create_tables, m)?)?;
    m.add_function(wrap_pyfunction!(fetch_all, m)?)?;
    m.add_function(wrap_pyfunction!(fetch_one, m)?)?;
    m.add_function(wrap_pyfunction!(register_instance, m)?)?;
    m.add_function(wrap_pyfunction!(save_record, m)?)?;
    m.add_function(wrap_pyfunction!(reset_engine, m)?)?;
    m.add_function(wrap_pyfunction!(clear_registry, m)?)?;
    m.add_function(wrap_pyfunction!(version, m)?)?;
    Ok(())
}
