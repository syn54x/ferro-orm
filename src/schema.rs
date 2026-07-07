//! Schema translation and registration logic.
//!
//! This module handles converting Pydantic JSON schemas into Sea-Query
//! table definitions and managing the model registry.

use crate::backend::EngineHandle;
use crate::state::{Dialect, MODEL_REGISTRY, engine_for_connection};
use ferro_ddl_lowering::{
    CanonicalType, ResolvedStorage, canonical_from_parts, db_check_constraint_name,
    db_type_token_to_canonical, enum_label_strings, render_db_check, single_index_name,
};
use ferro_schema_ir::SchemaCheck;
use pyo3::prelude::*;
use sea_query::{
    Alias, ForeignKeyAction, Index, PostgresQueryBuilder,
    SqliteQueryBuilder,
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

pub(crate) fn property_json_type_and_format(
    col_info: &serde_json::Value,
) -> (Option<&str>, Option<&str>) {
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

/// Map raw JSON Schema `(type, format)` to the domain `logical_type` tokens
/// emitted by the Python SchemaIR compiler (`compiler.py::_logical_type`).
pub(crate) fn json_schema_logical_type(json_type: &str, format: Option<&str>) -> &'static str {
    match (json_type, format) {
        ("integer", _) => "integer",
        ("number", Some("decimal")) => "decimal",
        ("number", _) => "number",
        ("boolean", _) => "boolean",
        ("string", Some("date-time")) => "datetime",
        ("string", Some("date")) => "date",
        ("string", Some("time")) => "time",
        ("string", Some("uuid")) => "uuid",
        ("string", Some("binary")) => "binary",
        ("string", _) => "string",
        ("object" | "array", _) => "json",
        _ => "unknown",
    }
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

pub(crate) fn order_schemas_for_creation(
    schemas: std::collections::HashMap<String, std::sync::Arc<crate::state::RegisteredModel>>,
) -> Vec<(String, std::sync::Arc<crate::state::RegisteredModel>)> {
    let mut remaining: Vec<(String, std::sync::Arc<crate::state::RegisteredModel>)> =
        schemas.into_iter().collect();
    remaining.sort_by(|a, b| a.0.cmp(&b.0));

    let mut ordered = Vec::with_capacity(remaining.len());
    let mut created = HashSet::new();

    while !remaining.is_empty() {
        let available_names: HashSet<String> = remaining
            .iter()
            .map(|(_, model)| model.table_name.clone())
            .collect();
        let mut progress = false;
        let mut index = 0;

        while index < remaining.len() {
            let deps = schema_dependencies(&remaining[index].1.schema);
            if deps
                .iter()
                .all(|dep| created.contains(dep) || !available_names.contains(dep))
            {
                let item = remaining.remove(index);
                let table = item.1.table_name.clone();
                created.insert(table);
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


// Artifact naming (idx_/uq_/ck_/fk_ builders and their 63-char guards) is
// single-sourced in `ferro-ddl-lowering` (FF-B B3) and imported at the top.

/// Resolve a model property to its backend-specific [`CanonicalType`] via the
/// shared lowering crate. `db_type` (when set) wins; otherwise the Pydantic
/// JSON type/format cascade decides; unknown types fall back to `varchar`.
pub(crate) fn canonical_column_type(
    raw_col_info: &serde_json::Value,
    resolved_col_info: &serde_json::Value,
    backend: Dialect,
) -> CanonicalType {
    let db_type = raw_col_info
        .get("db_type")
        .or_else(|| resolved_col_info.get("db_type"))
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let (json_type, format) = property_json_type_and_format(resolved_col_info);
    let logical_type = json_schema_logical_type(json_type.unwrap_or(""), format);
    canonical_from_parts(logical_type, None, db_type, backend)
        .unwrap_or(CanonicalType::Varchar(None))
}

/// Resolve a model property's full storage decision from the enriched JSON
/// schema — the legacy-planner counterpart of the IR path's
/// `resolve_column_storage` (FF-B B2): an explicit `db_type` wins, then
/// `enum` values select native Postgres enum storage (varchar(max label len)
/// on SQLite, matching SQLAlchemy), then the scalar cascade decides.
pub(crate) fn resolve_column_storage_json(
    col_name: &str,
    raw_col_info: &serde_json::Value,
    resolved_col_info: &serde_json::Value,
    backend: Dialect,
) -> ResolvedStorage {
    let db_type = raw_col_info
        .get("db_type")
        .or_else(|| resolved_col_info.get("db_type"))
        .and_then(|v| v.as_str())
        .unwrap_or("");
    if let Some(canonical) = db_type_token_to_canonical(db_type, backend) {
        return ResolvedStorage::Scalar(canonical);
    }
    let enum_values = resolved_col_info
        .get("enum")
        .or_else(|| raw_col_info.get("enum"))
        .and_then(|v| v.as_array())
        .filter(|v| !v.is_empty());
    if let Some(values) = enum_values {
        let labels = enum_label_strings(values);
        return match backend {
            Dialect::Postgres => {
                let type_name = raw_col_info
                    .get("enum_type_name")
                    .or_else(|| resolved_col_info.get("enum_type_name"))
                    .and_then(|v| v.as_str())
                    .unwrap_or(col_name)
                    .to_string();
                ResolvedStorage::PgEnum { type_name, labels }
            }
            Dialect::Sqlite => {
                let max_len = labels.iter().map(|l| l.chars().count()).max().unwrap_or(0);
                let max_len = u32::try_from(max_len).unwrap_or(u32::MAX);
                ResolvedStorage::Scalar(CanonicalType::Varchar(if max_len == 0 {
                    None
                } else {
                    Some(max_len)
                }))
            }
        };
    }
    ResolvedStorage::Scalar(canonical_column_type(raw_col_info, resolved_col_info, backend))
}

/// Pre-rendered SQL literal tokens for a db_check column's allowed values,
/// e.g. `["'admin'", "'user'"]`. Shape matches [`SchemaCheck::values`] so this
/// legacy JSON path can feed the same emitter as the IR path.
fn render_check_values(col_info: &serde_json::Value) -> Option<Vec<String>> {
    let values = col_info.get("enum").and_then(|v| v.as_array())?;
    if values.is_empty() {
        return None;
    }
    let rendered: Vec<String> = values
        .iter()
        .map(|v| match v {
            serde_json::Value::String(s) => format!("'{}'", s.replace('\'', "''")),
            serde_json::Value::Number(n) => n.to_string(),
            serde_json::Value::Bool(b) => b.to_string(),
            other => format!("'{}'", other.to_string().replace('\'', "''")),
        })
        .collect();
    Some(rendered)
}

fn build_check_constraint_sql(
    table_lower: &str,
    col_name: &str,
    col_info: &serde_json::Value,
    backend: Dialect,
) -> Option<String> {
    // SQLite cannot ALTER TABLE ADD CONSTRAINT and adding a named CHECK
    // requires CREATE TABLE rebuild. db_check is a Postgres-first feature in
    // Phase 1; SQLite users opting in just see the constraint elided at
    // runtime. The parity test (U5) compares Postgres-side rendering.
    //
    // Delegate the actual wrapper to `render_db_check` — the single source
    // shared with the IR emitter — so this legacy JSON path stays byte-identical
    // (including the idempotent DO-block guard) and the shadow comparator sees
    // matching plans.
    let values = render_check_values(col_info)?;
    let check = SchemaCheck {
        name: db_check_constraint_name(table_lower, col_name),
        column: col_name.to_string(),
        values,
    };
    render_db_check(table_lower, &check, backend).statement
}

/// Foreign-key metadata for a column, resolved from the model schema.
#[derive(Clone, Debug)]
pub(crate) struct FkSpec {
    pub to_table: String,
    pub on_delete: ForeignKeyAction,
}

/// Everything any DDL path needs to know about one model column, built once
/// so CREATE TABLE and ALTER TABLE ADD COLUMN emit byte-identical column
/// definitions (AGENTS.md § I-1).
pub(crate) struct ColumnPlan {
    pub storage: ResolvedStorage,
    pub is_primary_key: bool,
    pub is_nullable: bool,
    pub is_unique: bool,
    /// Post-create SQL owned by this column, in emission order:
    /// single-column index, then `db_check` CHECK constraint.
    pub index_sqls: Vec<String>,
    pub fk: Option<FkSpec>,
    /// Literal default from the JSON schema (the Pydantic field default).
    /// CREATE TABLE never emits server defaults; the ALTER path uses this to
    /// backfill NOT NULL column adds on populated tables.
    pub literal_default: Option<serde_json::Value>,
}

pub(crate) fn build_column_plan(
    table_lower: &str,
    col_name: &str,
    raw_col_info: &serde_json::Value,
    schema: &serde_json::Value,
    backend: Dialect,
) -> ColumnPlan {
    let col_info = resolve_ref(schema, raw_col_info);
    let storage = resolve_column_storage_json(col_name, raw_col_info, col_info, backend);

    let is_primary_key =
        column_bool_metadata(raw_col_info, col_info, "primary_key").unwrap_or(false);

    let is_nullable = column_bool_metadata(raw_col_info, col_info, "ferro_nullable") != Some(false);

    let is_unique = column_bool_metadata(raw_col_info, col_info, "unique").unwrap_or(false);

    let mut index_sqls = Vec::new();
    if column_bool_metadata(raw_col_info, col_info, "index").unwrap_or(false) {
        let index_name = single_index_name(table_lower, col_name);
        let index_stmt = Index::create()
            .name(&index_name)
            .table(Alias::new(table_lower))
            .col(Alias::new(col_name))
            .if_not_exists()
            .to_owned();
        let index_sql = match backend {
            Dialect::Sqlite => index_stmt.to_string(SqliteQueryBuilder),
            Dialect::Postgres => index_stmt.to_string(PostgresQueryBuilder),
        };
        index_sqls.push(index_sql);
    }

    // db_check=True -> single-column CHECK constraint named ck_<table>_<col>.
    // Emitted (via the shared `render_db_check`) as a post-create ALTER TABLE
    // wrapped in an idempotent DO-block guard, so the name flows through
    // identically on both backends and a re-run is a no-op. SQLite cannot
    // execute ADD CONSTRAINT; users opting in to db_check are expected to be on
    // Postgres for Phase 1.
    let db_check = column_bool_metadata(raw_col_info, col_info, "db_check").unwrap_or(false);
    if db_check
        && let Some(ck_sql) = build_check_constraint_sql(table_lower, col_name, col_info, backend)
    {
        index_sqls.push(ck_sql);
    }

    let fk = column_object_metadata(raw_col_info, col_info, "foreign_key").map(|fk_info| {
        let to_table = fk_info
            .get("to_table")
            .and_then(|t| t.as_str())
            .unwrap_or("");
        let on_delete_str = fk_info
            .get("on_delete")
            .and_then(|o| o.as_str())
            .unwrap_or("CASCADE");
        let on_delete = match on_delete_str.to_uppercase().as_str() {
            "RESTRICT" => ForeignKeyAction::Restrict,
            "SET NULL" => ForeignKeyAction::SetNull,
            "SET DEFAULT" => ForeignKeyAction::SetDefault,
            "NO ACTION" => ForeignKeyAction::NoAction,
            _ => ForeignKeyAction::Cascade, // Default
        };
        FkSpec {
            to_table: to_table.to_string(),
            on_delete,
        }
    });

    let literal_default = raw_col_info
        .get("default")
        .or_else(|| col_info.get("default"))
        .cloned();

    ColumnPlan {
        storage,
        is_primary_key,
        is_nullable,
        is_unique,
        index_sqls,
        fk,
        literal_default,
    }
}

/// Internal utility to create all registered tables in the database.
///
/// This is used by both the `connect(auto_migrate=True)` flow and the
/// manual `create_tables()` function.
///
/// # Errors
/// Returns a `PyErr` if the SQL execution fails.
pub async fn internal_create_tables(engine: Arc<EngineHandle>) -> PyResult<()> {
    // The runtime CREATE TABLE path is emitted from the Python-compiled SchemaIR
    // via the shared `ferro_migrate` emitter (issue #153). The modelset must have
    // been pushed by the `connect`/`create_tables` Python wrappers first — a
    // missing modelset is a loud error, never a silent empty create.
    let modelset = {
        let guard = crate::state::SCHEMA_IR_MODELSET.read().map_err(|_| {
            pyo3::exceptions::PyRuntimeError::new_err("Failed to lock SchemaIR modelset")
        })?;
        guard.clone().ok_or_else(|| {
            pyo3::exceptions::PyRuntimeError::new_err(
                "SchemaIR modelset not set — connect()/create_tables() must push it before creating tables",
            )
        })?
    };

    let dialect = engine.backend();

    let model_refs: Vec<&ferro_schema_ir::SchemaModel> =
        modelset.payload.models.iter().collect();
    for model in ferro_migrate::order_models_for_create(&model_refs) {
        let emission = ferro_migrate::render_create_table(model, dialect).map_err(|err| {
            pyo3::exceptions::PyRuntimeError::new_err(format!(
                "CREATE TABLE emission failed for '{}': {}",
                model.table_name, err.message
            ))
        })?;

        for pre_sql in &emission.pre_create_sqls {
            engine.execute_sql_unprepared(pre_sql).await.map_err(|e| {
                crate::errors::map_db_error(
                    &format!("SQL Execution failed for '{}' enum type", model.table_name),
                    e,
                )
            })?;
        }

        engine.execute_sql(&emission.create_sql).await.map_err(|e| {
            crate::errors::map_db_error(
                &format!("SQL Execution failed for '{}' table", model.table_name),
                e,
            )
        })?;

        for post_sql in &emission.post_create_sqls {
            engine.execute_sql(post_sql).await.map_err(|e| {
                crate::errors::map_db_error(
                    &format!("SQL Execution failed for '{}' index", model.table_name),
                    e,
                )
            })?;
        }

        for warning in &emission.warnings {
            crate::emit_user_warning(warning);
        }

        crate::log_debug(format!("✅ Ferro Engine: Table '{}' created", model.table_name));
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
#[pyo3(signature = (name, schema, table_name))]
pub fn register_model_schema(name: String, schema: String, table_name: String) -> PyResult<()> {
    let parsed_schema: serde_json::Value = serde_json::from_str(&schema).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("Invalid JSON schema: {}", e))
    })?;
    if table_name.is_empty() {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "table_name must be a non-empty string",
        ));
    }

    let mut registry = MODEL_REGISTRY
        .write()
        .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Model Registry"))?;

    registry.insert(
        name.clone(),
        crate::state::RegisteredModel::new(parsed_schema, table_name),
    );
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

/// Test-only helper: render the Rust emitter's CREATE TABLE SQL plus any
/// post-create SQL fragments (CHECK constraints, composite indexes) without
/// requiring a live database. Used by the cross-emitter parity test (U5 of
/// the configurable-column-storage plan) to assert that the Rust and Alembic
/// emitters agree on every `(canonical_token, dialect)` pair.
///
/// `dialect` must be `"postgres"` or `"sqlite"`. Anything else raises
/// `ValueError`. `schema_json` is a SchemaIR *payload* JSON string of the shape
/// `{"dialect_agnostic": bool, "models": [<SchemaModel>...]}` produced by
/// `ferro.ir.compiler.compile_schema_ir_payload`. The model matching `name`
/// (by `model_name`/`table_name`, falling back to the first) is rendered through
/// the same `ferro_migrate::render_create_table` emitter the runtime uses.
///
/// # Errors
/// Returns a `PyErr` when the JSON cannot be parsed, the dialect is
/// unrecognized, the payload has no models, or the emitter fails.
#[pyfunction]
#[pyo3(name = "_render_create_table_sql_for_test")]
pub fn _render_create_table_sql_for_test(
    name: String,
    schema_json: String,
    dialect: String,
) -> PyResult<(String, Vec<String>, Vec<String>)> {
    let dialect = match dialect.as_str() {
        "postgres" => Dialect::Postgres,
        "sqlite" => Dialect::Sqlite,
        other => {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "Unknown dialect {:?}; expected 'postgres' or 'sqlite'",
                other
            )));
        }
    };
    let payload: ferro_schema_ir::SchemaIrPayload = serde_json::from_str(&schema_json)
        .map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("Invalid SchemaIR payload: {}", e))
        })?;
    let table_lower = name.to_lowercase();
    let model = payload
        .models
        .iter()
        .find(|m| m.model_name == name || m.table_name == table_lower)
        .or_else(|| payload.models.first())
        .ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err(format!(
                "SchemaIR payload for {:?} contains no models",
                name
            ))
        })?;
    let emission = ferro_migrate::render_create_table(model, dialect).map_err(|err| {
        pyo3::exceptions::PyRuntimeError::new_err(format!(
            "CREATE TABLE emission failed for '{}': {}",
            model.table_name, err.message
        ))
    })?;
    Ok((
        emission.create_sql,
        emission.post_create_sqls,
        emission.pre_create_sqls,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use ferro_ddl_lowering::composite_index_name;
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
    fn test_db_check_constraint_name_short() {
        assert_eq!(db_check_constraint_name("doc", "format"), "ck_doc_format");
    }

    #[test]
    fn test_db_check_constraint_name_truncates_above_63() {
        let long_col = "a".repeat(70);
        let result = db_check_constraint_name("verylongtable", &long_col);
        assert_eq!(result.chars().count(), 63);
        assert!(result.ends_with("_ck"));
    }

    #[test]
    fn test_unknown_db_type_token_falls_back_to_json_cascade() {
        // An unrecognized token must not change behavior: the JSON-type
        // cascade decides, exactly as before the CanonicalType refactor.
        let raw = json!({"type": "integer", "db_type": "banana"});
        assert_eq!(
            canonical_column_type(&raw, &raw, Dialect::Postgres),
            CanonicalType::Integer
        );
    }

    #[test]
    fn canonical_column_type_maps_json_schema_formats_to_logical_tokens() {
        let explicit_date = json!({
            "anyOf": [{"type": "string", "format": "date"}, {"type": "null"}],
            "db_type": "date"
        });
        assert_eq!(
            canonical_column_type(&explicit_date, &explicit_date, Dialect::Postgres),
            CanonicalType::Date
        );
        let derived_date = json!({"type": "string", "format": "date"});
        assert_eq!(
            canonical_column_type(&derived_date, &derived_date, Dialect::Postgres),
            CanonicalType::Date
        );
    }

    #[test]
    fn canonical_column_type_unknown_and_missing_type_fall_back_to_varchar() {
        let unknown = serde_json::json!({ "type": "mystery" });
        assert_eq!(
            canonical_column_type(&unknown, &unknown, Dialect::Postgres),
            CanonicalType::Varchar(None)
        );
        let no_type = serde_json::json!({});
        assert_eq!(
            canonical_column_type(&no_type, &no_type, Dialect::Sqlite),
            CanonicalType::Varchar(None)
        );
    }

}
