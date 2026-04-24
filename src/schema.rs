//! Schema translation and registration logic.
//!
//! This module handles converting Pydantic JSON schemas into Sea-Query
//! table definitions and managing the model registry.

use crate::state::{engine_pool, sql_dialect, MODEL_REGISTRY, SqlDialect};
use pyo3::prelude::*;
use sea_query::{
    Alias, ColumnDef, ForeignKey, ForeignKeyAction, Index, PostgresQueryBuilder, SqliteQueryBuilder,
    Table,
};
use sqlx::{Any, Pool};
use std::collections::HashSet;
use std::sync::Arc;

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

fn property_json_type_and_format(col_info: &serde_json::Value) -> (Option<&str>, Option<&str>) {
    let top_type = col_info.get("type").and_then(|t| t.as_str());
    let top_format = col_info.get("format").and_then(|f| f.as_str());
    if top_type.is_some() || top_format.is_some() {
        return (top_type, top_format);
    }

    if let Some(items) = col_info.get("anyOf").and_then(|a| a.as_array()) {
        for item in items {
            let item_type = item.get("type").and_then(|t| t.as_str());
            if item_type == Some("null") {
                continue;
            }
            let item_format = item.get("format").and_then(|f| f.as_str());
            return (item_type, item_format);
        }
    }

    (None, None)
}

fn schema_dependencies(schema: &serde_json::Value) -> Vec<String> {
    let mut deps = Vec::new();
    if let Some(properties) = schema.get("properties").and_then(|p| p.as_object()) {
        for col_info in properties.values() {
            let col_info = resolve_ref(schema, col_info);
            if let Some(to_table) = col_info
                .get("foreign_key")
                .and_then(|fk| fk.get("to_table"))
                .and_then(|t| t.as_str())
            {
                deps.push(to_table.to_string());
            }
        }
    }
    deps.sort();
    deps.dedup();
    deps
}

fn order_schemas_for_creation(
    schemas: std::collections::HashMap<String, serde_json::Value>,
) -> Vec<(String, serde_json::Value)> {
    let mut remaining: Vec<(String, serde_json::Value)> = schemas.into_iter().collect();
    remaining.sort_by(|a, b| a.0.cmp(&b.0));

    let mut ordered = Vec::with_capacity(remaining.len());
    let mut created = HashSet::new();

    while !remaining.is_empty() {
        let available_names: HashSet<String> =
            remaining.iter().map(|(name, _)| name.to_lowercase()).collect();
        let mut progress = false;
        let mut index = 0;

        while index < remaining.len() {
            let deps = schema_dependencies(&remaining[index].1);
            if deps
                .iter()
                .all(|dep| created.contains(dep) || !available_names.contains(dep))
            {
                let item = remaining.remove(index);
                created.insert(item.0.to_lowercase());
                ordered.push(item);
                progress = true;
            } else {
                index += 1;
            }
        }

        if !progress {
            ordered.append(&mut remaining);
        }
    }

    ordered
}

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
            match sql_dialect() {
                SqlDialect::Sqlite => {
                    // SQLite stores booleans as integers.
                    col_def.integer();
                }
                SqlDialect::Postgres => {
                    col_def.boolean();
                }
            }
        }
        "object" | "array" => {
            col_def.text(); // SQLite stores JSON as text
        }
        _ => {
            col_def.string();
        }
    }
}

fn schema_format(col_info: &serde_json::Value) -> Option<&str> {
    col_info.get("format").and_then(|f| f.as_str()).or_else(|| {
        col_info
            .get("anyOf")
            .and_then(|a| a.as_array())
            .and_then(|variants| {
                variants
                    .iter()
                    .find_map(|variant| variant.get("format").and_then(|f| f.as_str()))
            })
    })
}

/// Unique index name for `ferro_composite_uniques`; matches Python Alembic `_build_sa_table`.
fn composite_unique_index_name(table_lower: &str, col_names: &[&str]) -> String {
    let joined = col_names.join("_");
    let raw = format!("uq_{}_{}", table_lower, joined);
    if raw.chars().count() > 63 {
        return format!("{}_uq", raw.chars().take(60).collect::<String>());
    }
    raw
}

fn append_composite_unique_index_sqls(
    table_lower: &str,
    schema: &serde_json::Value,
    index_sqls: &mut Vec<String>,
) {
    let Some(groups) = schema
        .get("ferro_composite_uniques")
        .and_then(|g| g.as_array())
    else {
        return;
    };
    for group in groups {
        let cols: Vec<&str> = group
            .as_array()
            .map(|arr| arr.iter().filter_map(|c| c.as_str()).collect())
            .unwrap_or_default();
        if cols.len() < 2 {
            crate::log_debug(format!(
                "Skipping invalid ferro_composite_uniques group for table '{}': {}",
                table_lower,
                serde_json::to_string(group).unwrap_or_else(|_| "<json>".to_string())
            ));
            continue;
        }
        let idx_name = composite_unique_index_name(table_lower, &cols);
        let mut stmt = Index::create()
            .unique()
            .name(&idx_name)
            .table(Alias::new(table_lower))
            .if_not_exists()
            .to_owned();
        for c in &cols {
            stmt.col(Alias::new(*c));
        }
        let sql = match sql_dialect() {
            SqlDialect::Sqlite => stmt.to_string(SqliteQueryBuilder),
            SqlDialect::Postgres => stmt.to_string(PostgresQueryBuilder),
        };
        index_sqls.push(sql);
    }
}

fn schema_dependencies(schema: &serde_json::Value) -> HashSet<String> {
    schema
        .get("properties")
        .and_then(|p| p.as_object())
        .map(|properties| {
            properties
                .values()
                .filter_map(|col_info| {
                    col_info
                        .get("foreign_key")
                        .and_then(|fk| fk.get("to_table"))
                        .and_then(|t| t.as_str())
                        .map(|name| name.to_lowercase())
                })
                .collect()
        })
        .unwrap_or_default()
}

fn order_schemas_for_creation(
    schemas: std::collections::HashMap<String, serde_json::Value>,
) -> Vec<(String, serde_json::Value)> {
    let mut remaining: Vec<(String, serde_json::Value)> = schemas.into_iter().collect();
    let mut created = HashSet::new();
    let mut ordered = Vec::new();

    while !remaining.is_empty() {
        let mut progressed = false;
        let mut deferred = Vec::new();

        for (name, schema) in remaining {
            let deps = schema_dependencies(&schema);
            if deps.iter().all(|dep| dep == &name.to_lowercase() || created.contains(dep)) {
                created.insert(name.to_lowercase());
                ordered.push((name, schema));
                progressed = true;
            } else {
                deferred.push((name, schema));
            }
        }

        if !progressed {
            deferred.sort_by(|(left, _), (right, _)| left.cmp(right));
            ordered.extend(deferred);
            break;
        }

        remaining = deferred;
    }

    ordered
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

    for (name, schema) in order_schemas_for_creation(schemas) {
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
                    let col_info = resolve_ref(&schema, col_info);

                    let (json_type, format) = property_json_type_and_format(col_info);

                    if let Some(t) = json_type {
                        match (t, format) {
                            ("string", Some("date-time")) => {
                                col_def.timestamp_with_time_zone();
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
                    } else {
                        col_def.string();
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
                        .get("ferro_nullable")
                        .and_then(|nullable| nullable.as_bool())
                        == Some(false)
                    {
                        col_def.not_null();
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
                        let index_stmt = Index::create()
                            .name(&index_name)
                            .table(Alias::new(&table_lower))
                            .col(Alias::new(col_name))
                            .if_not_exists()
                            .to_owned();
                        let index_sql = match sql_dialect() {
                            SqlDialect::Sqlite => index_stmt.to_string(SqliteQueryBuilder),
                            SqlDialect::Postgres => index_stmt.to_string(PostgresQueryBuilder),
                        };
                        index_sqls.push(index_sql);
                    }

                    table_stmt.col(&mut col_def);

                    // Check for Foreign Key from metadata
                    if let Some(fk_info) = col_info.get("foreign_key").and_then(|fk| fk.as_object())
                    {
                        let to_table = fk_info
                            .get("to_table")
                            .and_then(|t| t.as_str())
                            .unwrap_or("");
                        let on_delete_str = fk_info
                            .get("on_delete")
                            .and_then(|o| o.as_str())
                            .unwrap_or("CASCADE");

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

            append_composite_unique_index_sqls(&table_lower, &schema, &mut index_sqls);

            let table_sql = match sql_dialect() {
                SqlDialect::Sqlite => table_stmt.build(SqliteQueryBuilder),
                SqlDialect::Postgres => table_stmt.build(PostgresQueryBuilder),
            };
            (table_sql, index_sqls)
        };

        sqlx::query(&sql).execute(&mut *conn).await.map_err(|e| {
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

        crate::log_debug(format!("✅ Ferro Engine: Table '{}' created", name));
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
    crate::log_debug(format!("⚙️  Ferro Engine: Map generated for '{}'", name));
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
        let pool = engine_pool().ok_or_else(|| {
            pyo3::exceptions::PyRuntimeError::new_err(
                "Engine not initialized. Call connect() first.",
            )
        })?;

        internal_create_tables(pool).await
    })
}
