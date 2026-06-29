//! Auto-migrate schema diffing and execution.
//!
//! Extends `connect(auto_migrate=True)` beyond table creation: with
//! `migrate_updates`, missing model columns are added to existing tables
//! (plus, on Postgres, type/nullability reconciliation); with
//! `migrate_destructive`, live columns that no longer exist on the model are
//! dropped. Capability matrix and semantics are documented on the Python
//! `ferro.connect` / `ferro.migrate` APIs.
//!
//! Column DDL for auto-migrate is planned via SchemaIR diffing (`plan_from_ir`) and
//! lowered by `ferro-migrate` (`emit_sql_with_ir`), so an auto-migrated database
//! matches a freshly created one (AGENTS.md § I-1). The legacy enriched-JSON
//! planner (`plan_table_migration_legacy`) is retained for shadow comparison until
//! Phase 9.

use crate::backend::EngineHandle;
use ferro_ddl_lowering::{
    CanonicalType, Dialect, apply_canonical_type, canonical_to_db_type_token,
    information_schema_to_db_type_token, schema_columns_storage_drift,
};
use ferro_migrate::{BackendDialect, MigrationOp, emit_sql_with_ir, plan_from_ir};
use ferro_schema_ir::{
    IrEnvelope, SchemaCheck, SchemaColumn, SchemaForeignKey, SchemaIndex, SchemaIrPayload,
    SchemaModel, SchemaUnique,
};
use crate::introspect::{
    LiveColumn, LiveIndex, live_table_columns, live_table_indexes, quote_ident,
    sqlite_indexes_covering_column,
};
use crate::schema::{
    ColumnPlan, build_column_plan, internal_create_tables,
    order_schemas_for_creation, sql_dialect_to_lowering,
};
use crate::state::{IDENTITY_MAP, MODEL_REGISTRY, SqlDialect, engine_for_connection};
use pyo3::prelude::*;
use sea_query::{
    Alias, ColumnDef, ForeignKeyAction, Index, PostgresQueryBuilder, SqliteQueryBuilder, Table,
};
use std::collections::{HashMap, HashSet};
use std::sync::Arc;

fn resolve_col_schema<'a>(
    schema: &'a serde_json::Value,
    raw_col: &'a serde_json::Value,
) -> &'a serde_json::Value {
    if let Some(ref_path) = raw_col.get("$ref").and_then(|r| r.as_str()) {
        if let Some(def_name) = ref_path.strip_prefix("#/$defs/") {
            return schema
                .get("$defs")
                .and_then(|defs| defs.get(def_name))
                .unwrap_or(raw_col);
        }
    }
    raw_col
}

fn schema_ir_column<'a>(
    envelope: &'a IrEnvelope<SchemaIrPayload>,
    table: &str,
    column: &str,
) -> Option<&'a SchemaColumn> {
    envelope
        .payload
        .models
        .iter()
        .find(|model| model.table_name == table)
        .and_then(|model| model.columns.iter().find(|col| col.name == column))
}

fn backend_dialect(backend: SqlDialect) -> BackendDialect {
    match backend {
        SqlDialect::Sqlite => BackendDialect::Sqlite,
        SqlDialect::Postgres => BackendDialect::Postgres,
    }
}

/// Push the Python-compiled SchemaIR modelset for the runtime migrate diff to
/// consume. Called by the `connect`/`migrate` Python wrappers after the registry
/// is complete and relationships are resolved.
#[pyfunction]
#[pyo3(name = "_set_schema_ir_modelset")]
pub fn _set_schema_ir_modelset(json: String) -> PyResult<()> {
    let envelope: IrEnvelope<SchemaIrPayload> = serde_json::from_str(&json).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("invalid schema_ir_modelset json: {e}"))
    })?;
    if envelope.ir_kind != "schema" {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "expected ir_kind 'schema', got '{}'", envelope.ir_kind
        )));
    }
    *crate::state::SCHEMA_IR_MODELSET.write().map_err(|_| {
        pyo3::exceptions::PyRuntimeError::new_err("Failed to lock SchemaIR modelset")
    })? = Some(envelope);
    Ok(())
}

/// Narrow the declared modelset to a single-model envelope for `table`,
/// matching the shape `plan_table_migration` expects (old vs new are both
/// single-model). Returns None if the model is absent from the modelset.
fn declared_envelope_for(
    modelset: &IrEnvelope<SchemaIrPayload>,
    table: &str,
) -> Option<IrEnvelope<SchemaIrPayload>> {
    let model = modelset.payload.models.iter().find(|m| m.table_name == table)?;
    Some(IrEnvelope {
        ir_kind: "schema".to_string(),
        ir_version: modelset.ir_version,
        payload: SchemaIrPayload {
            dialect_agnostic: modelset.payload.dialect_agnostic,
            models: vec![model.clone()],
        },
    })
}

fn live_columns_to_schema_ir(
    table_lower: &str,
    live: &[LiveColumn],
    live_indexes: &[LiveIndex],
    backend: SqlDialect,
) -> IrEnvelope<SchemaIrPayload> {
    let dialect = match backend {
        SqlDialect::Sqlite => ferro_ddl_lowering::Dialect::Sqlite,
        SqlDialect::Postgres => ferro_ddl_lowering::Dialect::Postgres,
    };
    let mut columns: Vec<SchemaColumn> = live
        .iter()
        .map(|col| SchemaColumn {
            name: col.name.clone(),
            logical_type: "unknown".to_string(),
            db_type: Some(information_schema_to_db_type_token(
                &col.declared_type,
                col.char_max_len,
                dialect,
            )),
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
            postgres_native_enum: col.is_enum_udt,
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
                indexes: live_indexes.iter().map(|i| SchemaIndex {
                    name: i.name.clone(), columns: i.columns.clone(), unique: i.unique,
                }).collect(),
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

/// The DDL spelling used in `ALTER TABLE ... ALTER COLUMN ... TYPE <x> USING <col>::<x>`.
fn pg_alter_type_target(canonical: CanonicalType) -> String {
    match canonical {
        CanonicalType::Integer => "integer".to_string(),
        CanonicalType::SmallInt => "smallint".to_string(),
        CanonicalType::BigInt => "bigint".to_string(),
        CanonicalType::Double => "double precision".to_string(),
        CanonicalType::Decimal => "numeric".to_string(),
        CanonicalType::Boolean => "boolean".to_string(),
        CanonicalType::Json => "json".to_string(),
        CanonicalType::Text => "text".to_string(),
        CanonicalType::Varchar(None) => "varchar".to_string(),
        CanonicalType::Varchar(Some(n)) => format!("varchar({n})"),
        CanonicalType::Char(n) => format!("char({n})"),
        CanonicalType::Uuid => "uuid".to_string(),
        CanonicalType::DateTime | CanonicalType::Timestamp => "timestamp".to_string(),
        CanonicalType::TimestampTz => "timestamptz".to_string(),
        CanonicalType::Date => "date".to_string(),
        CanonicalType::Time => "time".to_string(),
        CanonicalType::Blob => "bytea".to_string(),
    }
}

/// The declared-type string sea-query's SQLite builder renders for each
/// canonical type (pinned by the cross-emitter parity tests).
fn sqlite_declared_type(canonical: CanonicalType) -> String {
    match canonical {
        CanonicalType::Integer => "integer".to_string(),
        CanonicalType::SmallInt => "smallint".to_string(),
        CanonicalType::BigInt => "bigint".to_string(),
        CanonicalType::Double => "double".to_string(),
        CanonicalType::Decimal => "real".to_string(),
        CanonicalType::Boolean => "boolean".to_string(),
        CanonicalType::Json => "json_text".to_string(),
        CanonicalType::Text => "text".to_string(),
        CanonicalType::Varchar(None) => "varchar".to_string(),
        CanonicalType::Varchar(Some(n)) => format!("varchar({n})"),
        CanonicalType::Char(n) => format!("char({n})"),
        CanonicalType::Uuid => "uuid_text".to_string(),
        CanonicalType::DateTime | CanonicalType::Timestamp => "datetime_text".to_string(),
        CanonicalType::TimestampTz => "timestamp_with_timezone_text".to_string(),
        CanonicalType::Date => "date_text".to_string(),
        CanonicalType::Time => "time_text".to_string(),
        CanonicalType::Blob => "blob".to_string(),
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
fn single_unique_index_name(table_lower: &str, col_name: &str) -> String {
    let raw = format!("uq_{}_{}", table_lower, col_name);
    if raw.chars().count() > 63 {
        return format!("{}_uq", raw.chars().take(60).collect::<String>());
    }
    raw
}

fn fk_action_sql(action: ForeignKeyAction) -> &'static str {
    match action {
        ForeignKeyAction::Restrict => "RESTRICT",
        ForeignKeyAction::SetNull => "SET NULL",
        ForeignKeyAction::SetDefault => "SET DEFAULT",
        ForeignKeyAction::NoAction => "NO ACTION",
        ForeignKeyAction::Cascade => "CASCADE",
    }
}

/// Convert a JSON-schema scalar default into a sea-query literal usable to
/// backfill a NOT NULL column add. Non-scalars (and `null`) are not usable.
fn literal_default_value(default: &serde_json::Value) -> Option<sea_query::Value> {
    match default {
        serde_json::Value::Bool(value) => Some((*value).into()),
        serde_json::Value::Number(value) => value
            .as_i64()
            .map(sea_query::Value::from)
            .or_else(|| value.as_f64().map(sea_query::Value::from)),
        serde_json::Value::String(value) => Some(value.clone().into()),
        _ => None,
    }
}

fn render_alter(stmt: &sea_query::TableAlterStatement, backend: SqlDialect) -> String {
    match backend {
        SqlDialect::Sqlite => stmt.to_string(SqliteQueryBuilder),
        SqlDialect::Postgres => stmt.to_string(PostgresQueryBuilder),
    }
}

/// Diff one registered model schema against its live table and produce the
/// DDL plan. Pure with respect to the database — callers introspect first.
///
/// # Errors
/// Returns a `PyErr` for changes that cannot be applied safely: adding a
/// primary-key column, or adding a NOT NULL column without a usable literal
/// default. These abort the migration ("fail loudly").
pub fn plan_table_migration(
    table_lower: &str,
    declared: &IrEnvelope<SchemaIrPayload>,
    live: &[LiveColumn],
    live_indexes: &[LiveIndex],
    backend: SqlDialect,
    opts: MigrateOptions,
) -> PyResult<MigrationPlan> {
    if !opts.updates {
        return Ok(MigrationPlan::new());
    }

    let old_ir = live_columns_to_schema_ir(table_lower, live, live_indexes, backend);
    let new_ir = declared;
    let mut typed_plan = plan_from_ir(&old_ir, new_ir, backend_dialect(backend));

    if !opts.destructive {
        typed_plan
            .operations
            .retain(|op| !matches!(op, MigrationOp::DropColumn { .. } | MigrationOp::DropIndex { .. }));
    }

    let mut plan = MigrationPlan::new();
    let mut exec_ops = Vec::new();

    for operation in typed_plan.operations {
        if let MigrationOp::DropColumn { table, column } = &operation {
            let Some(old_col) = schema_ir_column(&old_ir, table, column) else {
                return Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "Cannot drop column '{}.{}': column metadata missing from live IR context.",
                    table, column
                )));
            };
            if old_col.primary_key {
                return Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "Cannot drop column '{}.{}': it is part of the primary key. \
                     Primary-key changes must be migrated with Alembic.",
                    table, column
                )));
            }
            plan.drop_columns.push(column.clone());
        } else {
            exec_ops.push(operation);
        }
    }

    let exec_plan = ferro_migrate::MigrationPlan {
        operations: exec_ops,
        warnings: typed_plan.warnings,
    };
    let emission = emit_sql_with_ir(&exec_plan, &old_ir, new_ir, backend_dialect(backend))
        .map_err(|err| pyo3::exceptions::PyValueError::new_err(err.message))?;

    plan.statements = emission.statements;
    plan.warnings = emission.warnings;
    Ok(plan)
}

/// Deprecated enriched-JSON migration planner retained for #120 shadow comparison.
/// Phase 9 ([#108](https://github.com/syn54x/ferro-orm/issues/108)) removes this path.
fn plan_table_migration_legacy(
    table_lower: &str,
    schema: &serde_json::Value,
    live: &[LiveColumn],
    _live_indexes: &[LiveIndex],
    backend: SqlDialect,
    opts: MigrateOptions,
) -> PyResult<MigrationPlan> {
    let mut plan = MigrationPlan::new();
    if !opts.updates {
        return Ok(plan);
    }

    let live_by_name: HashMap<&str, &LiveColumn> =
        live.iter().map(|col| (col.name.as_str(), col)).collect();

    let mut model_columns: HashSet<&str> = HashSet::new();

    if let Some(properties) = schema.get("properties").and_then(|p| p.as_object()) {
        let mut col_names: Vec<&str> = properties.keys().map(String::as_str).collect();
        col_names.sort_unstable();
        for col_name in col_names {
            let raw_col_info = &properties[col_name];
            model_columns.insert(col_name);
            let col_plan =
                build_column_plan(table_lower, col_name, raw_col_info, schema, backend);

            match live_by_name.get(col_name) {
                None => plan_missing_column(table_lower, col_name, &col_plan, backend, &mut plan)?,
                Some(live_col) => {
                    plan_existing_column(
                        table_lower,
                        col_name,
                        &col_plan,
                        raw_col_info,
                        schema,
                        live_col,
                        backend,
                        &mut plan,
                    );
                }
            }
        }
    }

    if opts.destructive {
        let mut drop_columns: Vec<String> = Vec::new();
        for live_col in live {
            if model_columns.contains(live_col.name.as_str()) {
                continue;
            }
            if live_col.is_primary_key {
                return Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "Cannot drop column '{}.{}': it is part of the primary key. \
                     Primary-key changes must be migrated with Alembic.",
                    table_lower, live_col.name
                )));
            }
            drop_columns.push(live_col.name.clone());
        }
        drop_columns.sort();
        plan.drop_columns = drop_columns;
    }

    Ok(plan)
}

fn shadow_compare_migration_plan(
    table_lower: &str,
    declared: &IrEnvelope<SchemaIrPayload>,
    schema: &serde_json::Value,
    live: &[LiveColumn],
    live_indexes: &[LiveIndex],
    backend: SqlDialect,
    opts: MigrateOptions,
) -> Result<(), String> {
    if !opts.updates {
        return Ok(());
    }
    // Build the old and new IRs for the column-domain emission.
    let old_ir = live_columns_to_schema_ir(table_lower, live, live_indexes, backend);
    let mut typed_plan = plan_from_ir(&old_ir, declared, backend_dialect(backend));

    if !opts.destructive {
        typed_plan
            .operations
            .retain(|op| !matches!(op, MigrationOp::DropColumn { .. } | MigrationOp::DropIndex { .. }));
    }

    // Separate out DropColumn and standalone index ops; only column-domain ops
    // are compared to legacy (which never produces standalone index statements).
    let mut drop_columns: Vec<String> = Vec::new();
    let mut column_ops: Vec<MigrationOp> = Vec::new();
    for op in typed_plan.operations {
        match &op {
            MigrationOp::DropColumn { column, .. } => drop_columns.push(column.clone()),
            MigrationOp::AddIndex { .. } | MigrationOp::DropIndex { .. } => {
                // filtered out — legacy never produces standalone index ops
            }
            _ => column_ops.push(op),
        }
    }

    let column_plan = ferro_migrate::MigrationPlan {
        operations: column_ops,
        warnings: typed_plan.warnings,
    };
    let emission = emit_sql_with_ir(&column_plan, &old_ir, declared, backend_dialect(backend))
        .map_err(|e| e.message)?;

    let legacy = plan_table_migration_legacy(table_lower, schema, live, live_indexes, backend, opts)
        .map_err(|e| e.to_string())?;

    if emission.statements == legacy.statements
        && drop_columns == legacy.drop_columns
        && emission.warnings == legacy.warnings
    {
        return Ok(());
    }
    Err(format!(
        "shadow migration-plan mismatch for '{}': ir={} legacy={}",
        table_lower,
        serde_json::to_string(&emission.statements).unwrap_or_else(|_| "<ir>".to_string()),
        serde_json::to_string(&legacy.statements).unwrap_or_else(|_| "<legacy>".to_string())
    ))
}

/// Plan the `ADD COLUMN` (and any follow-up DDL) for a model column missing
/// from the live table.
fn plan_missing_column(
    table_lower: &str,
    col_name: &str,
    col_plan: &ColumnPlan,
    backend: SqlDialect,
    plan: &mut MigrationPlan,
) -> PyResult<()> {
    if col_plan.is_primary_key {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "Cannot add column '{}.{}': it is a primary key, and primary keys cannot \
             be added to existing tables. Use Alembic for this migration.",
            table_lower, col_name
        )));
    }

    // NOT NULL columns need a literal default to backfill existing rows.
    let backfill_default = if col_plan.is_nullable {
        None
    } else {
        let literal = col_plan
            .literal_default
            .as_ref()
            .and_then(literal_default_value);
        match literal {
            Some(value) => Some(value),
            None => {
                return Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "Cannot add NOT NULL column '{}.{}' to an existing table: it has no \
                     literal default to backfill existing rows. Make the field nullable, \
                     give it a literal default, or use Alembic for this migration.",
                    table_lower, col_name
                )));
            }
        }
    };

    // SQLite's ADD COLUMN cannot take a UNIQUE constraint; an explicit unique
    // index provides the same guarantee.
    let inline_unique = col_plan.is_unique && backend == SqlDialect::Postgres;

    let mut col_def = ColumnDef::new(Alias::new(col_name));
    apply_canonical_type(&mut col_def, col_plan.canonical);
    if !col_plan.is_nullable {
        col_def.not_null();
    }
    if inline_unique {
        col_def.unique_key();
    }
    if let Some(default_value) = &backfill_default {
        col_def.default(default_value.clone());
    }

    let stmt = Table::alter()
        .table(Alias::new(table_lower))
        .add_column(&mut col_def)
        .to_owned();
    plan.statements.push(render_alter(&stmt, backend));

    // The DEFAULT above exists only to backfill: a fresh CREATE TABLE emits no
    // server default, so drop it for parity. SQLite cannot drop a column
    // default; the leftover is documented (Alembic's compare_server_default
    // is off by default, so it produces no phantom diff).
    if backfill_default.is_some() && backend == SqlDialect::Postgres {
        plan.statements.push(format!(
            "ALTER TABLE {} ALTER COLUMN {} DROP DEFAULT",
            quote_ident(table_lower),
            quote_ident(col_name)
        ));
    }

    if col_plan.is_unique && backend == SqlDialect::Sqlite {
        let index_name = single_unique_index_name(table_lower, col_name);
        let index_stmt = Index::create()
            .unique()
            .name(&index_name)
            .table(Alias::new(table_lower))
            .col(Alias::new(col_name))
            .if_not_exists()
            .to_owned();
        plan.statements
            .push(index_stmt.to_string(SqliteQueryBuilder));
        plan.warnings.push(format!(
            "Added unique column '{}.{}' as a unique index '{}' (SQLite cannot add an \
             inline UNIQUE constraint to an existing table).",
            table_lower, col_name, index_name
        ));
    }

    // Single-column index and db_check constraint, exactly as CREATE TABLE
    // would emit them.
    plan.statements.extend(col_plan.index_sqls.iter().cloned());

    if let Some(fk) = &col_plan.fk {
        match backend {
            SqlDialect::Postgres => {
                // Unnamed, so Postgres assigns "<table>_<col>_fkey" — the same
                // name an inline FK from CREATE TABLE receives.
                plan.statements.push(format!(
                    "ALTER TABLE {} ADD FOREIGN KEY ({}) REFERENCES {} (\"id\") ON DELETE {}",
                    quote_ident(table_lower),
                    quote_ident(col_name),
                    quote_ident(&fk.to_table),
                    fk_action_sql(fk.on_delete)
                ));
            }
            SqlDialect::Sqlite => {
                plan.warnings.push(format!(
                    "Added foreign-key column '{}.{}' without its FOREIGN KEY constraint \
                     (SQLite cannot add table constraints to an existing table). Referential \
                     integrity for this column is not database-enforced; use Alembic if you \
                     need the constraint.",
                    table_lower, col_name
                ));
            }
        }
    }

    Ok(())
}

fn schema_column_for_drift(
    name: &str,
    db_type: String,
    nullable: bool,
    primary_key: bool,
) -> SchemaColumn {
    SchemaColumn {
        name: name.to_string(),
        logical_type: "unknown".to_string(),
        db_type: Some(db_type),
        db_type_explicit: None,
        nullable,
        primary_key,
        autoincrement: false,
        unique: false,
        index: false,
        default: None,
        format: None,
        enum_values: None,
        enum_type_name: None,
        postgres_native_enum: false,
    }
}

/// Reconcile an existing live column with the model definition. Postgres gets
/// native `ALTER COLUMN` DDL; SQLite cannot alter columns in place, so drift
/// surfaces as warnings (and is usually cosmetic there thanks to type
/// affinity).
fn plan_existing_column(
    table_lower: &str,
    col_name: &str,
    col_plan: &ColumnPlan,
    raw_col_info: &serde_json::Value,
    schema: &serde_json::Value,
    live: &LiveColumn,
    backend: SqlDialect,
    plan: &mut MigrationPlan,
) {
    // Primary keys (incl. autoincrement/serial) are never reconciled.
    if col_plan.is_primary_key || live.is_primary_key {
        return;
    }

    match backend {
        SqlDialect::Postgres => {
            let dialect = Dialect::Postgres;
            let resolved = resolve_col_schema(schema, raw_col_info);
            let model_db_type = raw_col_info
                .get("db_type")
                .or_else(|| resolved.get("db_type"))
                .and_then(|v| v.as_str())
                .map(str::to_string)
                .unwrap_or_else(|| canonical_to_db_type_token(col_plan.canonical, sql_dialect_to_lowering(backend)));
            let live_db_type = information_schema_to_db_type_token(
                &live.declared_type,
                live.char_max_len,
                dialect,
            );
            let live_col = schema_column_for_drift(col_name, live_db_type, live.is_nullable, false);
            let model_col =
                schema_column_for_drift(col_name, model_db_type, col_plan.is_nullable, false);
            // Native enum columns only exist via Alembic; leave them to it.
            if !live.is_enum_udt
                && schema_columns_storage_drift(&live_col, &model_col, dialect)
            {
                let target = pg_alter_type_target(col_plan.canonical);
                plan.statements.push(format!(
                    "ALTER TABLE {table} ALTER COLUMN {col} TYPE {target} USING {col}::{target}",
                    table = quote_ident(table_lower),
                    col = quote_ident(col_name),
                    target = target,
                ));
            }
            if !col_plan.is_nullable && live.is_nullable {
                plan.statements.push(format!(
                    "ALTER TABLE {} ALTER COLUMN {} SET NOT NULL",
                    quote_ident(table_lower),
                    quote_ident(col_name)
                ));
            } else if col_plan.is_nullable && !live.is_nullable {
                plan.statements.push(format!(
                    "ALTER TABLE {} ALTER COLUMN {} DROP NOT NULL",
                    quote_ident(table_lower),
                    quote_ident(col_name)
                ));
            }
        }
        SqlDialect::Sqlite => {
            let live_db_type = information_schema_to_db_type_token(
                &live.declared_type,
                live.char_max_len,
                Dialect::Sqlite,
            );
            let expected = sqlite_declared_type(col_plan.canonical);
            if sqlite_type_class(&live_db_type) != sqlite_type_class(&expected) {
                plan.warnings.push(format!(
                    "Column '{}.{}' is declared '{}' in the database but the model expects \
                     '{}'. SQLite cannot change column types in place; use Alembic to \
                     migrate this column.",
                    table_lower, col_name, live_db_type, expected
                ));
            }
            if col_plan.is_nullable != live.is_nullable {
                plan.warnings.push(format!(
                    "Column '{}.{}' is {} in the database but the model expects {}. SQLite \
                     cannot change column nullability in place; use Alembic to migrate \
                     this column.",
                    table_lower,
                    col_name,
                    if live.is_nullable {
                        "nullable"
                    } else {
                        "NOT NULL"
                    },
                    if col_plan.is_nullable {
                        "nullable"
                    } else {
                        "NOT NULL"
                    },
                ));
            }
        }
    }
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
    let modelset = {
        let guard = crate::state::SCHEMA_IR_MODELSET.read().map_err(|_| {
            pyo3::exceptions::PyRuntimeError::new_err("Failed to lock SchemaIR modelset")
        })?;
        guard.clone().ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err(
            "SchemaIR modelset not set — connect()/migrate() must push it before migrating"
        ))?
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
        let live_indexes = live_table_indexes(&engine, &table_lower).await?;

        let Some(declared) = declared_envelope_for(&modelset, &table_lower) else { continue };
        let mut plan = plan_table_migration(&table_lower, &declared, &live, &live_indexes, backend, opts)?;
        if engine.is_shadow_runtime_enabled()
            && let Err(diff) =
                shadow_compare_migration_plan(&table_lower, &declared, &schema, &live, &live_indexes, backend, opts)
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
#[pyo3(signature = (name, schema_ir_json, live_columns_json, dialect, updates=true, destructive=false, live_indexes_json=String::new()))]
pub fn _render_migration_sql_for_test(
    name: String,
    schema_ir_json: String,
    live_columns_json: String,
    dialect: String,
    updates: bool,
    destructive: bool,
    live_indexes_json: String,
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
    let declared: IrEnvelope<SchemaIrPayload> = serde_json::from_str(&schema_ir_json).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("invalid schema_ir_json: {e}"))
    })?;
    let live: Vec<LiveColumn> = serde_json::from_str(&live_columns_json).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("Invalid live-columns JSON: {}", e))
    })?;
    let live_indexes: Vec<LiveIndex> = if live_indexes_json.is_empty() {
        Vec::new()
    } else {
        serde_json::from_str(&live_indexes_json).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("Invalid live-indexes JSON: {}", e))
        })?
    };

    let table_lower = name.to_lowercase();
    let opts = MigrateOptions::laddered(updates, destructive);
    let plan = plan_table_migration(&table_lower, &declared, &live, &live_indexes, backend, opts)?;

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

fn migration_plan_snapshot(plan: &MigrationPlan) -> serde_json::Value {
    serde_json::json!({
        "statements": plan.statements,
        "drop_columns": plan.drop_columns,
        "warnings": plan.warnings,
    })
}

/// Test-only: compare IR-primary vs legacy migration planners for shadow fixtures.
///
/// Returns JSON with `matches`, `ir`, and `legacy` plan snapshots.
/// `schema_ir_json` drives the IR planner; `schema_json` drives the legacy planner.
#[pyfunction]
#[pyo3(name = "_shadow_compare_migration_plan_for_test")]
#[pyo3(signature = (name, schema_ir_json, schema_json, live_columns_json, dialect, updates=true, destructive=false, live_indexes_json=String::new()))]
pub fn _shadow_compare_migration_plan_for_test(
    name: String,
    schema_ir_json: String,
    schema_json: String,
    live_columns_json: String,
    dialect: String,
    updates: bool,
    destructive: bool,
    live_indexes_json: String,
) -> PyResult<String> {
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
    let new_ir: IrEnvelope<SchemaIrPayload> = serde_json::from_str(&schema_ir_json).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("invalid schema_ir_json: {e}"))
    })?;
    let schema: serde_json::Value = serde_json::from_str(&schema_json).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("Invalid JSON schema: {}", e))
    })?;
    let live: Vec<LiveColumn> = serde_json::from_str(&live_columns_json).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("Invalid live-columns JSON: {}", e))
    })?;
    let live_indexes: Vec<LiveIndex> = if live_indexes_json.is_empty() {
        Vec::new()
    } else {
        serde_json::from_str(&live_indexes_json).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("Invalid live-indexes JSON: {}", e))
        })?
    };

    let table_lower = name.to_lowercase();
    let opts = MigrateOptions::laddered(updates, destructive);

    // Build column-domain IR plan (filter AddIndex/DropIndex) for parity comparison.
    let old_ir = live_columns_to_schema_ir(&table_lower, &live, &live_indexes, backend);
    let mut typed_plan = plan_from_ir(&old_ir, &new_ir, backend_dialect(backend));
    if !opts.destructive {
        typed_plan
            .operations
            .retain(|op| !matches!(op, MigrationOp::DropColumn { .. } | MigrationOp::DropIndex { .. }));
    }
    let mut drop_columns: Vec<String> = Vec::new();
    let mut column_ops: Vec<MigrationOp> = Vec::new();
    for op in typed_plan.operations {
        match &op {
            MigrationOp::DropColumn { column, .. } => drop_columns.push(column.clone()),
            MigrationOp::AddIndex { .. } | MigrationOp::DropIndex { .. } => {}
            _ => column_ops.push(op),
        }
    }
    let column_plan = ferro_migrate::MigrationPlan {
        operations: column_ops,
        warnings: typed_plan.warnings,
    };
    let emission = emit_sql_with_ir(&column_plan, &old_ir, &new_ir, backend_dialect(backend))
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.message))?;

    let legacy = plan_table_migration_legacy(&table_lower, &schema, &live, &live_indexes, backend, opts)?;

    let matches = emission.statements == legacy.statements
        && drop_columns == legacy.drop_columns
        && emission.warnings == legacy.warnings;
    let ir_snapshot = serde_json::json!({
        "statements": emission.statements,
        "drop_columns": drop_columns,
        "warnings": emission.warnings,
    });
    let payload = serde_json::json!({
        "matches": matches,
        "ir": ir_snapshot,
        "legacy": migration_plan_snapshot(&legacy),
    });
    serde_json::to_string(&payload).map_err(|e| {
        pyo3::exceptions::PyRuntimeError::new_err(format!("Failed to encode JSON: {e}"))
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use ferro_ddl_lowering::{
        canonical_to_db_type_token, composite_index_name, composite_unique_index_name,
        db_check_constraint_name, single_index_name,
        single_unique_index_name as ddl_single_unique_index_name,
    };
    use crate::schema::property_json_type_and_format;
    use serde_json::json;

    fn migrate_column_bool(
        raw_col: &serde_json::Value,
        resolved: &serde_json::Value,
        key: &str,
    ) -> Option<bool> {
        raw_col
            .get(key)
            .or_else(|| resolved.get(key))
            .and_then(|value| value.as_bool())
    }

    fn migrate_column_object<'a>(
        raw_col: &'a serde_json::Value,
        resolved: &'a serde_json::Value,
        key: &str,
    ) -> Option<&'a serde_json::Map<String, serde_json::Value>> {
        raw_col
            .get(key)
            .or_else(|| resolved.get(key))
            .and_then(|value| value.as_object())
    }

    fn migrate_check_expression(col_name: &str, col_info: &serde_json::Value) -> Option<String> {
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
        Some(format!("\"{}\" IN ({})", col_name, rendered.join(", ")))
    }

    /// Test-only helper: convert a Ferro-enriched JSON schema into a single-model
    /// `IrEnvelope<SchemaIrPayload>`. Production code now consumes the Python-compiled
    /// modelset pushed via `_set_schema_ir_modelset`.
    fn schema_json_to_schema_ir(
        table_lower: &str,
        schema: &serde_json::Value,
        backend: SqlDialect,
    ) -> IrEnvelope<SchemaIrPayload> {
        let mut columns = Vec::new();
        let mut foreign_keys = Vec::new();
        let mut checks = Vec::new();
        let mut indexes: Vec<SchemaIndex> = Vec::new();
        let mut uniques: Vec<SchemaUnique> = Vec::new();
        if let Some(properties) = schema.get("properties").and_then(|p| p.as_object()) {
            for (name, raw_col) in properties {
                let resolved = resolve_col_schema(schema, raw_col);
                let col_plan = build_column_plan(table_lower, name, raw_col, schema, backend);
                let db_type_explicit = raw_col
                    .get("db_type")
                    .or_else(|| resolved.get("db_type"))
                    .and_then(|v| v.as_str());
                let db_type = db_type_explicit
                    .map(str::to_string)
                    .unwrap_or_else(|| canonical_to_db_type_token(col_plan.canonical, sql_dialect_to_lowering(backend)));
                columns.push(SchemaColumn {
                    name: name.clone(),
                    logical_type: property_json_type_and_format(resolved)
                        .0
                        .unwrap_or("unknown")
                        .to_string(),
                    db_type: Some(db_type),
                    db_type_explicit: db_type_explicit.map(|_| true),
                    nullable: col_plan.is_nullable,
                    primary_key: col_plan.is_primary_key,
                    autoincrement: col_plan.autoincrement,
                    unique: col_plan.is_unique,
                    index: migrate_column_bool(raw_col, resolved, "index").unwrap_or(false),
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
                    postgres_native_enum: false,
                });

                if migrate_column_bool(raw_col, resolved, "index").unwrap_or(false) {
                    indexes.push(SchemaIndex {
                        name: single_index_name(table_lower, name),
                        columns: vec![name.clone()],
                        unique: false,
                    });
                }
                if col_plan.is_unique {
                    uniques.push(SchemaUnique {
                        name: ddl_single_unique_index_name(table_lower, name),
                        columns: vec![name.clone()],
                    });
                }

                if let Some(fk_info) = migrate_column_object(raw_col, resolved, "foreign_key") {
                    let to_table = fk_info
                        .get("to_table")
                        .and_then(|t| t.as_str())
                        .unwrap_or("")
                        .to_string();
                    let on_delete = fk_info
                        .get("on_delete")
                        .and_then(|o| o.as_str())
                        .map(str::to_string);
                    foreign_keys.push(SchemaForeignKey {
                        column: name.clone(),
                        to_table,
                        to_column: fk_info
                            .get("to_column")
                            .and_then(|c| c.as_str())
                            .unwrap_or("id")
                            .to_string(),
                        on_delete,
                        name: None,
                    });
                }

                if migrate_column_bool(raw_col, resolved, "db_check").unwrap_or(false)
                    && let Some(expression) = migrate_check_expression(name, resolved)
                {
                    checks.push(SchemaCheck {
                        name: db_check_constraint_name(table_lower, name),
                        expression,
                    });
                }
            }
        }
        for group in schema.get("ferro_composite_indexes").and_then(|g| g.as_array()).into_iter().flatten() {
            let cols: Vec<String> = group.as_array().map(|a| a.iter().filter_map(|c| c.as_str().map(String::from)).collect()).unwrap_or_default();
            if cols.len() >= 2 {
                let refs: Vec<&str> = cols.iter().map(String::as_str).collect();
                indexes.push(SchemaIndex { name: composite_index_name(table_lower, &refs), columns: cols, unique: false });
            }
        }
        for group in schema.get("ferro_composite_uniques").and_then(|g| g.as_array()).into_iter().flatten() {
            let cols: Vec<String> = group.as_array().map(|a| a.iter().filter_map(|c| c.as_str().map(String::from)).collect()).unwrap_or_default();
            if cols.len() >= 2 {
                let refs: Vec<&str> = cols.iter().map(String::as_str).collect();
                uniques.push(SchemaUnique { name: composite_unique_index_name(table_lower, &refs), columns: cols });
            }
        }
        indexes.sort_by(|a, b| a.name.cmp(&b.name));
        uniques.sort_by(|a, b| a.name.cmp(&b.name));
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
                    indexes,
                    uniques,
                    checks,
                }],
            },
        }
    }

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

    fn plan_with_ir_legacy_parity(
        table: &str,
        schema: &serde_json::Value,
        live: &[LiveColumn],
        live_indexes: &[LiveIndex],
        backend: SqlDialect,
        opts: MigrateOptions,
    ) -> MigrationPlan {
        let declared = schema_json_to_schema_ir(table, schema, backend);
        let ir = plan_table_migration(table, &declared, live, live_indexes, backend, opts)
            .unwrap_or_else(|e| panic!("IR planner failed for '{table}': {e}"));
        let legacy = plan_table_migration_legacy(table, schema, live, live_indexes, backend, opts)
            .unwrap_or_else(|e| panic!("legacy planner failed for '{table}': {e}"));

        if !opts.updates {
            // Both planners return empty when updates is off; skip column-domain diffing.
            assert_eq!(ir.statements, legacy.statements, "statements mismatch on {table:?} ({backend:?})");
            assert_eq!(ir.drop_columns, legacy.drop_columns, "drop_columns mismatch on {table:?} ({backend:?})");
            assert_eq!(ir.warnings, legacy.warnings, "warnings mismatch on {table:?} ({backend:?})");
            shadow_compare_migration_plan(table, &declared, schema, live, live_indexes, backend, opts)
                .unwrap_or_else(|diff| panic!("shadow compare failed: {diff}"));
            return ir;
        }

        // Build column-domain IR emission for parity comparison.
        // AddIndex/DropIndex are filtered out because legacy never produces standalone index ops.
        let old_ir = live_columns_to_schema_ir(table, live, live_indexes, backend);
        let new_ir = schema_json_to_schema_ir(table, schema, backend);
        let mut typed_plan = plan_from_ir(&old_ir, &new_ir, backend_dialect(backend));
        if !opts.destructive {
            typed_plan
                .operations
                .retain(|op| !matches!(op, MigrationOp::DropColumn { .. } | MigrationOp::DropIndex { .. }));
        }
        let mut drop_columns_col: Vec<String> = Vec::new();
        let mut column_ops: Vec<MigrationOp> = Vec::new();
        for op in typed_plan.operations {
            match &op {
                MigrationOp::DropColumn { column, .. } => drop_columns_col.push(column.clone()),
                MigrationOp::AddIndex { .. } | MigrationOp::DropIndex { .. } => {}
                _ => column_ops.push(op),
            }
        }
        let column_plan_ops = ferro_migrate::MigrationPlan {
            operations: column_ops,
            warnings: typed_plan.warnings,
        };
        let emission = emit_sql_with_ir(&column_plan_ops, &old_ir, &new_ir, backend_dialect(backend))
            .unwrap_or_else(|e| panic!("IR column-domain emission failed for '{table}': {e:?}"));

        assert_eq!(
            emission.statements, legacy.statements,
            "statements mismatch on {table:?} ({backend:?})"
        );
        assert_eq!(
            drop_columns_col, legacy.drop_columns,
            "drop_columns mismatch on {table:?} ({backend:?})"
        );
        assert_eq!(
            emission.warnings, legacy.warnings,
            "warnings mismatch on {table:?} ({backend:?})"
        );
        shadow_compare_migration_plan(table, &declared, schema, live, live_indexes, backend, opts)
            .unwrap_or_else(|diff| panic!("shadow compare failed: {diff}"));
        ir
    }

    fn assert_ir_legacy_parity(
        table: &str,
        schema: &serde_json::Value,
        live: &[LiveColumn],
        live_indexes: &[LiveIndex],
        backend: SqlDialect,
        opts: MigrateOptions,
    ) {
        plan_with_ir_legacy_parity(table, schema, live, live_indexes, backend, opts);
    }

    fn assert_ir_legacy_parity_err(
        table: &str,
        schema: &serde_json::Value,
        live: &[LiveColumn],
        live_indexes: &[LiveIndex],
        backend: SqlDialect,
        opts: MigrateOptions,
        needle: &str,
    ) {
        let declared = schema_json_to_schema_ir(table, schema, backend);
        let ir_err = plan_table_migration(table, &declared, live, live_indexes, backend, opts)
            .expect_err("IR planner should fail");
        let legacy_err = plan_table_migration_legacy(table, schema, live, live_indexes, backend, opts)
            .expect_err("legacy planner should fail");
        assert!(
            ir_err.to_string().contains(needle),
            "IR error missing {needle:?}: {ir_err}"
        );
        assert!(
            legacy_err.to_string().contains(needle),
            "legacy error missing {needle:?}: {legacy_err}"
        );
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
                plan_with_ir_legacy_parity("invoice", &schema, &live_cols, &[], backend, UPDATES);
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
            plan_with_ir_legacy_parity("invoice", &schema, &live_cols, &[], SqlDialect::Sqlite, UPDATES);
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

        let plan = plan_with_ir_legacy_parity("doc", &schema, &live_cols, &[], SqlDialect::Postgres, UPDATES);
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
            plan_with_ir_legacy_parity("doc", &schema, &live_cols, &[], SqlDialect::Sqlite, UPDATES);
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
            let declared = schema_json_to_schema_ir("doc", &schema, backend);
            let err =
                plan_table_migration("doc", &declared, &live_cols, &[], backend, UPDATES).unwrap_err();
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
        let declared = schema_json_to_schema_ir("doc", &schema, SqlDialect::Sqlite);
        let err = plan_table_migration("doc", &declared, &live_cols, &[], SqlDialect::Sqlite, UPDATES)
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
            plan_with_ir_legacy_parity("doc", &schema, &live_cols, &[], SqlDialect::Sqlite, UPDATES);
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

        let plan = plan_with_ir_legacy_parity("doc", &schema, &live_cols, &[], SqlDialect::Postgres, UPDATES);
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
            plan_with_ir_legacy_parity("doc", &schema, &live_cols, &[], SqlDialect::Sqlite, UPDATES);
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

        let plan = plan_with_ir_legacy_parity(
            "invoice",
            &schema,
            &live_cols,
            &[],
            SqlDialect::Postgres,
            UPDATES,
        );
        assert_eq!(plan.statements.len(), 2, "{:?}", plan.statements);
        assert_eq!(
            plan.statements[1],
            "ALTER TABLE \"invoice\" ADD FOREIGN KEY (\"client_id\") REFERENCES \"client\" (\"id\") ON DELETE CASCADE"
        );

        let plan =
            plan_with_ir_legacy_parity("invoice", &schema, &live_cols, &[], SqlDialect::Sqlite, UPDATES);
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
        let plan = plan_with_ir_legacy_parity(
            "invoice",
            &schema,
            &live_cols,
            &[],
            SqlDialect::Postgres,
            UPDATES,
        );
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
        let plan = plan_with_ir_legacy_parity("doc", &schema, &live_cols, &[], SqlDialect::Postgres, UPDATES);
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
        let plan = plan_with_ir_legacy_parity("doc", &schema, &live_cols, &[], SqlDialect::Postgres, UPDATES);
        assert!(plan.statements.is_empty(), "{:?}", plan.statements);
    }

    #[test]
    fn optional_string_anyof_add_column_uses_varchar_spelling() {
        let schema = json!({
            "properties": {
                "id": {"type": "integer", "primary_key": true},
                "motto": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": null,
                },
            }
        });
        let live_cols = vec![LiveColumn {
            is_primary_key: true,
            ..live("id", "integer")
        }];
        let plan =
            plan_with_ir_legacy_parity("doc", &schema, &live_cols, &[], SqlDialect::Sqlite, UPDATES);
        let sql = &plan.statements[0];
        assert!(sql.contains("ADD COLUMN \"motto\""), "{sql}");
        assert!(
            !sql.to_uppercase().contains(" TEXT "),
            "Optional anyOf string columns must not render as TEXT: {sql}"
        );
    }

    #[test]
    fn pg_matching_primitive_columns_emit_no_type_alter() {
        let schema = json!({
            "properties": {
                "id": {"type": "integer", "primary_key": true},
                "is_active": {"type": "boolean"},
                "amount": {"type": "number"},
                "total": {"type": "number", "db_type": "numeric"},
                "meta": {"type": "object", "db_type": "json"},
            }
        });
        let live_cols = vec![
            LiveColumn {
                is_primary_key: true,
                ..live("id", "integer")
            },
            live("is_active", "boolean"),
            live("amount", "double precision"),
            live("total", "numeric"),
            LiveColumn {
                declared_type: "jsonb".to_string(),
                ..live("meta", "jsonb")
            },
        ];
        let plan = plan_with_ir_legacy_parity("doc", &schema, &live_cols, &[], SqlDialect::Postgres, UPDATES);
        assert!(
            !plan
                .statements
                .iter()
                .any(|sql| sql.contains("ALTER COLUMN") && sql.contains(" TYPE ")),
            "matching live/model types must not reconcile: {:?}",
            plan.statements
        );
    }

    #[test]
    fn pg_db_check_add_column_emits_named_check_constraint() {
        let schema = json!({
            "properties": {
                "id": {"type": "integer", "primary_key": true},
                "status": {
                    "type": "string",
                    "db_type": "text",
                    "db_check": true,
                    "enum": ["pending", "approved"],
                },
            }
        });
        let live_cols = vec![LiveColumn {
            is_primary_key: true,
            ..live("id", "integer")
        }];
        let plan = plan_with_ir_legacy_parity("doc", &schema, &live_cols, &[], SqlDialect::Postgres, UPDATES);
        assert!(
            plan.statements.iter().any(|sql| {
                sql.contains("ADD CONSTRAINT \"ck_doc_status\"")
                    && sql.contains("CHECK (\"status\" IN ('pending', 'approved'))")
            }),
            "{:?}",
            plan.statements
        );
    }

    #[test]
    fn pg_db_check_add_column_uses_truncated_constraint_name() {
        let long_col = "a".repeat(70);
        let table = "verylongtable";
        let expected_name = db_check_constraint_name(table, &long_col);
        assert_eq!(expected_name.chars().count(), 63);

        let mut properties = serde_json::Map::new();
        properties.insert("id".into(), json!({"type": "integer", "primary_key": true}));
        properties.insert(
            long_col.clone(),
            json!({
                "type": "string",
                "db_type": "text",
                "db_check": true,
                "enum": ["pending", "approved"],
            }),
        );
        let schema = json!({ "properties": properties });
        let live_cols = vec![LiveColumn {
            is_primary_key: true,
            ..live("id", "integer")
        }];
        let plan =
            plan_with_ir_legacy_parity(table, &schema, &live_cols, &[], SqlDialect::Postgres, UPDATES);
        let check_sql = plan
            .statements
            .iter()
            .find(|sql| sql.contains("ADD CONSTRAINT"))
            .expect("expected ADD CONSTRAINT for db_check column");
        assert!(
            check_sql.contains(&format!("ADD CONSTRAINT \"{expected_name}\"")),
            "{check_sql}"
        );
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
            plan_with_ir_legacy_parity("doc", &schema, &live_cols, &[], SqlDialect::Sqlite, UPDATES);
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
            plan_with_ir_legacy_parity("doc", &schema, &live_cols, &[], SqlDialect::Sqlite, UPDATES);
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
            plan_with_ir_legacy_parity("doc", &schema, &live_cols, &[], SqlDialect::Sqlite, DESTRUCTIVE);
        assert_eq!(plan.drop_columns, vec!["legacy_notes".to_string()]);

        // Without the destructive flag the extra column is untouched.
        let plan =
            plan_with_ir_legacy_parity("doc", &schema, &live_cols, &[], SqlDialect::Sqlite, UPDATES);
        assert!(plan.drop_columns.is_empty());

        // A live PK column missing from the model is a hard error.
        let schema_without_id = json!({
            "properties": {
                "name": {"type": "string"},
            }
        });
        for backend in [SqlDialect::Sqlite, SqlDialect::Postgres] {
            assert_ir_legacy_parity_err(
                "doc",
                &schema_without_id,
                &live_cols,
                &[],
                backend,
                DESTRUCTIVE,
                "primary key",
            );
        }
    }

    #[test]
    fn sqlite_drift_warnings_use_normalized_live_db_type_tokens() {
        let schema = json!({
            "properties": {
                "id": {"type": "integer", "primary_key": true},
                "count": {"type": "integer"},
            }
        });
        let live_cols = vec![
            LiveColumn {
                is_primary_key: true,
                declared_type: "INTEGER".to_string(),
                ..live("id", "integer")
            },
            LiveColumn {
                declared_type: "VARCHAR".to_string(),
                ..live("count", "varchar")
            },
        ];
        let plan = plan_with_ir_legacy_parity(
            "doc",
            &schema,
            &live_cols,
            &[],
            SqlDialect::Sqlite,
            UPDATES,
        );
        assert_eq!(plan.warnings.len(), 1, "{:?}", plan.warnings);
        assert!(
            plan.warnings[0].contains("declared 'varchar' in the database"),
            "{}",
            plan.warnings[0]
        );
    }

    #[test]
    fn no_updates_flag_produces_empty_plan() {
        let schema = invoice_schema();
        let live_cols = vec![live("number", "varchar")];
        let plan = plan_with_ir_legacy_parity(
            "invoice",
            &schema,
            &live_cols,
            &[],
            SqlDialect::Sqlite,
            MigrateOptions::default(),
        );
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
        let ir = schema_json_to_schema_ir("doc", &schema, SqlDialect::Sqlite);
        let ir_json = serde_json::to_string(&ir).unwrap();
        let live_json = json!([
            {"name": "id", "declared_type": "integer", "is_primary_key": true, "is_nullable": false},
            {"name": "stale", "declared_type": "varchar"},
        ]);
        let (statements, warnings) = _render_migration_sql_for_test(
            "Doc".to_string(),
            ir_json,
            live_json.to_string(),
            "sqlite".to_string(),
            true,
            true,
            String::new(),
        )
        .unwrap();
        assert_eq!(
            statements,
            vec!["ALTER TABLE \"doc\" DROP COLUMN \"stale\"".to_string()]
        );
        assert!(warnings.is_empty());
    }

    #[test]
    fn ir_legacy_parity_matrix() {
        let invoice = invoice_schema();
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
            assert_ir_legacy_parity("invoice", &invoice, &live_cols, &[], backend, UPDATES);
        }

        let not_null_default = json!({
            "properties": {
                "id": {"type": "integer", "primary_key": true},
                "status": {"type": "string", "ferro_nullable": false, "default": "draft"},
            }
        });
        let id_only = vec![LiveColumn {
            is_primary_key: true,
            ..live("id", "integer")
        }];
        assert_ir_legacy_parity(
            "doc",
            &not_null_default,
            &id_only,
            &[],
            SqlDialect::Postgres,
            UPDATES,
        );
        assert_ir_legacy_parity(
            "doc",
            &not_null_default,
            &id_only,
            &[],
            SqlDialect::Sqlite,
            UPDATES,
        );

        let not_null_no_default = json!({
            "properties": {
                "id": {"type": "integer", "primary_key": true},
                "created_at": {"type": "string", "format": "date-time", "ferro_nullable": false},
            }
        });
        for backend in [SqlDialect::Sqlite, SqlDialect::Postgres] {
            assert_ir_legacy_parity_err(
                "doc",
                &not_null_no_default,
                &id_only,
                &[],
                backend,
                UPDATES,
                "Alembic",
            );
        }

        let pk_guard = json!({
            "properties": {
                "id": {"type": "integer", "primary_key": true},
                "name": {"type": "string"},
            }
        });
        assert_ir_legacy_parity_err(
            "doc",
            &pk_guard,
            &[live("name", "varchar")],
            &[],
            SqlDialect::Sqlite,
            UPDATES,
            "primary key",
        );

        let destructive_pk_drop_schema = json!({
            "properties": {
                "name": {"type": "string"},
            }
        });
        let destructive_pk_drop_live = vec![
            LiveColumn {
                is_primary_key: true,
                ..live("id", "integer")
            },
            live("name", "varchar"),
            live("legacy_notes", "text"),
        ];
        for backend in [SqlDialect::Sqlite, SqlDialect::Postgres] {
            assert_ir_legacy_parity_err(
                "doc",
                &destructive_pk_drop_schema,
                &destructive_pk_drop_live,
                &[],
                backend,
                DESTRUCTIVE,
                "primary key",
            );
        }

        let destructive_schema = json!({
            "properties": {
                "id": {"type": "integer", "primary_key": true},
                "name": {"type": "string"},
            }
        });
        let destructive_live = vec![
            LiveColumn {
                is_primary_key: true,
                ..live("id", "integer")
            },
            live("name", "varchar"),
            live("legacy_notes", "text"),
        ];
        assert_ir_legacy_parity(
            "doc",
            &destructive_schema,
            &destructive_live,
            &[],
            SqlDialect::Sqlite,
            DESTRUCTIVE,
        );
        assert_ir_legacy_parity(
            "doc",
            &destructive_schema,
            &destructive_live,
            &[],
            SqlDialect::Postgres,
            DESTRUCTIVE,
        );

        let no_updates_live = vec![live("number", "varchar")];
        for backend in [SqlDialect::Sqlite, SqlDialect::Postgres] {
            assert_ir_legacy_parity(
                "invoice",
                &invoice,
                &no_updates_live,
                &[],
                backend,
                MigrateOptions::default(),
            );
        }

        let multi_col_schema = json!({
            "properties": {
                "id": {"type": "integer", "primary_key": true},
                "zebra": {"type": "string", "index": true},
                "alpha": {"type": "string", "unique": true},
            }
        });
        let multi_col_live = vec![LiveColumn {
            is_primary_key: true,
            ..live("id", "integer")
        }];
        assert_ir_legacy_parity(
            "doc",
            &multi_col_schema,
            &multi_col_live,
            &[],
            SqlDialect::Sqlite,
            UPDATES,
        );
    }
}
