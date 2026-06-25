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

/// Canonical, backend-resolved column type shared by every Rust DDL path —
/// CREATE TABLE, ALTER TABLE ADD COLUMN, and schema diffing. The pair
/// `canonical_column_type` → `apply_canonical_type` is the single source of
/// truth for "what SQL type does this model field get"; any path that needs a
/// column type must go through it so emitters cannot drift. See AGENTS.md § I-1.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum CanonicalType {
    Integer,
    SmallInt,
    BigInt,
    Double,
    Decimal,
    Boolean,
    Json,
    Text,
    /// `varchar` with optional length (`.string()` / `.string_len(n)`).
    Varchar(Option<u32>),
    Char(u32),
    Uuid,
    /// SQLite rendering of the `timestamp`/`timestamptz` tokens (`.date_time()`).
    DateTime,
    Timestamp,
    TimestampTz,
    Date,
    Time,
    Blob,
}

pub(crate) fn apply_canonical_type(col_def: &mut ColumnDef, canonical: CanonicalType) {
    match canonical {
        CanonicalType::Integer => {
            col_def.integer();
        }
        CanonicalType::SmallInt => {
            col_def.small_integer();
        }
        CanonicalType::BigInt => {
            col_def.big_integer();
        }
        CanonicalType::Double => {
            col_def.double();
        }
        CanonicalType::Decimal => {
            col_def.decimal();
        }
        CanonicalType::Boolean => {
            col_def.boolean();
        }
        CanonicalType::Json => {
            col_def.json();
        }
        CanonicalType::Text => {
            col_def.text();
        }
        CanonicalType::Varchar(None) => {
            col_def.string();
        }
        CanonicalType::Varchar(Some(n)) => {
            col_def.string_len(n);
        }
        CanonicalType::Char(n) => {
            col_def.char_len(n);
        }
        CanonicalType::Uuid => {
            col_def.uuid();
        }
        CanonicalType::DateTime => {
            col_def.date_time();
        }
        CanonicalType::Timestamp => {
            col_def.timestamp();
        }
        CanonicalType::TimestampTz => {
            col_def.timestamp_with_time_zone();
        }
        CanonicalType::Date => {
            col_def.date();
        }
        CanonicalType::Time => {
            col_def.time();
        }
        CanonicalType::Blob => {
            col_def.blob();
        }
    }
}

fn json_type_to_canonical(json_type: &str, backend: SqlDialect) -> CanonicalType {
    match json_type {
        "integer" => CanonicalType::Integer,
        "string" => CanonicalType::Varchar(None),
        "number" => CanonicalType::Double,
        "boolean" => match backend {
            // SQLite stores booleans as integers.
            SqlDialect::Sqlite => CanonicalType::Integer,
            SqlDialect::Postgres => CanonicalType::Boolean,
        },
        "object" | "array" => CanonicalType::Json,
        _ => CanonicalType::Varchar(None),
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

/// Check-constraint name for a single-column `db_check`; matches the Python
/// Alembic helper `_ck_constraint_name` in `src/ferro/migrations/alembic.py`.
/// See AGENTS.md § I-1 (cross-emitter DDL parity).
fn db_check_constraint_name(table_lower: &str, col_name: &str) -> String {
    let raw = format!("ck_{}_{}", table_lower, col_name);
    if raw.chars().count() > 63 {
        return format!("{}_ck", raw.chars().take(60).collect::<String>());
    }
    raw
}

fn parse_varchar_token(token: &str) -> Option<u32> {
    let body = token.strip_prefix("varchar(")?.strip_suffix(')')?;
    let n: u32 = body.parse().ok()?;
    if n == 0 { None } else { Some(n) }
}

/// Map a canonical `db_type` token to a [`CanonicalType`]. Returns `None` when
/// the token is unrecognized (the caller falls back to the JSON-type cascade).
/// Strict per-class-definition validation runs in
/// `metaclass._validate_db_type_options`.
///
/// Duplicated on the Python side in `src/ferro/migrations/alembic.py
/// ::_db_type_to_sa_type`. Add new tokens to both emitters in the same change
/// and update the parity test. See AGENTS.md § I-1.
fn db_type_token_to_canonical(token: &str, backend: SqlDialect) -> Option<CanonicalType> {
    // Per-dialect resolution is chosen to byte-match SA's compilation in the
    // Alembic bridge. SQLite emits the typed keyword (BIGINT, SMALLINT,
    // CHAR(32), DATETIME) and lets SQLite type affinity normalize at
    // runtime; the parity test (U5) pins both sides token-for-token.
    match token {
        "text" => Some(CanonicalType::Text),
        "smallint" => Some(CanonicalType::SmallInt),
        "int" => Some(CanonicalType::Integer),
        "bigint" => Some(CanonicalType::BigInt),
        "uuid" => Some(match backend {
            SqlDialect::Sqlite => CanonicalType::Char(32),
            SqlDialect::Postgres => CanonicalType::Uuid,
        }),
        "timestamp" => Some(match backend {
            SqlDialect::Sqlite => CanonicalType::DateTime,
            SqlDialect::Postgres => CanonicalType::Timestamp,
        }),
        "timestamptz" => Some(match backend {
            SqlDialect::Sqlite => CanonicalType::DateTime,
            SqlDialect::Postgres => CanonicalType::TimestampTz,
        }),
        "date" => Some(CanonicalType::Date),
        "time" => Some(CanonicalType::Time),
        other => parse_varchar_token(other).map(|n| CanonicalType::Varchar(Some(n))),
    }
}

/// Resolve a model property to its backend-specific [`CanonicalType`].
///
/// `db_type` is the canonical user-facing storage knob: when present and
/// recognized it overrides every other column-type branch. Otherwise the
/// Pydantic JSON type/format cascade decides.
pub(crate) fn canonical_column_type(
    raw_col_info: &serde_json::Value,
    resolved_col_info: &serde_json::Value,
    backend: SqlDialect,
) -> CanonicalType {
    let db_type_token = raw_col_info
        .get("db_type")
        .or_else(|| resolved_col_info.get("db_type"))
        .and_then(|v| v.as_str());
    if let Some(token) = db_type_token
        && let Some(canonical) = db_type_token_to_canonical(token, backend)
    {
        return canonical;
    }

    let (json_type, format) = property_json_type_and_format(resolved_col_info);
    match (json_type, format) {
        (Some("string"), Some("date-time")) => CanonicalType::TimestampTz,
        (Some("string"), Some("date")) => CanonicalType::Date,
        (Some("string"), Some("uuid")) => CanonicalType::Uuid,
        (Some(_), Some("decimal")) => CanonicalType::Decimal,
        (Some("string"), Some("binary")) => CanonicalType::Blob,
        (Some(t), _) => json_type_to_canonical(t, backend),
        (None, _) => CanonicalType::Varchar(None),
    }
}

fn render_check_values(col_info: &serde_json::Value) -> Option<String> {
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
    Some(rendered.join(", "))
}

fn build_check_constraint_sql(
    table_lower: &str,
    col_name: &str,
    col_info: &serde_json::Value,
    backend: SqlDialect,
) -> Option<String> {
    // SQLite cannot ALTER TABLE ADD CONSTRAINT and adding a named CHECK
    // requires CREATE TABLE rebuild. db_check is a Postgres-first feature in
    // Phase 1; SQLite users opting in just see the constraint elided at
    // runtime. The parity test (U5) compares Postgres-side rendering.
    if backend != SqlDialect::Postgres {
        return None;
    }
    let values = render_check_values(col_info)?;
    let ck_name = db_check_constraint_name(table_lower, col_name);
    Some(format!(
        "ALTER TABLE \"{table}\" ADD CONSTRAINT \"{name}\" CHECK (\"{col}\" IN ({values}))",
        table = table_lower,
        name = ck_name,
        col = col_name,
        values = values,
    ))
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
    /// CREATE-ready column definition: type, then PK/auto-increment, then
    /// NOT NULL, then UNIQUE — applied in the exact order the CREATE TABLE
    /// emitter has always used (spec order is part of the rendered SQL).
    pub col_def: ColumnDef,
    pub canonical: CanonicalType,
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
    backend: SqlDialect,
) -> ColumnPlan {
    let col_info = resolve_ref(schema, raw_col_info);
    let canonical = canonical_column_type(raw_col_info, col_info, backend);

    let mut col_def = ColumnDef::new(Alias::new(col_name));
    apply_canonical_type(&mut col_def, canonical);

    let is_primary_key =
        column_bool_metadata(raw_col_info, col_info, "primary_key").unwrap_or(false);
    let is_auto = column_bool_metadata(raw_col_info, col_info, "autoincrement").unwrap_or(true);
    if is_primary_key {
        col_def.primary_key();
        if is_auto {
            col_def.auto_increment();
        }
    }

    let is_nullable = column_bool_metadata(raw_col_info, col_info, "ferro_nullable") != Some(false);
    if !is_nullable {
        col_def.not_null();
    }

    let is_unique = column_bool_metadata(raw_col_info, col_info, "unique").unwrap_or(false);
    if is_unique {
        col_def.unique_key();
    }

    let mut index_sqls = Vec::new();
    if column_bool_metadata(raw_col_info, col_info, "index").unwrap_or(false) {
        let index_name = format!("idx_{}_{}", table_lower, col_name);
        let index_stmt = Index::create()
            .name(&index_name)
            .table(Alias::new(table_lower))
            .col(Alias::new(col_name))
            .if_not_exists()
            .to_owned();
        let index_sql = match backend {
            SqlDialect::Sqlite => index_stmt.to_string(SqliteQueryBuilder),
            SqlDialect::Postgres => index_stmt.to_string(PostgresQueryBuilder),
        };
        index_sqls.push(index_sql);
    }

    // db_check=True -> single-column CHECK constraint named ck_<table>_<col>.
    // Emitted as a post-create ALTER TABLE so the name flows through
    // identically on both backends. SQLite cannot execute ADD CONSTRAINT;
    // users opting in to db_check are expected to be on Postgres for Phase 1.
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
        col_def,
        canonical,
        is_primary_key,
        is_nullable,
        is_unique,
        index_sqls,
        fk,
        literal_default,
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
            let mut plan = build_column_plan(&table_lower, col_name, raw_col_info, schema, backend);

            table_stmt.col(&mut plan.col_def);
            index_sqls.append(&mut plan.index_sqls);

            if let Some(fk) = &plan.fk {
                let mut fk_stmt = ForeignKey::create();
                fk_stmt
                    .from(Alias::new(&table_lower), Alias::new(col_name))
                    .to(Alias::new(&fk.to_table), Alias::new("id")) // CX Choice: Assume target PK is 'id' for now
                    .on_delete(fk.on_delete);

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

fn shadow_compare_create_table_sqls(
    name: &str,
    schema: &serde_json::Value,
    backend: SqlDialect,
) -> Result<(), String> {
    let legacy = build_create_table_sqls(name, schema, backend);
    let schema_roundtrip: serde_json::Value =
        serde_json::from_str(&serde_json::to_string(schema).map_err(|e| e.to_string())?)
            .map_err(|e| e.to_string())?;
    let shadow = build_create_table_sqls(name, &schema_roundtrip, backend);
    if legacy == shadow {
        return Ok(());
    }
    Err(format!(
        "shadow create-table mismatch for '{}': legacy={} shadow={}",
        name,
        serde_json::to_string(&legacy).unwrap_or_else(|_| "<legacy>".to_string()),
        serde_json::to_string(&shadow).unwrap_or_else(|_| "<shadow>".to_string()),
    ))
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
        if engine.is_shadow_runtime_enabled()
            && let Err(diff) = shadow_compare_create_table_sqls(&name, &schema, backend)
        {
            crate::log_debug(format!("⚠️ Ferro shadow runtime mismatch: {diff}"));
            if std::env::var("FERRO_SHADOW_RUNTIME_STRICT")
                .map(|value| value == "1" || value.eq_ignore_ascii_case("true"))
                .unwrap_or(false)
            {
                return Err(pyo3::exceptions::PyRuntimeError::new_err(diff));
            }
        }

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

/// Test-only helper: render the Rust emitter's CREATE TABLE SQL plus any
/// post-create SQL fragments (CHECK constraints, composite indexes) without
/// requiring a live database. Used by the cross-emitter parity test (U5 of
/// the configurable-column-storage plan) to assert that the Rust and Alembic
/// emitters agree on every `(canonical_token, dialect)` pair.
///
/// `dialect` must be `"postgres"` or `"sqlite"`. Anything else raises
/// `ValueError`. `schema_json` is a JSON string in the same shape that
/// `register_model_schema` consumes.
///
/// # Errors
/// Returns a `PyErr` when the JSON cannot be parsed or the dialect is
/// unrecognized.
#[pyfunction]
#[pyo3(name = "_render_create_table_sql_for_test")]
pub fn _render_create_table_sql_for_test(
    name: String,
    schema_json: String,
    dialect: String,
) -> PyResult<(String, Vec<String>)> {
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
    let schema: serde_json::Value = serde_json::from_str(&schema_json).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("Invalid JSON schema: {}", e))
    })?;
    Ok(build_create_table_sqls(&name, &schema, backend))
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

    // ------------------------------------------------------------------
    // U4: db_type / db_check dispatch
    // ------------------------------------------------------------------

    fn col_sql(token: &str, json_type: &str, backend: SqlDialect) -> String {
        let schema = json!({
            "properties": {
                "id": {"type": "integer", "primary_key": true, "autoincrement": true},
                "x": {"type": json_type, "db_type": token, "ferro_nullable": false},
            }
        });
        let (sql, _) = build_create_table_sqls("t", &schema, backend);
        sql.to_uppercase()
    }

    #[test]
    fn test_db_type_text_emits_text_column() {
        for backend in [SqlDialect::Sqlite, SqlDialect::Postgres] {
            let sql = col_sql("text", "string", backend);
            assert!(
                sql.contains("TEXT"),
                "missing TEXT for {:?}: {}",
                backend,
                sql
            );
        }
    }

    #[test]
    fn test_db_type_bigint_postgres_emits_bigint() {
        let sql = col_sql("bigint", "integer", SqlDialect::Postgres);
        assert!(sql.contains("BIGINT"), "missing BIGINT: {}", sql);
    }

    #[test]
    fn test_db_type_bigint_sqlite_emits_bigint_keyword() {
        // SQLite still gets the BIGINT keyword; type affinity normalizes at
        // runtime. Parity with the Alembic bridge (SA emits the same keyword).
        let sql = col_sql("bigint", "integer", SqlDialect::Sqlite);
        assert!(sql.contains("BIGINT"), "missing BIGINT keyword: {}", sql);
    }

    #[test]
    fn test_db_type_smallint_postgres_emits_smallint() {
        let sql = col_sql("smallint", "integer", SqlDialect::Postgres);
        assert!(sql.contains("SMALLINT"), "missing SMALLINT: {}", sql);
    }

    #[test]
    fn test_db_type_smallint_sqlite_emits_smallint_keyword() {
        let sql = col_sql("smallint", "integer", SqlDialect::Sqlite);
        assert!(
            sql.contains("SMALLINT"),
            "missing SMALLINT keyword: {}",
            sql
        );
    }

    #[test]
    fn test_db_type_timestamptz_emits_with_time_zone() {
        let sql = col_sql("timestamptz", "string", SqlDialect::Postgres);
        assert!(
            sql.contains("TIMESTAMP WITH TIME ZONE") || sql.contains("TIMESTAMPTZ"),
            "missing tz timestamp: {}",
            sql
        );
    }

    #[test]
    fn test_db_type_timestamp_emits_plain_timestamp() {
        let sql = col_sql("timestamp", "string", SqlDialect::Postgres);
        assert!(sql.contains("TIMESTAMP"), "missing TIMESTAMP: {}", sql);
        assert!(
            !sql.contains("WITH TIME ZONE") && !sql.contains("TIMESTAMPTZ"),
            "leaked tz: {}",
            sql
        );
    }

    #[test]
    fn test_db_type_varchar_n_emits_varchar_with_length() {
        let sql = col_sql("varchar(255)", "string", SqlDialect::Postgres);
        // sea-query Postgres builder renders VARCHAR(N) for string_len(N).
        assert!(
            sql.contains("VARCHAR(255)") || sql.contains("CHARACTER VARYING(255)"),
            "missing varchar(255): {}",
            sql
        );
    }

    #[test]
    fn test_db_type_overrides_json_format_branch() {
        // string + format=date-time would normally emit timestamptz; with
        // db_type='text' it must render TEXT instead.
        let schema = json!({
            "properties": {
                "id": {"type": "integer", "primary_key": true, "autoincrement": true},
                "x": {"type": "string", "format": "date-time", "db_type": "text"},
            }
        });
        let (sql, _) = build_create_table_sqls("t", &schema, SqlDialect::Postgres);
        let upper = sql.to_uppercase();
        assert!(upper.contains("TEXT"), "missing TEXT override: {}", sql);
        assert!(
            !upper.contains("TIMESTAMP"),
            "default cascade leaked through: {}",
            sql
        );
    }

    #[test]
    fn test_db_check_emits_named_constraint() {
        let schema = json!({
            "properties": {
                "id": {"type": "integer", "primary_key": true, "autoincrement": true},
                "format": {
                    "type": "string",
                    "db_type": "text",
                    "db_check": true,
                    "enum": ["pdf", "json"]
                }
            }
        });
        let (_table_sql, post_sqls) = build_create_table_sqls("doc", &schema, SqlDialect::Postgres);
        let joined = post_sqls.join("\n");
        assert!(
            joined.contains("ck_doc_format"),
            "missing constraint name: {}",
            joined
        );
        assert!(
            joined.to_uppercase().contains("CHECK"),
            "missing CHECK: {}",
            joined
        );
        assert!(
            joined.contains("'pdf'") && joined.contains("'json'"),
            "missing values: {}",
            joined
        );
    }

    #[test]
    fn test_db_check_with_int_values_unquoted() {
        let schema = json!({
            "properties": {
                "id": {"type": "integer", "primary_key": true, "autoincrement": true},
                "priority": {
                    "type": "integer",
                    "db_type": "smallint",
                    "db_check": true,
                    "enum": [1, 2]
                }
            }
        });
        let (_table_sql, post_sqls) =
            build_create_table_sqls("task", &schema, SqlDialect::Postgres);
        let joined = post_sqls.join("\n");
        assert!(
            joined.contains("(1, 2)"),
            "ints should be unquoted: {}",
            joined
        );
        assert!(
            !joined.contains("'1'"),
            "ints should not be quoted: {}",
            joined
        );
    }

    #[test]
    fn test_db_check_off_emits_no_constraint() {
        let schema = json!({
            "properties": {
                "id": {"type": "integer", "primary_key": true, "autoincrement": true},
                "format": {"type": "string", "db_type": "text", "enum": ["pdf"]}
            }
        });
        let (_table_sql, post_sqls) = build_create_table_sqls("doc", &schema, SqlDialect::Postgres);
        assert!(
            post_sqls.iter().all(|s| !s.contains("CHECK")),
            "unexpected CHECK: {:?}",
            post_sqls
        );
    }

    #[test]
    fn test_db_check_skipped_on_sqlite() {
        // SQLite cannot ALTER TABLE ADD CONSTRAINT; db_check is Postgres-only
        // in Phase 1 and the emitter elides it on SQLite rather than emitting
        // SQL that would fail at execution time.
        let schema = json!({
            "properties": {
                "id": {"type": "integer", "primary_key": true, "autoincrement": true},
                "format": {
                    "type": "string",
                    "db_type": "text",
                    "db_check": true,
                    "enum": ["pdf", "json"]
                }
            }
        });
        let (_table_sql, post_sqls) = build_create_table_sqls("doc", &schema, SqlDialect::Sqlite);
        assert!(
            post_sqls
                .iter()
                .all(|s| !s.to_uppercase().contains("CONSTRAINT")),
            "SQLite must not emit ADD CONSTRAINT: {:?}",
            post_sqls
        );
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
    fn test_db_type_unknown_token_maps_to_none() {
        assert_eq!(
            db_type_token_to_canonical("banana", SqlDialect::Postgres),
            None
        );
    }

    #[test]
    fn test_unknown_db_type_token_falls_back_to_json_cascade() {
        // An unrecognized token must not change behavior: the JSON-type
        // cascade decides, exactly as before the CanonicalType refactor.
        let raw = json!({"type": "integer", "db_type": "banana"});
        assert_eq!(
            canonical_column_type(&raw, &raw, SqlDialect::Postgres),
            CanonicalType::Integer
        );
    }

    #[test]
    fn test_parse_varchar_token_rejects_zero_and_garbage() {
        assert_eq!(parse_varchar_token("varchar(0)"), None);
        assert_eq!(parse_varchar_token("varchar(abc)"), None);
        assert_eq!(parse_varchar_token("varchar()"), None);
        assert_eq!(parse_varchar_token("varchar(10)"), Some(10));
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
