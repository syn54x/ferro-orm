//! Auto-migrate schema diffing and execution.
//!
//! Extends `connect(auto_migrate=True)` beyond table creation: with
//! `migrate_updates`, existing tables are reconciled with the registered
//! models (missing columns, indexes, foreign keys and checks added; on
//! Postgres, type/nullability drift, enum labels and row security
//! reconciled); with `migrate_destructive`, what the models no longer declare
//! is dropped. Capability matrix and semantics are documented on the Python
//! `ferro.connect` / `ferro.migrate` APIs.
//!
//! The pass is read-live → plan → render → execute: the live database is read
//! into an IR plus its live facts (`crate::live_ir`), the one planner
//! (`ferro_migrate::plan_from_ir`) decides every change for the whole
//! modelset, and `ferro_migrate::render_plan` renders it through the same
//! `ferro_ddl_lowering` functions every migration door uses (AGENTS.md § I-1).

use crate::backend::EngineHandle;
use crate::introspect::{
    LiveCheck, LiveColumn, LiveForeignKey, LiveIndex, connected_role_bypasses_row_security,
    quote_ident, sqlite_indexes_covering_column,
};
use crate::live_ir::{LiveTable, live_schema_ir, live_tables_to_schema_ir};
use crate::schema::internal_create_tables;
use crate::state::{MODEL_REGISTRY, engine_for_connection};
use ferro_ddl_lowering::{Dialect, LiveRowSecurity, row_security_migrator_warning};
use ferro_migrate::{
    LiveFacts, MigrationOp, PlanOptions, RenderedOp, plan_from_ir, render_plan, validate_schema_ir,
};
use ferro_schema_ir::{IrEnvelope, SchemaIrPayload};
use pyo3::prelude::*;
use std::sync::Arc;

/// Atomically install the column registry, schema modelset, and modelset
/// fingerprint from one assembled payload (#244).
///
/// This is the single Rust registration sync seam: `connect()`,
/// `create_tables()`, and `migrate()` route through it. The heavy lifting
/// (build-then-swap, the fingerprint gate, the push counter) lives in
/// [`crate::state::install_registration`]; this wrapper only parses and
/// validates the payload envelope.
///
/// Returns `true` when an install was performed, `false` when the fingerprint
/// gate skipped it.
///
/// # Errors
/// `PyValueError` when the JSON is invalid, the envelope is not a `schema` IR,
/// or a model's columns cannot compile; `PyRuntimeError` on a poisoned lock.
#[pyfunction]
#[pyo3(name = "_install_registration")]
pub fn _install_registration(payload_json: String, fingerprint: String) -> PyResult<bool> {
    // Fast path: a warm reconnect skips the swap, so it need not pay the
    // payload parse. `install_registration` re-checks the gate authoritatively.
    if crate::state::installed_fingerprint_matches(&fingerprint)? {
        return Ok(false);
    }
    let envelope: IrEnvelope<SchemaIrPayload> = serde_json::from_str(&payload_json).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("invalid registration payload json: {e}"))
    })?;
    if envelope.ir_kind != "schema" {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "expected ir_kind 'schema', got '{}'",
            envelope.ir_kind
        )));
    }
    crate::state::install_registration(envelope, fingerprint)
}

/// Test-only instrument: the process-wide bulk-install count (#244).
///
/// Mirrors `_catalog_query_count_for_test` — a single counter bumped at one
/// choke point (`install_registration`, only on an actual swap, never on a
/// fingerprint-skip) and read from Python to assert install cardinality.
#[pyfunction]
#[pyo3(name = "_bulk_install_count_for_test")]
pub fn _bulk_install_count_for_test() -> u64 {
    crate::state::BULK_INSTALL_COUNT.load(std::sync::atomic::Ordering::Relaxed)
}

/// Test-only instrument: count of models in the Rust column registry (#246).
///
/// Mirrors `_bulk_install_count_for_test` — a single read at the registry
/// store so tests can assert provisional import leaves Rust empty until the
/// first bulk install.
#[pyfunction]
#[pyo3(name = "_rust_model_registry_count_for_test")]
pub fn _rust_model_registry_count_for_test() -> PyResult<usize> {
    let registry = MODEL_REGISTRY.read().map_err(|_| {
        pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Model Registry")
    })?;
    Ok(registry.len())
}

/// Test-only helper: clear the pushed SchemaIR modelset (and its recorded
/// fingerprint, so the gate can never match state the runtime no longer holds)
/// so the fail-loud path in `internal_create_tables` / `internal_migrate` can be
/// exercised from Python (a missing modelset must raise, never silently create
/// nothing).
#[pyfunction]
#[pyo3(name = "_clear_schema_ir_modelset_for_test")]
pub fn _clear_schema_ir_modelset_for_test() -> PyResult<()> {
    let mut modelset_guard = crate::state::SCHEMA_IR_MODELSET.write().map_err(|_| {
        pyo3::exceptions::PyRuntimeError::new_err("Failed to lock SchemaIR modelset")
    })?;
    let mut fingerprint_guard = crate::state::INSTALLED_FINGERPRINT.write().map_err(|_| {
        pyo3::exceptions::PyRuntimeError::new_err("Failed to lock installed fingerprint")
    })?;
    *modelset_guard = None;
    *fingerprint_guard = None;
    Ok(())
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

    fn plan_options(self) -> PlanOptions {
        PlanOptions {
            destructive: self.destructive,
        }
    }
}

fn parse_dialect(dialect: &str) -> PyResult<Dialect> {
    match dialect {
        "postgres" => Ok(Dialect::Postgres),
        "sqlite" => Ok(Dialect::Sqlite),
        other => Err(pyo3::exceptions::PyValueError::new_err(format!(
            "Unknown dialect {:?}; expected 'postgres' or 'sqlite'",
            other
        ))),
    }
}

fn emission_error(err: ferro_migrate::EmissionError) -> PyErr {
    pyo3::exceptions::PyValueError::new_err(err.message)
}

/// Prefix of the debug line auto-migrate logs (on the `ferro` logger) before
/// it executes each statement — the create pass's, every statement of a
/// table's reconciliation plan including its column drops, and each enum type
/// statement (logged against its type name) — so a run's exact DDL is
/// observable without a database-side statement log.
const RECONCILE_STATEMENT_LOG_PREFIX: &str = "Ferro Engine: auto-migrate executing on";

pub(crate) fn log_reconcile_statement(table_lower: &str, sql: &str) {
    crate::log_debug(format!(
        "{RECONCILE_STATEMENT_LOG_PREFIX} '{table_lower}': {sql}"
    ));
}

/// Map a column-drop execution failure to a `PyErr` with a consistent,
/// actionable message, on both dialects.
fn map_drop_column_error(table_lower: &str, col_name: &str, e: sqlx::Error) -> PyErr {
    crate::errors::map_db_error(
        &format!(
            "Cannot drop column '{}.{}' (columns referenced by constraints, foreign \
             keys, triggers, or views need a reviewed migration: `ferro migrate new`)",
            table_lower, col_name
        ),
        e,
    )
}

fn map_statement_error(table_lower: &str, sql: &str, e: sqlx::Error) -> PyErr {
    crate::errors::map_db_error(
        &format!(
            "Auto-migrate DDL failed for table '{}' (statement: {})",
            table_lower, sql
        ),
        e,
    )
}

/// Drop one column on SQLite, resolving its index dependencies first.
///
/// Explicit indexes covering the column are orphaned by its removal and are
/// dropped beforehand (SQLite refuses `DROP COLUMN` on an indexed column).
/// Constraint autoindexes cannot be dropped separately, so their presence is
/// a hard error, as is any remaining engine refusal (CHECK references,
/// triggers, views, inbound foreign keys). `drop_sql` is the op's rendering.
async fn execute_sqlite_drop_column(
    engine: &EngineHandle,
    table_lower: &str,
    col_name: &str,
    drop_sql: &str,
) -> PyResult<()> {
    let indexes = sqlite_indexes_covering_column(engine, table_lower, col_name).await?;
    if let Some(blocking) = indexes.iter().find(|index| index.origin != "c") {
        let constraint = match blocking.origin.as_str() {
            "u" => "a UNIQUE constraint",
            "pk" => "the PRIMARY KEY",
            _ => "a table constraint",
        };
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "Cannot drop column '{}.{}': it is enforced by {} ('{}'), which SQLite \
             cannot drop separately from the table definition. Generate a reviewed \
             migration with `ferro migrate new`.",
            table_lower, col_name, constraint, blocking.name
        )));
    }
    for index in &indexes {
        let sql = format!("DROP INDEX IF EXISTS {}", quote_ident(&index.name));
        log_reconcile_statement(table_lower, &sql);
        engine.execute_sql_unprepared(&sql).await.map_err(|e| {
            crate::errors::map_db_error(
                &format!(
                    "Auto-migrate failed dropping index '{}' (required to drop column \
                     '{}.{}')",
                    index.name, table_lower, col_name
                ),
                e,
            )
        })?;
    }

    log_reconcile_statement(table_lower, drop_sql);
    engine
        .execute_sql_unprepared(drop_sql)
        .await
        .map_err(|e| map_drop_column_error(table_lower, col_name, e))?;
    Ok(())
}

/// The actionable failure for one enum-type statement.
fn type_statement_error(op: &MigrationOp, e: sqlx::Error) -> PyErr {
    let context = match op {
        MigrationOp::AddEnumLabel { type_name, label } => {
            format!("Auto-migrate failed to add enum label '{label}' to type '{type_name}'")
        }
        MigrationOp::CreateEnumType { type_name, .. } => {
            format!("Auto-migrate failed to create enum type '{type_name}'")
        }
        MigrationOp::DropEnumType { type_name } => {
            format!("Auto-migrate failed to drop enum type '{type_name}'")
        }
        other => format!("Auto-migrate failed executing {other:?}"),
    };
    crate::errors::map_db_error(&context, e)
}

fn type_name_of(op: &MigrationOp) -> &str {
    match op {
        MigrationOp::AddEnumLabel { type_name, .. }
        | MigrationOp::CreateEnumType { type_name, .. }
        | MigrationOp::DropEnumType { type_name } => type_name,
        _ => "",
    }
}

/// Execute one table's rendered ops. On Postgres the whole group runs in one
/// transaction (FF-G G3): a mid-plan failure leaves the table exactly as it
/// was, so a failed run is safely re-runnable. SQLite runs statement at a
/// time, with each column drop going through its index-dependency path.
/// Returns how many statements ran and how many columns were dropped.
async fn execute_table_ops(
    engine: &EngineHandle,
    table: &str,
    ops: &[&RenderedOp],
    backend: Dialect,
) -> PyResult<(usize, usize)> {
    let statements: usize = ops.iter().map(|op| op.statements.len()).sum();
    let drops = ops
        .iter()
        .filter(|op| matches!(op.op, MigrationOp::DropColumn { .. }))
        .count();
    if statements == 0 {
        return Ok((0, 0));
    }

    if backend == Dialect::Sqlite {
        for op in ops {
            if let MigrationOp::DropColumn { column, .. } = &op.op {
                for sql in &op.statements {
                    execute_sqlite_drop_column(engine, table, column, sql).await?;
                }
                continue;
            }
            for sql in &op.statements {
                log_reconcile_statement(table, sql);
                engine
                    .execute_sql_unprepared(sql)
                    .await
                    .map_err(|e| map_statement_error(table, sql, e))?;
            }
        }
        return Ok((statements - drops, drops));
    }

    let mut conn = engine.begin_transaction_connection().await.map_err(|e| {
        crate::errors::map_db_error(
            &format!(
                "Auto-migrate failed to open a transaction for table '{}'",
                table
            ),
            e,
        )
    })?;
    let table_result: PyResult<()> = async {
        for op in ops {
            for sql in &op.statements {
                log_reconcile_statement(table, sql);
                conn.execute_sql_unprepared(sql)
                    .await
                    .map_err(|e| match &op.op {
                        MigrationOp::DropColumn { column, .. } => {
                            map_drop_column_error(table, column, e)
                        }
                        _ => map_statement_error(table, sql, e),
                    })?;
            }
        }
        Ok(())
    }
    .await;
    match table_result {
        Ok(()) => {
            conn.commit().await.map_err(|e| {
                crate::errors::map_db_error(
                    &format!("Auto-migrate failed to commit DDL for table '{}'", table),
                    e,
                )
            })?;
            Ok((statements - drops, drops))
        }
        Err(err) => {
            // Same disposal the create pass and settings delivery perform
            // (#416): a connection whose ROLLBACK failed may be
            // idle-in-transaction, and sqlx only pings on release, so it is
            // discarded rather than returned to the pool.
            if let Err(rollback_err) = conn.rollback().await {
                crate::log_debug(format!(
                    "⚠️ Ferro Engine: rollback after failed migration of '{}' also \
                     failed: {} — discarding the connection",
                    table, rollback_err
                ));
                let _ = conn.detach_and_close().await;
            }
            Err(err)
        }
    }
}

/// Run the full auto-migrate pass: create missing tables, then (per
/// `MigrateOptions`) reconcile existing tables with the registered models.
///
/// The reconciliation is one plan for the whole modelset: the live database
/// is read into an IR plus its live facts ([`live_schema_ir`]), the one
/// planner decides every change ([`plan_from_ir`]), [`render_plan`] renders
/// it, and this function executes it — enum type statements in autocommit
/// first (a label is committed before any table statement can name it), then
/// each table's ops in its own transaction on Postgres.
///
/// After any DDL executed, the engine pool is refreshed so no connection can
/// serve a statement prepared against the pre-DDL schema.
///
/// # Errors
/// Returns a `PyErr` if introspection, rendering, DDL execution, or the pool
/// refresh fails, or if the plan contains a change that cannot be applied
/// safely — rendering runs before anything executes, so such a plan executes
/// nothing.
pub async fn internal_migrate(engine: Arc<EngineHandle>, opts: MigrateOptions) -> PyResult<()> {
    let tables_before_create = internal_create_tables(engine.clone(), opts.updates).await?;
    if !opts.updates {
        return Ok(());
    }

    let modelset = {
        let guard = crate::state::SCHEMA_IR_MODELSET.read().map_err(|_| {
            pyo3::exceptions::PyRuntimeError::new_err("Failed to lock SchemaIR modelset")
        })?;
        guard.clone().ok_or_else(|| {
            pyo3::exceptions::PyRuntimeError::new_err(
                "SchemaIR modelset not set — connect()/migrate() must push it before migrating",
            )
        })?
    };
    let backend = engine.backend();

    // ADR-0010: the reconciliation pass owns tables that already existed. A
    // table the create pass built in this same run is already exactly the
    // model, so it is not read live: the plan sees it as an add, which the
    // create pass has executed.
    let mut existing: Vec<String> = modelset
        .payload
        .models
        .iter()
        .map(|model| model.table_name.clone())
        .filter(|table| tables_before_create.contains(table))
        .collect();
    existing.sort();
    let (live, facts) = live_schema_ir(&engine, Some(&existing)).await?;
    let plan = plan_from_ir(&live, &modelset, backend, &facts, opts.plan_options());
    let rendered = render_plan(&plan, &live, &modelset, backend).map_err(emission_error)?;

    if backend == Dialect::Postgres {
        // The migrator warning (#413; PRD #406 user story 19) is asked once,
        // before any table is touched: is the role running this migration
        // itself subject to the FORCE policies it is about to maintain? A
        // superuser or BYPASSRLS role is exempt and hears nothing.
        let forced_tables: Vec<String> = modelset
            .payload
            .models
            .iter()
            .filter(|model| {
                model
                    .row_security
                    .as_ref()
                    .is_some_and(|declaration| declaration.force)
                    && tables_before_create.contains(&model.table_name)
            })
            .map(|model| model.table_name.clone())
            .collect();
        if !forced_tables.is_empty()
            && !connected_role_bypasses_row_security(&engine).await?
            && let Some(warning) = row_security_migrator_warning(&forced_tables)
        {
            // Emitted HERE, not queued with the rest: it warns that the data
            // steps below may see zero rows, and a warning that arrives after
            // those steps have already run silently succeeded is no warning at
            // all (#413 gate).
            crate::emit_user_warning_always(&warning);
        }
    }

    let mut warnings = plan.warnings.clone();
    let mut ddl_ran = false;
    let mut index = 0;
    while index < rendered.len() {
        let current = &rendered[index];
        // An add is the create pass's, which has already run.
        if matches!(current.op, MigrationOp::AddTable { .. }) {
            index += 1;
            continue;
        }
        let Some(table) = current.op.table() else {
            // Enum type statements run in autocommit: `ALTER TYPE ... ADD
            // VALUE` is non-transactional before PG12 and its label is
            // unusable until commit on PG12+.
            for sql in &current.statements {
                log_reconcile_statement(type_name_of(&current.op), sql);
                engine
                    .execute_sql_unprepared(sql)
                    .await
                    .map_err(|e| type_statement_error(&current.op, e))?;
                ddl_ran = true;
            }
            warnings.extend(current.warnings.iter().cloned());
            index += 1;
            continue;
        };
        let group: Vec<&RenderedOp> = rendered[index..]
            .iter()
            .take_while(|op| op.op.table() == Some(table))
            .filter(|op| !matches!(op.op, MigrationOp::AddTable { .. }))
            .collect();
        let consumed = rendered[index..]
            .iter()
            .take_while(|op| op.op.table() == Some(table))
            .count();
        let (statements, dropped) = execute_table_ops(&engine, table, &group, backend).await?;
        if statements + dropped > 0 {
            ddl_ran = true;
            crate::log_debug(format!(
                "✅ Ferro Engine: Table '{}' migrated ({} statement(s), {} column(s) dropped)",
                table, statements, dropped
            ));
        }
        for op in &group {
            warnings.extend(op.warnings.iter().cloned());
        }
        index += consumed;
    }

    if ddl_ran {
        engine.refresh_pool().await.map_err(|e| {
            crate::errors::map_db_error(
                "Auto-migrate applied DDL but failed to refresh the connection pool",
                e,
            )
        })?;
    }

    for warning in &warnings {
        crate::emit_user_warning(warning);
    }
    // Row-security notes describe whether THIS connect left rows fenced, so
    // the warning registry must never quiet them down after the first boot.
    for warning in &plan.always_warnings {
        crate::emit_user_warning_always(warning);
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
/// On Postgres each table's plan runs in one transaction (a mid-plan failure
/// rolls that table back); SQLite applies statements one at a time.
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

fn parse_json_or_default<T: serde::de::DeserializeOwned + Default>(
    json: &str,
    what: &str,
) -> PyResult<T> {
    if json.is_empty() {
        return Ok(T::default());
    }
    serde_json::from_str(json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("Invalid {what} JSON: {e}")))
}

/// Test-only helper: plan and render the reconciliation of one table against
/// a JSON description of its live state, without a database. Returns
/// `(statements, warnings)` — every rendered statement in plan order, then
/// the planning warnings, the rendering warnings and the row-security
/// warnings. Column drops render as their plain `DROP COLUMN` (the SQLite
/// index-dependency handling needs a live database and is exercised by
/// integration tests).
///
/// # Errors
/// Returns a `PyErr` when the JSON cannot be parsed, the dialect is
/// unrecognized, or the plan contains an unsafe change.
#[pyfunction]
#[pyo3(name = "_render_migration_sql_for_test")]
#[pyo3(signature = (name, schema_ir_json, live_columns_json, dialect, updates=true, destructive=false, live_indexes_json=String::new(), live_foreign_keys_json=String::new(), live_checks_json=String::new(), live_row_security_json=String::new()))]
pub fn _render_migration_sql_for_test(
    name: String,
    schema_ir_json: String,
    live_columns_json: String,
    dialect: String,
    updates: bool,
    destructive: bool,
    live_indexes_json: String,
    live_foreign_keys_json: String,
    live_checks_json: String,
    live_row_security_json: String,
) -> PyResult<(Vec<String>, Vec<String>)> {
    let backend = parse_dialect(&dialect)?;
    let declared: IrEnvelope<SchemaIrPayload> =
        serde_json::from_str(&schema_ir_json).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("invalid schema_ir_json: {e}"))
        })?;
    let columns: Vec<LiveColumn> = serde_json::from_str(&live_columns_json).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("Invalid live-columns JSON: {}", e))
    })?;
    let table = LiveTable {
        name,
        columns,
        indexes: parse_json_or_default::<Vec<LiveIndex>>(&live_indexes_json, "live-indexes")?,
        foreign_keys: parse_json_or_default::<Vec<LiveForeignKey>>(
            &live_foreign_keys_json,
            "live-foreign-keys",
        )?,
        checks: parse_json_or_default::<Vec<LiveCheck>>(&live_checks_json, "live-checks")?,
        row_security: parse_json_or_default::<LiveRowSecurity>(
            &live_row_security_json,
            "live-row-security",
        )?,
    };

    let opts = MigrateOptions::laddered(updates, destructive);
    if !opts.updates {
        return Ok((Vec::new(), Vec::new()));
    }
    let (live, facts) = live_tables_to_schema_ir(vec![table], Default::default(), backend);
    let plan = plan_from_ir(&live, &declared, backend, &facts, opts.plan_options());
    let rendered = render_plan(&plan, &live, &declared, backend).map_err(emission_error)?;

    let mut statements = Vec::new();
    let mut warnings = plan.warnings;
    for op in rendered {
        statements.extend(op.statements);
        warnings.extend(op.warnings);
    }
    warnings.extend(plan.always_warnings);
    Ok((statements, warnings))
}

fn parse_schema_envelope(json: &str, what: &str) -> PyResult<IrEnvelope<SchemaIrPayload>> {
    let envelope: IrEnvelope<SchemaIrPayload> = serde_json::from_str(json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("invalid {what}: {e}")))?;
    if envelope.ir_kind != "schema" {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "{what}: expected ir_kind 'schema', got '{}'",
            envelope.ir_kind
        )));
    }
    Ok(envelope)
}

/// The one planner over FFI: every change that turns the `old_ir_json`
/// snapshot into `new_ir_json` on `dialect`, as JSON.
///
/// `options_json` is `{"destructive": bool}`. `facts_json` is the live
/// side-table `_live_schema_ir` returns beside a live envelope; omitted, the
/// old snapshot is read as declared (`LiveFacts::declared`). The result is
/// `{"operations": [{"kind": …, <op fields>}], "warnings": […],
/// "always_warnings": […]}`; with `render`, each op also carries the
/// `statements` and `warnings` it renders to.
///
/// # Errors
/// `ValueError` when a JSON argument is malformed, an envelope is not a
/// `schema` IR, the dialect is unknown, or an op cannot render.
#[pyfunction]
#[pyo3(name = "_plan_from_ir")]
#[pyo3(signature = (old_ir_json, new_ir_json, dialect, options_json, render=false, facts_json=None))]
pub fn _plan_from_ir(
    old_ir_json: String,
    new_ir_json: String,
    dialect: String,
    options_json: String,
    render: bool,
    facts_json: Option<String>,
) -> PyResult<String> {
    let backend = parse_dialect(&dialect)?;
    let old = parse_schema_envelope(&old_ir_json, "old_ir_json")?;
    let new = parse_schema_envelope(&new_ir_json, "new_ir_json")?;
    let options: PlanOptions = serde_json::from_str(&options_json).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("invalid options_json: {e}"))
    })?;
    let facts: LiveFacts = match facts_json {
        Some(json) => serde_json::from_str(&json).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("invalid facts_json: {e}"))
        })?,
        None => LiveFacts::declared(),
    };
    validate_schema_ir(&old).map_err(emission_error)?;
    validate_schema_ir(&new).map_err(emission_error)?;

    let plan = plan_from_ir(&old, &new, backend, &facts, options);
    let to_value = |value: serde_json::Result<serde_json::Value>| {
        value.map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("could not serialize the plan: {e}"))
        })
    };
    let operations: Vec<serde_json::Value> = if render {
        render_plan(&plan, &old, &new, backend)
            .map_err(emission_error)?
            .into_iter()
            .map(|rendered| {
                let mut op = to_value(serde_json::to_value(&rendered.op))?;
                if let Some(fields) = op.as_object_mut() {
                    fields.insert("statements".into(), rendered.statements.into());
                    fields.insert("warnings".into(), rendered.warnings.into());
                }
                Ok(op)
            })
            .collect::<PyResult<_>>()?
    } else {
        plan.operations
            .iter()
            .map(|op| to_value(serde_json::to_value(op)))
            .collect::<PyResult<_>>()?
    };
    let out = serde_json::json!({
        "operations": operations,
        "warnings": plan.warnings,
        "always_warnings": plan.always_warnings,
    });
    Ok(out.to_string())
}
