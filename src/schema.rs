//! Schema translation and registration logic.
//!
//! This module handles converting Pydantic JSON schemas into Sea-Query
//! table definitions and managing the model registry.

use crate::backend::EngineHandle;
use crate::state::{MODEL_REGISTRY, SqlDialect, engine_for_connection};
use pyo3::prelude::*;
use sea_query::{
    Alias, ColumnDef, ForeignKey, ForeignKeyAction, Index, PostgresQueryBuilder,
    SqliteQueryBuilder, Table,
};
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
    if top_type.is_some() {
        return (top_type, top_format);
    }

    if let Some(items) = col_info.get("anyOf").and_then(|a| a.as_array()) {
        for item in items {
            let item_type = item.get("type").and_then(|t| t.as_str());
            if item_type == Some("null") {
                continue;
            }
            let item_format = item.get("format").and_then(|f| f.as_str());
            return (item_type, item_format.or(top_format));
        }
    }

    (None, top_format)
}

fn column_bool_metadata(
    raw_col_info: &serde_json::Value,
    resolved_col_info: &serde_json::Value,
    key: &str,
) -> Option<bool> {
    raw_col_info
        .get(key)
        .or_else(|| resolved_col_info.get(key))
        .and_then(|value| value.as_bool())
}

fn column_object_metadata<'a>(
    raw_col_info: &'a serde_json::Value,
    resolved_col_info: &'a serde_json::Value,
    key: &str,
) -> Option<&'a serde_json::Map<String, serde_json::Value>> {
    raw_col_info
        .get(key)
        .or_else(|| resolved_col_info.get(key))
        .and_then(|value| value.as_object())
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
        let available_names: HashSet<String> = remaining
            .iter()
            .map(|(name, _)| name.to_lowercase())
            .collect();
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

fn json_type_to_sea_query_for_backend(
    col_def: &mut ColumnDef,
    json_type: &str,
    backend: SqlDialect,
) {
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
            match backend {
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
            col_def.json();
        }
        _ => {
            col_def.string();
        }
    }
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
    backend: SqlDialect,
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
        let sql = match backend {
            SqlDialect::Sqlite => stmt.to_string(SqliteQueryBuilder),
            SqlDialect::Postgres => stmt.to_string(PostgresQueryBuilder),
        };
        index_sqls.push(sql);
    }
}

/// Non-unique index name for `ferro_composite_indexes`; matches Python Alembic `_build_sa_table`.
fn composite_index_name(table_lower: &str, col_names: &[&str]) -> String {
    let joined = col_names.join("_");
    let raw = format!("idx_{}_{}", table_lower, joined);
    if raw.chars().count() > 63 {
        return format!("{}_idx", raw.chars().take(59).collect::<String>());
    }
    raw
}

fn append_composite_index_sqls(
    table_lower: &str,
    schema: &serde_json::Value,
    index_sqls: &mut Vec<String>,
    backend: SqlDialect,
) {
    let Some(groups) = schema
        .get("ferro_composite_indexes")
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
                "Skipping invalid ferro_composite_indexes group for table '{}': {}",
                table_lower,
                serde_json::to_string(group).unwrap_or_else(|_| "<json>".to_string())
            ));
            continue;
        }
        let idx_name = composite_index_name(table_lower, &cols);
        let mut stmt = Index::create()
            .name(&idx_name)
            .table(Alias::new(table_lower))
            .if_not_exists()
            .to_owned();
        for c in &cols {
            stmt.col(Alias::new(*c));
        }
        let sql = match backend {
            SqlDialect::Sqlite => stmt.to_string(SqliteQueryBuilder),
            SqlDialect::Postgres => stmt.to_string(PostgresQueryBuilder),
        };
        index_sqls.push(sql);
    }
}

fn build_create_table_sqls(
    name: &str,
    schema: &serde_json::Value,
    backend: SqlDialect,
) -> (String, Vec<String>) {
    let table_lower = name.to_lowercase();
    let mut table_stmt = Table::create()
        .table(Alias::new(&table_lower))
        .if_not_exists()
        .to_owned();

    let mut index_sqls = Vec::new();

    if let Some(properties) = schema.get("properties").and_then(|p| p.as_object()) {
        for (col_name, raw_col_info) in properties {
            let mut col_def = ColumnDef::new(Alias::new(col_name));
            let col_info = resolve_ref(schema, raw_col_info);

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
                    (_, Some("decimal")) => {
                        col_def.decimal();
                    }
                    ("string", Some("binary")) => {
                        col_def.blob();
                    }
                    _ => json_type_to_sea_query_for_backend(&mut col_def, t, backend),
                }
            } else {
                col_def.string();
            }

            // Check for primary key and autoincrement from our custom metadata
            let is_pk =
                column_bool_metadata(raw_col_info, col_info, "primary_key").unwrap_or(false);

            let is_auto =
                column_bool_metadata(raw_col_info, col_info, "autoincrement").unwrap_or(true);

            if is_pk {
                col_def.primary_key();
                if is_auto {
                    col_def.auto_increment();
                }
            }

            if column_bool_metadata(raw_col_info, col_info, "ferro_nullable") == Some(false) {
                col_def.not_null();
            }

            if column_bool_metadata(raw_col_info, col_info, "unique").unwrap_or(false) {
                col_def.unique_key();
            }

            if column_bool_metadata(raw_col_info, col_info, "index").unwrap_or(false) {
                let index_name = format!("idx_{}_{}", table_lower, col_name);
                let index_stmt = Index::create()
                    .name(&index_name)
                    .table(Alias::new(&table_lower))
                    .col(Alias::new(col_name))
                    .if_not_exists()
                    .to_owned();
                let index_sql = match backend {
                    SqlDialect::Sqlite => index_stmt.to_string(SqliteQueryBuilder),
                    SqlDialect::Postgres => index_stmt.to_string(PostgresQueryBuilder),
                };
                index_sqls.push(index_sql);
            }

            table_stmt.col(&mut col_def);

            // Check for Foreign Key from metadata
            if let Some(fk_info) = column_object_metadata(raw_col_info, col_info, "foreign_key") {
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

    append_composite_unique_index_sqls(&table_lower, schema, &mut index_sqls, backend);
    append_composite_index_sqls(&table_lower, schema, &mut index_sqls, backend);

    let table_sql = match backend {
        SqlDialect::Sqlite => table_stmt.build(SqliteQueryBuilder),
        SqlDialect::Postgres => table_stmt.build(PostgresQueryBuilder),
    };
    (table_sql, index_sqls)
}

/// Internal utility to create all registered tables in the database.
///
/// This is used by both the `connect(auto_migrate=True)` flow and the
/// manual `create_tables()` function.
///
/// # Errors
/// Returns a `PyErr` if the SQL execution fails.
pub async fn internal_create_tables(engine: Arc<EngineHandle>) -> PyResult<()> {
    let schemas = {
        let registry = MODEL_REGISTRY.read().map_err(|_| {
            pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Model Registry")
        })?;
        registry.clone()
    };

    let backend = engine.backend();

    for (name, schema) in order_schemas_for_creation(schemas) {
        let (sql, index_sqls) = build_create_table_sqls(&name, &schema, backend);

        engine.execute_sql(&sql).await.map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!(
                "SQL Execution failed for '{}' table: {}",
                name, e
            ))
        })?;

        for index_sql in index_sqls {
            engine.execute_sql(&index_sql).await.map_err(|e| {
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
#[pyo3(signature = (using=None))]
pub fn create_tables(py: Python<'_>, using: Option<String>) -> PyResult<Bound<'_, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let engine = engine_for_connection(using)?;
        internal_create_tables(engine).await
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn test_composite_index_name_short() {
        assert_eq!(composite_index_name("users", &["a", "b"]), "idx_users_a_b");
    }

    #[test]
    fn test_composite_index_name_at_63_chars() {
        // Build inputs that produce a name of exactly 63 chars (no truncation).
        // raw = "idx_t_<padding>_y" should be 63 chars.
        // "idx_t_" is 6 chars; "_y" is 2 chars; need 55 chars of padding.
        let pad: String = "x".repeat(55);
        let cols = [pad.as_str(), "y"];
        let result = composite_index_name("t", &cols);
        assert_eq!(result.chars().count(), 63);
        assert!(!result.ends_with("_idx") || result == format!("idx_t_{}_y", pad));
    }

    #[test]
    fn test_composite_index_name_truncation_above_63() {
        let long_a = "very_long_column_name_alpha_for_idx_truncation_test";
        let long_b = "very_long_column_name_beta_for_idx_truncation_test";
        let table = "verylongcompositeindexmodelnamefortruncation";
        let result = composite_index_name(table, &[long_a, long_b]);
        assert_eq!(result.chars().count(), 63);
        assert!(result.ends_with("_idx"));
    }

    #[test]
    fn test_composite_index_name_unicode_safe() {
        let table = "tbl_üñîçødé_with_long_table_name_for_truncation_check";
        let cols = ["α_column_one", "β_column_two_extended_for_overflow"];
        let result = composite_index_name(table, &cols);
        assert!(result.chars().count() <= 63);
    }

    #[test]
    fn test_append_composite_index_sqls_emits_non_unique() {
        for backend in [SqlDialect::Sqlite, SqlDialect::Postgres] {
            let schema = json!({"ferro_composite_indexes": [["a", "b"]]});
            let mut sqls = Vec::new();
            append_composite_index_sqls("t", &schema, &mut sqls, backend);
            assert_eq!(sqls.len(), 1);
            let sql_upper = sqls[0].to_uppercase();
            assert!(sql_upper.contains("CREATE INDEX"));
            assert!(!sql_upper.contains("CREATE UNIQUE INDEX"));
        }
    }

    #[test]
    fn test_append_composite_index_sqls_preserves_column_order() {
        let schema = json!({"ferro_composite_indexes": [["y", "x"]]});
        let mut sqls = Vec::new();
        append_composite_index_sqls("t", &schema, &mut sqls, SqlDialect::Sqlite);
        let sql = &sqls[0];
        let pos_y = sql.find("\"y\"").unwrap();
        let pos_x = sql.find("\"x\"").unwrap();
        assert!(pos_y < pos_x);
    }

    #[test]
    fn test_append_composite_index_sqls_no_groups_is_noop() {
        let schema = json!({"properties": {}});
        let mut sqls = Vec::new();
        append_composite_index_sqls("t", &schema, &mut sqls, SqlDialect::Sqlite);
        assert!(sqls.is_empty());
    }

    #[test]
    fn test_composite_index_and_unique_can_share_table() {
        let schema = json!({
            "ferro_composite_uniques": [["u1", "u2"]],
            "ferro_composite_indexes": [["i1", "i2"]]
        });
        let mut sqls = Vec::new();
        append_composite_unique_index_sqls("t", &schema, &mut sqls, SqlDialect::Sqlite);
        append_composite_index_sqls("t", &schema, &mut sqls, SqlDialect::Sqlite);
        assert_eq!(sqls.len(), 2);
        let combined = sqls.join("\n").to_uppercase();
        assert!(combined.contains("CREATE UNIQUE INDEX"));
        assert!(combined.contains("CREATE INDEX"));
        assert!(combined.contains("\"IDX_T_I1_I2\""));
    }

    #[test]
    fn test_foreign_key_column_with_index_flag_emits_create_index() {
        let schema = json!({
            "properties": {
                "id": {
                    "type": "integer",
                    "primary_key": true,
                    "autoincrement": true
                },
                "org_id": {
                    "type": "integer",
                    "ferro_nullable": false,
                    "index": true,
                    "foreign_key": {
                        "to_table": "org",
                        "on_delete": "CASCADE",
                        "unique": false
                    }
                }
            }
        });

        for backend in [SqlDialect::Sqlite, SqlDialect::Postgres] {
            let (_table_sql, index_sqls) = build_create_table_sqls("project", &schema, backend);
            let joined = index_sqls.join("\n").to_uppercase();
            assert!(
                joined.contains("CREATE INDEX"),
                "expected CREATE INDEX for FK column with index=true, got {:?} ({:?})",
                index_sqls,
                backend
            );
            assert!(
                joined.contains("IDX_PROJECT_ORG_ID"),
                "expected idx_project_org_id index name, got {:?} ({:?})",
                index_sqls,
                backend
            );
            assert!(
                !joined.contains("CREATE UNIQUE INDEX"),
                "FK column with index=true (not unique=true) must not emit a unique index; got {:?}",
                index_sqls
            );
        }
    }
}
