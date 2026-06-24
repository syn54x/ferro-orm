//! Auto-migrate schema diffing and execution.
//!
//! Extends `connect(auto_migrate=True)` beyond table creation: with
//! `migrate_updates`, missing model columns are added to existing tables
//! (plus, on Postgres, type/nullability reconciliation); with
//! `migrate_destructive`, live columns that no longer exist on the model are
//! dropped. Capability matrix and semantics are documented on the Python
//! `ferro.connect` / `ferro.migrate` APIs.
//!
//! Runtime migration plans are produced by `ferro-migrate` from SchemaIR so
//! auto-migrate shares the same planner core as other DDL paths (AGENTS.md § I-1).

use crate::backend::EngineHandle;
use crate::introspect::{
    LiveColumn, live_table_columns, quote_ident, sqlite_indexes_covering_column,
};
use crate::schema::{internal_create_tables, order_schemas_for_creation};
use crate::state::{IDENTITY_MAP, MODEL_REGISTRY, SqlDialect, engine_for_connection};
use ferro_migrate::{BackendDialect, MigrationOp, emit_sql, plan_from_ir};
use ferro_schema_ir::{
    IrEnvelope, SchemaCheck, SchemaColumn, SchemaForeignKey, SchemaIndex, SchemaIrPayload,
    SchemaModel, SchemaUnique,
};
use pyo3::prelude::*;
use std::collections::HashMap;
use std::sync::Arc;

fn schema_json_to_schema_ir(
    table_lower: &str,
    schema: &serde_json::Value,
) -> IrEnvelope<SchemaIrPayload> {
    fn infer_db_type(resolved: &serde_json::Value) -> String {
        let logical = resolved.get("type").and_then(|v| v.as_str());
        let format = resolved.get("format").and_then(|v| v.as_str());
        match (logical, format) {
            (Some("integer"), _) => "int".to_string(),
            (Some("number"), Some("decimal")) => "double".to_string(),
            (Some("number"), _) => "double".to_string(),
            (Some("boolean"), _) => "int".to_string(),
            (Some("string"), Some("uuid")) => "uuid".to_string(),
            (Some("string"), Some("date-time")) => "timestamptz".to_string(),
            (Some("string"), Some("date")) => "date".to_string(),
            (Some("string"), Some("time")) => "time".to_string(),
            (Some("string"), _) => "varchar(255)".to_string(),
            (Some("array"), _) | (Some("object"), _) => "text".to_string(),
            _ => "text".to_string(),
        }
    }

    let mut columns = Vec::new();
    let mut foreign_keys = Vec::new();
    let mut checks = Vec::new();
    if let Some(properties) = schema.get("properties").and_then(|p| p.as_object()) {
        for (name, raw_col) in properties {
            let resolved = if let Some(ref_path) = raw_col.get("$ref").and_then(|r| r.as_str()) {
                if let Some(def_name) = ref_path.strip_prefix("#/$defs/") {
                    schema
                        .get("$defs")
                        .and_then(|defs| defs.get(def_name))
                        .unwrap_or(raw_col)
                } else {
                    raw_col
                }
            } else {
                raw_col
            };
            let nullable = raw_col
                .get("ferro_nullable")
                .or_else(|| resolved.get("ferro_nullable"))
                .and_then(|v| v.as_bool())
                .unwrap_or(true);
            let db_type = raw_col
                .get("db_type")
                .or_else(|| resolved.get("db_type"))
                .and_then(|v| v.as_str())
                .map(str::to_string)
                .unwrap_or_else(|| infer_db_type(resolved));
            columns.push(SchemaColumn {
                name: name.clone(),
                logical_type: resolved
                    .get("type")
                    .and_then(|v| v.as_str())
                    .unwrap_or("unknown")
                    .to_string(),
                db_type,
                db_type_explicit: raw_col
                    .get("db_type")
                    .or_else(|| resolved.get("db_type"))
                    .and_then(|v| v.as_str())
                    .map(|_| true),
                nullable,
                primary_key: resolved
                    .get("primary_key")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false),
                autoincrement: resolved
                    .get("autoincrement")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false),
                unique: resolved
                    .get("unique")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false),
                index: resolved
                    .get("index")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false),
                default: resolved.get("default").cloned(),
                format: resolved
                    .get("format")
                    .and_then(|v| v.as_str())
                    .map(str::to_string),
                enum_values: resolved.get("enum").and_then(|v| v.as_array()).cloned(),
                enum_type_name: resolved
                    .get("enum_type_name")
                    .and_then(|v| v.as_str())
                    .map(str::to_string),
            });
            if let Some(fk) = raw_col
                .get("foreign_key")
                .or_else(|| resolved.get("foreign_key"))
                .and_then(|v| v.as_object())
            {
                let to_table = fk
                    .get("to_table")
                    .and_then(|v| v.as_str())
                    .unwrap_or_default()
                    .to_string();
                if !to_table.is_empty() {
                    foreign_keys.push(SchemaForeignKey {
                        column: name.clone(),
                        to_table,
                        to_column: "id".to_string(),
                        on_delete: fk
                            .get("on_delete")
                            .and_then(|v| v.as_str())
                            .map(str::to_string),
                        name: None,
                    });
                }
            }
            let db_check = raw_col
                .get("db_check")
                .or_else(|| resolved.get("db_check"))
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            if db_check
                && let Some(values) = resolved.get("enum").and_then(|v| v.as_array())
                && !values.is_empty()
            {
                let rendered: Vec<String> = values
                    .iter()
                    .map(|v| match v {
                        serde_json::Value::String(s) => format!("'{}'", s.replace('\'', "''")),
                        serde_json::Value::Number(n) => n.to_string(),
                        serde_json::Value::Bool(b) => b.to_string(),
                        other => format!("'{}'", other.to_string().replace('\'', "''")),
                    })
                    .collect();
                checks.push(SchemaCheck {
                    name: format!("ck_{}_{}", table_lower, name),
                    expression: format!("\"{}\" IN ({})", name, rendered.join(", ")),
                });
            }
        }
    }
    columns.sort_by(|a, b| a.name.cmp(&b.name));

    IrEnvelope {
        ir_kind: "schema".to_string(),
        ir_version: 1,
        payload: SchemaIrPayload {
            dialect_agnostic: true,
            models: vec![SchemaModel {
                model_name: table_lower.to_string(),
                table_name: table_lower.to_string(),
                columns,
                foreign_keys,
                indexes: Vec::<SchemaIndex>::new(),
                uniques: Vec::<SchemaUnique>::new(),
                checks,
            }],
        },
    }
}

fn declared_type_to_db_type(col: &LiveColumn) -> String {
    let lower = col.declared_type.to_ascii_lowercase();
    if lower.contains("character varying") || lower.contains("varchar") {
        if let Some(n) = col.char_max_len {
            return format!("varchar({n})");
        }
        return "varchar(255)".to_string();
    }
    if lower.contains("smallint") {
        return "smallint".to_string();
    }
    if lower.contains("bigint") {
        return "bigint".to_string();
    }
    if lower.contains("int") {
        return "int".to_string();
    }
    if lower.contains("uuid") || lower.contains("char(32)") {
        return "uuid".to_string();
    }
    if lower.contains("timestamp with time zone") {
        return "timestamptz".to_string();
    }
    if lower.contains("timestamp") || lower.contains("datetime") {
        return "timestamp".to_string();
    }
    if lower == "date" || lower.contains("date_") {
        return "date".to_string();
    }
    if lower == "time" || lower.contains("time_") {
        return "time".to_string();
    }
    "text".to_string()
}

fn live_columns_to_schema_ir(
    table_lower: &str,
    live: &[LiveColumn],
) -> IrEnvelope<SchemaIrPayload> {
    let mut columns: Vec<SchemaColumn> = live
        .iter()
        .map(|col| SchemaColumn {
            name: col.name.clone(),
            logical_type: if col.is_enum_udt {
                "enum_udt".to_string()
            } else {
                "unknown".to_string()
            },
            db_type: declared_type_to_db_type(col),
            db_type_explicit: None,
            nullable: col.is_nullable,
            primary_key: col.is_primary_key,
            autoincrement: false,
            unique: false,
            index: false,
            default: None,
            format: None,
            enum_values: None,
            enum_type_name: None,
        })
        .collect();
    columns.sort_by(|a, b| a.name.cmp(&b.name));
    IrEnvelope {
        ir_kind: "schema".to_string(),
        ir_version: 1,
        payload: SchemaIrPayload {
            dialect_agnostic: true,
            models: vec![SchemaModel {
                model_name: table_lower.to_string(),
                table_name: table_lower.to_string(),
                columns,
                foreign_keys: Vec::<SchemaForeignKey>::new(),
                indexes: Vec::<SchemaIndex>::new(),
                uniques: Vec::<SchemaUnique>::new(),
                checks: Vec::<SchemaCheck>::new(),
            }],
        },
    }
}

/// Which migration behaviors beyond table creation are enabled.
#[derive(Clone, Copy, Debug, Default)]
pub struct MigrateOptions {
    /// Add missing model columns to existing tables; on Postgres, also
    /// reconcile column type and nullability drift.
    pub updates: bool,
    /// Drop live columns that no longer exist on the model. Implies `updates`.
    pub destructive: bool,
}

impl MigrateOptions {
    /// Apply the flag ladder: `destructive` ⇒ `updates`.
    pub fn laddered(updates: bool, destructive: bool) -> Self {
        Self {
            updates: updates || destructive,
            destructive,
        }
    }
}

/// The DDL and diagnostics produced by diffing one table.
#[derive(Debug)]
pub struct MigrationPlan {
    /// Ready-to-execute DDL statements, in order.
    pub statements: Vec<String>,
    /// Columns to drop (destructive mode). Kept separate from `statements`
    /// because the executor must resolve live index dependencies first.
    pub drop_columns: Vec<String>,
    /// Human-readable notes emitted as Python `UserWarning`s.
    pub warnings: Vec<String>,
}

impl MigrationPlan {
    fn new() -> Self {
        Self {
            statements: Vec::new(),
            drop_columns: Vec::new(),
            warnings: Vec::new(),
        }
    }

    fn is_empty(&self) -> bool {
        self.statements.is_empty() && self.drop_columns.is_empty()
    }
}

/// Storage-semantics class of a declared SQLite type. SQLite is dynamically
/// typed: many declared-type spellings are storage-equivalent (its type
/// affinity rules), so drift warnings fire only when the *class* changes —
/// e.g. `integer` vs `varchar` — not for cosmetic spelling differences like
/// `DATETIME` (Alembic) vs `datetime_text` (sea-query).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum SqliteTypeClass {
    Integer,
    Text,
    Blob,
    Real,
    Numeric,
    Temporal,
}

fn sqlite_type_class(declared: &str) -> SqliteTypeClass {
    let declared = declared.to_ascii_lowercase();
    // ISO-text temporal spellings from both emitters (DATE, DATETIME,
    // TIMESTAMP, date_text, timestamp_with_timezone_text, ...).
    if declared.contains("date") || declared.contains("time") {
        return SqliteTypeClass::Temporal;
    }
    if declared.contains("json") {
        return SqliteTypeClass::Text;
    }
    if declared.contains("bool") || declared.contains("int") {
        return SqliteTypeClass::Integer;
    }
    if declared.contains("char") || declared.contains("clob") || declared.contains("text") {
        return SqliteTypeClass::Text;
    }
    if declared.is_empty() || declared.contains("blob") {
        return SqliteTypeClass::Blob;
    }
    if declared.contains("real")
        || declared.contains("floa")
        || declared.contains("doub")
        || declared.contains("num")
        || declared.contains("dec")
    {
        return SqliteTypeClass::Real;
    }
    SqliteTypeClass::Numeric
}

/// Single-column unique index name with the 63-char Postgres-identifier
/// guard; matches the composite `uq_` convention (AGENTS.md § I-1).

/// Diff one registered model schema against its live table and produce the
/// DDL plan. Pure with respect to the database — callers introspect first.
///
/// # Errors
/// Returns a `PyErr` for changes that cannot be applied safely: adding a
/// primary-key column, or adding a NOT NULL column without a usable literal
/// default. These abort the migration ("fail loudly").
pub fn plan_table_migration(
    table_lower: &str,
    schema: &serde_json::Value,
    live: &[LiveColumn],
    backend: SqlDialect,
    opts: MigrateOptions,
) -> PyResult<MigrationPlan> {
    let mut plan = MigrationPlan::new();
    if !opts.updates {
        return Ok(plan);
    }

    let old_ir = live_columns_to_schema_ir(table_lower, live);
    let new_ir = schema_json_to_schema_ir(table_lower, schema);
    let mut typed_plan = plan_from_ir(&old_ir, &new_ir);
    if !opts.destructive {
        typed_plan.operations.retain(|op| {
            !matches!(
                op,
                MigrationOp::DropColumn { .. } | MigrationOp::DropTable { .. }
            )
        });
    }

    let emitted = emit_sql(
        &typed_plan,
        match backend {
            SqlDialect::Sqlite => BackendDialect::Sqlite,
            SqlDialect::Postgres => BackendDialect::Postgres,
        },
    )
    .map_err(pyo3::exceptions::PyValueError::new_err)?;

    let live_by_name: HashMap<&str, &LiveColumn> =
        live.iter().map(|col| (col.name.as_str(), col)).collect();
    for encoded in emitted.drop_columns {
        let Some((drop_table, drop_col)) = encoded.split_once("::") else {
            continue;
        };
        if drop_table != table_lower {
            continue;
        }
        if let Some(live_col) = live_by_name.get(drop_col)
            && live_col.is_primary_key
        {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "Cannot drop column '{}.{}': it is part of the primary key. Primary-key changes must be migrated with Alembic.",
                table_lower, drop_col
            )));
        }
        plan.drop_columns.push(drop_col.to_string());
    }
    plan.statements = emitted.statements;
    plan.warnings = emitted.warnings;
    Ok(plan)
}

fn shadow_compare_migration_plan(
    table_lower: &str,
    schema: &serde_json::Value,
    live: &[LiveColumn],
    backend: SqlDialect,
    opts: MigrateOptions,
) -> Result<(), String> {
    let legacy = plan_table_migration(table_lower, schema, live, backend, opts)
        .map_err(|e| e.to_string())?;
    let schema_roundtrip: serde_json::Value =
        serde_json::from_str(&serde_json::to_string(schema).map_err(|e| e.to_string())?)
            .map_err(|e| e.to_string())?;
    let live_roundtrip = live.to_vec();
    let shadow = plan_table_migration(
        table_lower,
        &schema_roundtrip,
        &live_roundtrip,
        backend,
        opts,
    )
    .map_err(|e| e.to_string())?;
    if legacy.statements == shadow.statements
        && legacy.drop_columns == shadow.drop_columns
        && legacy.warnings == shadow.warnings
    {
        return Ok(());
    }
    Err(format!(
        "shadow migration-plan mismatch for '{}': legacy={} shadow={}",
        table_lower,
        serde_json::to_string(&legacy.statements).unwrap_or_else(|_| "<legacy>".to_string()),
        serde_json::to_string(&shadow.statements).unwrap_or_else(|_| "<shadow>".to_string())
    ))
}

/// Drop one column, resolving SQLite index dependencies first.
///
/// Explicit indexes covering the column are orphaned by its removal and are
/// dropped beforehand (SQLite refuses `DROP COLUMN` on an indexed column).
/// Constraint autoindexes cannot be dropped separately, so their presence is
/// a hard error, as is any remaining engine refusal (CHECK references,
/// triggers, views, inbound foreign keys).
async fn execute_drop_column(
    engine: &EngineHandle,
    table_lower: &str,
    col_name: &str,
    backend: SqlDialect,
) -> PyResult<()> {
    if backend == SqlDialect::Sqlite {
        let indexes = sqlite_indexes_covering_column(engine, table_lower, col_name).await?;
        if let Some(blocking) = indexes.iter().find(|index| index.origin != "c") {
            let constraint = match blocking.origin.as_str() {
                "u" => "a UNIQUE constraint",
                "pk" => "the PRIMARY KEY",
                _ => "a table constraint",
            };
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "Cannot drop column '{}.{}': it is enforced by {} ('{}'), which SQLite \
                 cannot drop separately from the table definition. Use Alembic for this \
                 migration.",
                table_lower, col_name, constraint, blocking.name
            )));
        }
        for index in &indexes {
            let sql = format!("DROP INDEX IF EXISTS {}", quote_ident(&index.name));
            engine.execute_sql_unprepared(&sql).await.map_err(|e| {
                pyo3::exceptions::PyRuntimeError::new_err(format!(
                    "Auto-migrate failed dropping index '{}' (required to drop column \
                     '{}.{}'): {}",
                    index.name, table_lower, col_name, e
                ))
            })?;
        }
    }

    let sql = format!(
        "ALTER TABLE {} DROP COLUMN {}",
        quote_ident(table_lower),
        quote_ident(col_name)
    );
    engine.execute_sql_unprepared(&sql).await.map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!(
            "Cannot drop column '{}.{}': {}. Columns referenced by constraints, foreign \
             keys, triggers, or views must be migrated with Alembic.",
            table_lower, col_name, e
        ))
    })?;
    Ok(())
}

/// Run the full auto-migrate pass: create missing tables, then (per
/// `MigrateOptions`) reconcile existing tables with the registered models.
///
/// After any ALTER/DROP executed, the engine pool is refreshed so no
/// connection can serve a statement prepared against the pre-DDL schema.
///
/// # Errors
/// Returns a `PyErr` if introspection, DDL execution, or the pool refresh
/// fails, or if the diff contains a change that cannot be applied safely.
pub async fn internal_migrate(engine: Arc<EngineHandle>, opts: MigrateOptions) -> PyResult<()> {
    internal_create_tables(engine.clone()).await?;
    if !opts.updates {
        return Ok(());
    }

    let schemas = {
        let registry = MODEL_REGISTRY.read().map_err(|_| {
            pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Model Registry")
        })?;
        registry.clone()
    };
    let backend = engine.backend();

    let mut warnings = Vec::new();
    let mut ddl_ran = false;

    for (name, schema) in order_schemas_for_creation(schemas) {
        let table_lower = name.to_lowercase();
        let Some(live) = live_table_columns(&engine, &table_lower).await? else {
            // Freshly created (or otherwise absent) tables have nothing to diff.
            continue;
        };

        let mut plan = plan_table_migration(&table_lower, &schema, &live, backend, opts)?;
        if engine.is_shadow_runtime_enabled()
            && let Err(diff) =
                shadow_compare_migration_plan(&table_lower, &schema, &live, backend, opts)
        {
            crate::log_debug(format!("⚠️ Ferro shadow runtime mismatch: {diff}"));
            if std::env::var("FERRO_SHADOW_RUNTIME_STRICT")
                .map(|value| value == "1" || value.eq_ignore_ascii_case("true"))
                .unwrap_or(false)
            {
                return Err(pyo3::exceptions::PyRuntimeError::new_err(diff));
            }
        }
        if plan.is_empty() {
            warnings.append(&mut plan.warnings);
            continue;
        }

        for sql in &plan.statements {
            engine.execute_sql_unprepared(sql).await.map_err(|e| {
                pyo3::exceptions::PyRuntimeError::new_err(format!(
                    "Auto-migrate DDL failed for table '{}': {} (statement: {})",
                    table_lower, e, sql
                ))
            })?;
            ddl_ran = true;
        }
        for col_name in &plan.drop_columns {
            execute_drop_column(&engine, &table_lower, col_name, backend).await?;
            ddl_ran = true;
        }
        warnings.append(&mut plan.warnings);

        crate::log_debug(format!(
            "✅ Ferro Engine: Table '{}' migrated ({} statement(s), {} column(s) dropped)",
            table_lower,
            plan.statements.len(),
            plan.drop_columns.len()
        ));
    }

    if ddl_ran {
        engine.refresh_pool().await.map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!(
                "Auto-migrate applied DDL but failed to refresh the connection pool: {}",
                e
            ))
        })?;
        // Identity-mapped instances were hydrated against the pre-migration
        // schema (e.g. a row loaded before a column add lacks the new field).
        // The schema lives in the database, which any number of named
        // connections may share, so the whole map is invalidated — eviction is
        // always safe; instances simply re-hydrate on next load.
        IDENTITY_MAP.clear();
    }

    for warning in &warnings {
        crate::emit_user_warning(warning);
    }

    Ok(())
}

/// Manually run the auto-migrate pass against a connected engine.
///
/// Mirrors `connect(auto_migrate=True, migrate_updates=..., migrate_destructive=...)`
/// for consumers that want explicit control over when DDL runs. `updates`
/// defaults to true — calling `migrate()` and getting create-only behavior
/// would be surprising; use `create_tables()` for that.
///
/// # Errors
/// Returns a `PyErr` if the engine is not initialized or the migration fails.
#[pyfunction]
#[pyo3(signature = (using=None, updates=true, destructive=false))]
pub fn migrate(
    py: Python<'_>,
    using: Option<String>,
    updates: bool,
    destructive: bool,
) -> PyResult<Bound<'_, PyAny>> {
    let opts = MigrateOptions::laddered(updates, destructive);
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let engine = engine_for_connection(using)?;
        internal_migrate(engine, opts).await
    })
}

/// Test-only helper: run the migration diff for one table against a JSON
/// description of its live columns, without a database. Returns
/// `(statements, warnings)`; destructive drops are rendered as plain
/// `DROP COLUMN` statements (the dependency-aware index handling needs a live
/// database and is exercised by integration tests).
///
/// # Errors
/// Returns a `PyErr` when the JSON cannot be parsed, the dialect is
/// unrecognized, or the diff contains an unsafe change.
#[pyfunction]
#[pyo3(name = "_render_migration_sql_for_test")]
#[pyo3(signature = (name, schema_json, live_columns_json, dialect, updates=true, destructive=false))]
pub fn _render_migration_sql_for_test(
    name: String,
    schema_json: String,
    live_columns_json: String,
    dialect: String,
    updates: bool,
    destructive: bool,
) -> PyResult<(Vec<String>, Vec<String>)> {
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
    let live: Vec<LiveColumn> = serde_json::from_str(&live_columns_json).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("Invalid live-columns JSON: {}", e))
    })?;

    let table_lower = name.to_lowercase();
    let opts = MigrateOptions::laddered(updates, destructive);
    let plan = plan_table_migration(&table_lower, &schema, &live, backend, opts)?;

    let mut statements = plan.statements;
    for col_name in &plan.drop_columns {
        statements.push(format!(
            "ALTER TABLE {} DROP COLUMN {}",
            quote_ident(&table_lower),
            quote_ident(col_name)
        ));
    }
    Ok((statements, plan.warnings))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const UPDATES: MigrateOptions = MigrateOptions {
        updates: true,
        destructive: false,
    };
    const DESTRUCTIVE: MigrateOptions = MigrateOptions {
        updates: true,
        destructive: true,
    };

    fn live(name: &str, declared_type: &str) -> LiveColumn {
        LiveColumn {
            name: name.to_string(),
            declared_type: declared_type.to_string(),
            is_nullable: true,
            is_primary_key: false,
            char_max_len: None,
            is_enum_udt: false,
        }
    }

    fn invoice_schema() -> serde_json::Value {
        json!({
            "properties": {
                "id": {"type": "integer", "primary_key": true, "autoincrement": true},
                "number": {"type": "string", "ferro_nullable": false},
                "paid_date": {"type": "string", "db_type": "date", "ferro_nullable": true},
            }
        })
    }

    #[test]
    fn adds_missing_nullable_column_on_both_backends() {
        let schema = invoice_schema();
        for (backend, varchar_spelling) in [
            (SqlDialect::Sqlite, "varchar"),
            (SqlDialect::Postgres, "character varying"),
        ] {
            let live_cols = vec![
                LiveColumn {
                    is_primary_key: true,
                    ..live("id", "integer")
                },
                LiveColumn {
                    is_nullable: false,
                    ..live("number", varchar_spelling)
                },
            ];
            let plan =
                plan_table_migration("invoice", &schema, &live_cols, backend, UPDATES).unwrap();
            assert_eq!(
                plan.statements.len(),
                1,
                "{:?}: {:?}",
                backend,
                plan.statements
            );
            let sql = &plan.statements[0];
            assert!(sql.contains("ALTER TABLE \"invoice\""), "{sql}");
            assert!(sql.contains("ADD COLUMN \"paid_date\""), "{sql}");
            assert!(
                plan.warnings.is_empty(),
                "{:?}: {:?}",
                backend,
                plan.warnings
            );
        }
    }

    #[test]
    fn add_column_reuses_create_table_type_spelling() {
        let schema = invoice_schema();
        let live_cols = vec![
            LiveColumn {
                is_primary_key: true,
                ..live("id", "integer")
            },
            live("number", "varchar"),
        ];
        let plan =
            plan_table_migration("invoice", &schema, &live_cols, SqlDialect::Sqlite, UPDATES)
                .unwrap();
        // db_type "date" renders date_text on SQLite — identical to CREATE TABLE.
        assert!(
            plan.statements[0].contains("date_text"),
            "{}",
            plan.statements[0]
        );
    }

    #[test]
    fn missing_not_null_column_with_literal_default_backfills() {
        let schema = json!({
            "properties": {
                "id": {"type": "integer", "primary_key": true},
                "status": {"type": "string", "ferro_nullable": false, "default": "draft"},
            }
        });
        let live_cols = vec![LiveColumn {
            is_primary_key: true,
            ..live("id", "integer")
        }];

        let plan = plan_table_migration("doc", &schema, &live_cols, SqlDialect::Postgres, UPDATES)
            .unwrap();
        assert_eq!(plan.statements.len(), 2, "{:?}", plan.statements);
        assert!(
            plan.statements[0].contains("NOT NULL"),
            "{}",
            plan.statements[0]
        );
        assert!(
            plan.statements[0].contains("DEFAULT 'draft'"),
            "{}",
            plan.statements[0]
        );
        assert!(
            plan.statements[1].contains("DROP DEFAULT"),
            "Postgres must not keep the backfill default: {}",
            plan.statements[1]
        );

        let plan =
            plan_table_migration("doc", &schema, &live_cols, SqlDialect::Sqlite, UPDATES).unwrap();
        assert_eq!(
            plan.statements.len(),
            1,
            "SQLite cannot DROP DEFAULT: {:?}",
            plan.statements
        );
        assert!(
            plan.statements[0].contains("DEFAULT 'draft'"),
            "{}",
            plan.statements[0]
        );
    }

    #[test]
    fn missing_not_null_column_without_default_fails_loudly() {
        let schema = json!({
            "properties": {
                "id": {"type": "integer", "primary_key": true},
                "created_at": {"type": "string", "format": "date-time", "ferro_nullable": false},
            }
        });
        let live_cols = vec![LiveColumn {
            is_primary_key: true,
            ..live("id", "integer")
        }];

        for backend in [SqlDialect::Sqlite, SqlDialect::Postgres] {
            let err =
                plan_table_migration("doc", &schema, &live_cols, backend, UPDATES).unwrap_err();
            let message = err.to_string();
            assert!(message.contains("doc.created_at"), "{message}");
            assert!(message.contains("Alembic"), "{message}");
        }
    }

    #[test]
    fn missing_primary_key_column_fails_loudly() {
        let schema = json!({
            "properties": {
                "id": {"type": "integer", "primary_key": true},
                "name": {"type": "string"},
            }
        });
        let live_cols = vec![live("name", "varchar")];
        let err = plan_table_migration("doc", &schema, &live_cols, SqlDialect::Sqlite, UPDATES)
            .unwrap_err();
        assert!(err.to_string().contains("primary key"), "{err}");
    }

    #[test]
    fn unique_column_add_strips_inline_unique_on_sqlite() {
        let schema = json!({
            "properties": {
                "id": {"type": "integer", "primary_key": true},
                "slug": {"type": "string", "unique": true},
            }
        });
        let live_cols = vec![LiveColumn {
            is_primary_key: true,
            ..live("id", "integer")
        }];

        let plan =
            plan_table_migration("doc", &schema, &live_cols, SqlDialect::Sqlite, UPDATES).unwrap();
        assert!(
            !plan.statements[0].to_uppercase().contains("UNIQUE"),
            "inline UNIQUE must be stripped on SQLite: {}",
            plan.statements[0]
        );
        assert!(
            plan.statements[1].contains("CREATE UNIQUE INDEX IF NOT EXISTS \"uq_doc_slug\""),
            "{}",
            plan.statements[1]
        );
        assert_eq!(plan.warnings.len(), 1);

        let plan = plan_table_migration("doc", &schema, &live_cols, SqlDialect::Postgres, UPDATES)
            .unwrap();
        assert!(
            plan.statements[0].to_uppercase().contains("UNIQUE"),
            "Postgres keeps the inline UNIQUE: {}",
            plan.statements[0]
        );
        assert!(plan.warnings.is_empty());
    }

    #[test]
    fn indexed_column_add_emits_index_sql() {
        let schema = json!({
            "properties": {
                "id": {"type": "integer", "primary_key": true},
                "status": {"type": "string", "index": true},
            }
        });
        let live_cols = vec![LiveColumn {
            is_primary_key: true,
            ..live("id", "integer")
        }];
        let plan =
            plan_table_migration("doc", &schema, &live_cols, SqlDialect::Sqlite, UPDATES).unwrap();
        assert_eq!(plan.statements.len(), 2, "{:?}", plan.statements);
        assert!(
            plan.statements[1].contains("\"idx_doc_status\""),
            "{}",
            plan.statements[1]
        );
    }

    #[test]
    fn fk_shadow_column_add_handles_backend_capabilities() {
        let schema = json!({
            "properties": {
                "id": {"type": "integer", "primary_key": true},
                "client_id": {
                    "type": "integer",
                    "foreign_key": {"to_table": "client", "on_delete": "CASCADE"},
                },
            }
        });
        let live_cols = vec![LiveColumn {
            is_primary_key: true,
            ..live("id", "integer")
        }];

        let plan = plan_table_migration(
            "invoice",
            &schema,
            &live_cols,
            SqlDialect::Postgres,
            UPDATES,
        )
        .unwrap();
        assert_eq!(plan.statements.len(), 2, "{:?}", plan.statements);
        assert_eq!(
            plan.statements[1],
            "ALTER TABLE \"invoice\" ADD FOREIGN KEY (\"client_id\") REFERENCES \"client\" (\"id\") ON DELETE CASCADE"
        );

        let plan =
            plan_table_migration("invoice", &schema, &live_cols, SqlDialect::Sqlite, UPDATES)
                .unwrap();
        assert_eq!(plan.statements.len(), 1, "{:?}", plan.statements);
        assert_eq!(plan.warnings.len(), 1);
        assert!(
            plan.warnings[0].contains("FOREIGN KEY"),
            "{}",
            plan.warnings[0]
        );
    }

    #[test]
    fn pg_type_mismatch_emits_alter_with_using_cast() {
        let schema = json!({
            "properties": {
                "id": {"type": "integer", "primary_key": true},
                "total": {"type": "integer", "db_type": "bigint", "ferro_nullable": false},
            }
        });
        let live_cols = vec![
            LiveColumn {
                is_primary_key: true,
                ..live("id", "integer")
            },
            LiveColumn {
                is_nullable: false,
                ..live("total", "integer")
            },
        ];
        let plan = plan_table_migration(
            "invoice",
            &schema,
            &live_cols,
            SqlDialect::Postgres,
            UPDATES,
        )
        .unwrap();
        assert_eq!(plan.statements.len(), 1, "{:?}", plan.statements);
        assert_eq!(
            plan.statements[0],
            "ALTER TABLE \"invoice\" ALTER COLUMN \"total\" TYPE bigint USING \"total\"::bigint"
        );
    }

    #[test]
    fn pg_nullability_mismatch_emits_set_and_drop_not_null() {
        let schema = json!({
            "properties": {
                "id": {"type": "integer", "primary_key": true},
                "a": {"type": "string", "ferro_nullable": false},
                "b": {"type": "string", "ferro_nullable": true},
            }
        });
        let live_cols = vec![
            LiveColumn {
                is_primary_key: true,
                ..live("id", "integer")
            },
            LiveColumn {
                declared_type: "character varying".to_string(),
                ..live("a", "character varying")
            },
            LiveColumn {
                is_nullable: false,
                ..live("b", "character varying")
            },
        ];
        let plan = plan_table_migration("doc", &schema, &live_cols, SqlDialect::Postgres, UPDATES)
            .unwrap();
        assert!(
            plan.statements
                .contains(&"ALTER TABLE \"doc\" ALTER COLUMN \"a\" SET NOT NULL".to_string()),
            "{:?}",
            plan.statements
        );
        assert!(
            plan.statements
                .contains(&"ALTER TABLE \"doc\" ALTER COLUMN \"b\" DROP NOT NULL".to_string()),
            "{:?}",
            plan.statements
        );
    }

    #[test]
    fn pg_enum_udt_columns_are_never_type_reconciled() {
        let schema = json!({
            "properties": {
                "id": {"type": "integer", "primary_key": true},
                "status": {"type": "string"},
            }
        });
        let live_cols = vec![
            LiveColumn {
                is_primary_key: true,
                ..live("id", "integer")
            },
            LiveColumn {
                is_enum_udt: true,
                declared_type: "USER-DEFINED".to_string(),
                ..live("status", "USER-DEFINED")
            },
        ];
        let plan = plan_table_migration("doc", &schema, &live_cols, SqlDialect::Postgres, UPDATES)
            .unwrap();
        assert!(plan.statements.is_empty(), "{:?}", plan.statements);
    }

    #[test]
    fn sqlite_type_drift_warns_only_on_storage_class_change() {
        let schema = json!({
            "properties": {
                "id": {"type": "integer", "primary_key": true},
                "created_at": {"type": "string", "format": "date-time"},
                "count": {"type": "integer"},
            }
        });
        // DATETIME (Alembic spelling) vs timestamp_with_timezone_text: same
        // storage class, no warning. varchar vs integer: real drift, warn.
        let live_cols = vec![
            LiveColumn {
                is_primary_key: true,
                ..live("id", "integer")
            },
            live("created_at", "DATETIME"),
            live("count", "varchar"),
        ];
        let plan =
            plan_table_migration("doc", &schema, &live_cols, SqlDialect::Sqlite, UPDATES).unwrap();
        assert!(plan.statements.is_empty(), "{:?}", plan.statements);
        assert_eq!(plan.warnings.len(), 1, "{:?}", plan.warnings);
        assert!(
            plan.warnings[0].contains("doc.count"),
            "{}",
            plan.warnings[0]
        );
        assert!(plan.warnings[0].contains("Alembic"), "{}", plan.warnings[0]);
    }

    #[test]
    fn sqlite_nullability_drift_warns() {
        let schema = json!({
            "properties": {
                "id": {"type": "integer", "primary_key": true},
                "name": {"type": "string", "ferro_nullable": false},
            }
        });
        let live_cols = vec![
            LiveColumn {
                is_primary_key: true,
                ..live("id", "integer")
            },
            live("name", "varchar"),
        ];
        let plan =
            plan_table_migration("doc", &schema, &live_cols, SqlDialect::Sqlite, UPDATES).unwrap();
        assert!(plan.statements.is_empty());
        assert_eq!(plan.warnings.len(), 1);
        assert!(
            plan.warnings[0].contains("NOT NULL"),
            "{}",
            plan.warnings[0]
        );
    }

    #[test]
    fn destructive_collects_removed_columns_and_protects_pk() {
        let schema = json!({
            "properties": {
                "id": {"type": "integer", "primary_key": true},
                "name": {"type": "string"},
            }
        });
        let live_cols = vec![
            LiveColumn {
                is_primary_key: true,
                ..live("id", "integer")
            },
            live("name", "varchar"),
            live("legacy_notes", "text"),
        ];

        let plan =
            plan_table_migration("doc", &schema, &live_cols, SqlDialect::Sqlite, DESTRUCTIVE)
                .unwrap();
        assert_eq!(plan.drop_columns, vec!["legacy_notes".to_string()]);

        // Without the destructive flag the extra column is untouched.
        let plan =
            plan_table_migration("doc", &schema, &live_cols, SqlDialect::Sqlite, UPDATES).unwrap();
        assert!(plan.drop_columns.is_empty());

        // A live PK column missing from the model is a hard error.
        let schema_without_id = json!({
            "properties": {
                "name": {"type": "string"},
            }
        });
        let err = plan_table_migration(
            "doc",
            &schema_without_id,
            &live_cols,
            SqlDialect::Sqlite,
            DESTRUCTIVE,
        )
        .unwrap_err();
        assert!(err.to_string().contains("primary key"), "{err}");
    }

    #[test]
    fn no_updates_flag_produces_empty_plan() {
        let schema = invoice_schema();
        let live_cols = vec![live("number", "varchar")];
        let plan = plan_table_migration(
            "invoice",
            &schema,
            &live_cols,
            SqlDialect::Sqlite,
            MigrateOptions::default(),
        )
        .unwrap();
        assert!(plan.is_empty());
        assert!(plan.warnings.is_empty());
    }

    #[test]
    fn ladder_implies_updates_from_destructive() {
        let opts = MigrateOptions::laddered(false, true);
        assert!(opts.updates);
        assert!(opts.destructive);
    }

    #[test]
    fn sqlite_type_classes_group_storage_equivalent_spellings() {
        for (a, b) in [
            ("DATETIME", "timestamp_with_timezone_text"),
            ("DATE", "date_text"),
            ("uuid_text", "char(32)"),
            ("JSON", "json_text"),
            ("NUMERIC", "real"),
            ("BOOLEAN", "integer"),
            ("BIGINT", "integer"),
            ("VARCHAR(3)", "varchar"),
        ] {
            assert_eq!(
                sqlite_type_class(a),
                sqlite_type_class(b),
                "{a} and {b} should be storage-equivalent"
            );
        }
        assert_ne!(sqlite_type_class("integer"), sqlite_type_class("varchar"));
        assert_ne!(sqlite_type_class("blob"), sqlite_type_class("text"));
    }

    #[test]
    fn render_helper_outputs_drop_statements() {
        let schema = json!({
            "properties": {
                "id": {"type": "integer", "primary_key": true},
            }
        });
        let live_json = json!([
            {"name": "id", "declared_type": "integer", "is_primary_key": true, "is_nullable": false},
            {"name": "stale", "declared_type": "varchar"},
        ]);
        let (statements, warnings) = _render_migration_sql_for_test(
            "Doc".to_string(),
            schema.to_string(),
            live_json.to_string(),
            "sqlite".to_string(),
            true,
            true,
        )
        .unwrap();
        assert_eq!(
            statements,
            vec!["ALTER TABLE \"doc\" DROP COLUMN \"stale\"".to_string()]
        );
        assert!(warnings.is_empty());
    }
}
