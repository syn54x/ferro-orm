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
//! modelset, and the plan renders itself (`Plan::render`) through the same
//! `ferro_ddl_lowering` functions every migration door uses (AGENTS.md § I-1).

use crate::backend::{EngineBindValue, EngineHandle};
use crate::ddl_exec::{DdlError, DdlExecutor, Door, Executed, Failed, Role, Unit};
use crate::introspect::{
    LiveCheck, LiveColumn, LiveForeignKey, LiveIndex, connected_role_bypasses_row_security,
    live_table_checks, live_table_columns, quote_ident, sqlite_indexes_covering_column,
};
use crate::live_ir::{
    LiveTable, live_schema_ir, live_table_renames, live_tables_to_schema_ir, tables_to_read,
};
use crate::run::{
    FORMAT_TABLE, RunLock, TRACKING_TABLE, governed_schema, refused, tracking_tables_for,
};
use crate::schema::internal_create_tables;
use crate::state::{MODEL_REGISTRY, engine_for_connection};
use ferro_ddl_lowering::{
    Dialect, LiveRowSecurity, ddl_lock_retry_warning, row_security_migrator_warning,
    run_lock_wait_warning,
};
use ferro_migrate::{
    LiveFacts, MigrationOp, Plan, PlanOptions, RenderedOp, Report, ReportKind, Side, Subject,
    plan_down, plan_from_ir, validate_schema_ir,
};
use ferro_schema_ir::{IrEnvelope, SchemaIrPayload};
use pyo3::prelude::*;
use std::collections::{BTreeSet, HashSet};
use std::sync::Arc;
use std::time::Duration;

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

/// `ddl_lock_timeout`'s default (ADR-0044), for a project with no config.
pub const DEFAULT_DDL_LOCK_TIMEOUT: Duration = Duration::from_secs(5);

/// Which migration behaviors beyond table creation are enabled.
#[derive(Clone, Copy, Debug)]
pub struct MigrateOptions {
    /// Add missing model columns to existing tables; on Postgres, also
    /// reconcile column type and nullability drift.
    pub updates: bool,
    /// Drop live columns that no longer exist on the model. Implies `updates`.
    pub destructive: bool,
    /// How long each statement of the create and reconciliation passes
    /// waits for a table lock on Postgres before its unit is retried
    /// (ADR-0044); `None` waits without limit.
    pub ddl_lock_timeout: Option<Duration>,
}

impl MigrateOptions {
    /// Apply the flag ladder: `destructive` ⇒ `updates`; the DDL lock
    /// timeout at its default.
    pub fn laddered(updates: bool, destructive: bool) -> Self {
        Self {
            updates: updates || destructive,
            destructive,
            ddl_lock_timeout: Some(DEFAULT_DDL_LOCK_TIMEOUT),
        }
    }

    /// The same options under the project's `ddl_lock_timeout`, in seconds
    /// as Python reads it from `FerroSettings` (`0` disables).
    ///
    /// # Errors
    /// `ValueError` for a negative or non-finite number.
    pub fn with_ddl_lock_timeout_seconds(self, seconds: f64) -> PyResult<Self> {
        Ok(Self {
            ddl_lock_timeout: DdlExecutor::from_seconds(seconds)?.timeout,
            ..self
        })
    }

    fn plan_options(self) -> PlanOptions {
        PlanOptions {
            destructive: self.destructive,
        }
    }
}

pub(crate) fn parse_dialect(dialect: &str) -> PyResult<Dialect> {
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

fn plan_error(err: ferro_migrate::PlanError) -> PyErr {
    pyo3::exceptions::PyValueError::new_err(err.to_string())
}

/// What one auto-migrate pass did (ADR-0049): every statement it sent to the
/// database, in execution order across the create pass, the type statements
/// and the reconciliation, and every warning it raised, in the order raised.
/// `ferro.migrate()` and `ferro.create_tables()` return it as
/// `ferro.PassReport`; `connect()` builds it and logs from it.
///
/// It is filled from what the DDL executor ran ([`Executed`]), never rebuilt
/// from the plan, so it cannot describe a statement that did not execute. A
/// pass that fails partway carries it on its error ([`with_pass_report`]):
/// what committed before the failure, the failing statement left out.
#[derive(Debug, Default)]
pub struct PassReport {
    /// Every statement sent, with its subject and role.
    pub executed: Executed,
    /// Every warning raised.
    pub warnings: Vec<Report>,
}

impl PassReport {
    /// Raise `report` as a Python warning ([`emit_report`]) and list it.
    pub(crate) fn warn(&mut self, report: Report) {
        emit_report(&report);
        self.warnings.push(report);
    }

    /// List what a unit run under the executor sent: all of it, or what it
    /// committed before it failed. The failure is handed back.
    pub(crate) fn unit(
        &mut self,
        result: Result<Executed, DdlError<Failed>>,
    ) -> Result<(), DdlError<Failed>> {
        match result {
            Ok(executed) => {
                self.executed.statements.extend(executed.statements);
                Ok(())
            }
            Err(mut err) => {
                self.executed
                    .statements
                    .extend(err.take_committed().statements);
                Err(err)
            }
        }
    }

    /// The wire form: `{"statements": [{"subject", "sql", "role"}],
    /// "warnings": [{"kind", "subject", "text", "recurs", "blocks"}]}`.
    ///
    /// # Errors
    /// `RuntimeError` if it cannot be serialized.
    pub fn to_json(&self) -> PyResult<String> {
        serde_json::to_string(&serde_json::json!({
            "statements": self.executed.statements,
            "warnings": self.warnings,
        }))
        .map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!(
                "could not serialize the pass report: {e}"
            ))
        })
    }

    /// One debug line summing the pass up, for `connect()`, which returns
    /// nothing: every statement already logged its own line as it ran.
    pub(crate) fn log_summary(&self) {
        let ran = |role| {
            self.executed
                .statements
                .iter()
                .filter(|statement| statement.role == role)
                .count()
        };
        crate::log_debug(format!(
            "ferro auto-migrate: {} schema statement(s), {} warning(s)",
            ran(Role::Schema),
            self.warnings.len()
        ));
    }
}

/// The attribute a failed pass's error carries its report on, as JSON; the
/// Python door reads it into the error's `.report`.
pub const PASS_REPORT_ATTR: &str = "_ferro_pass_report";

/// `err`, carrying `report` (what committed before the failure) for the
/// Python door to expose as `.report`.
pub(crate) fn with_pass_report(err: PyErr, report: &PassReport) -> PyErr {
    let json = match report.to_json() {
        Ok(json) => json,
        Err(serialize) => return serialize,
    };
    Python::attach(|py| match err.value(py).setattr(PASS_REPORT_ATTR, json) {
        Ok(()) => err,
        Err(attach) => attach,
    })
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

/// The warning the create and reconciliation passes raise for each attempt
/// that timed out waiting for a lock: `migrating 'author': waiting for a lock
/// on "author" (attempt 1 of 10, retry in 1s)` ([`ReportKind::DdlLockRetry`]).
pub(crate) fn pass_attempt_warning(subject: Subject, attempt: &crate::ddl_exec::Attempt) -> Report {
    ddl_lock_retry_warning(subject, &attempt.describe())
}

/// The error for a create or reconciliation unit that timed out on every
/// attempt: `OperationalError` naming `ddl_lock_timeout`.
pub(crate) fn pass_lock_timeout_error(
    subject: &str,
    timeout: &crate::ddl_exec::DdlLockTimeout,
) -> PyErr {
    crate::ddl_exec::lock_timeout_error(&format!(
        "Auto-migrate DDL failed for '{subject}': {timeout}"
    ))
}

/// The unique index `op` builds, as `(name, declared columns)`: an added,
/// redefined (ADR-0051) or rebuilt unique, whose build duplicates refuse.
fn unique_build_of(
    op: &MigrationOp,
    declared: &IrEnvelope<SchemaIrPayload>,
) -> Option<(String, Vec<String>)> {
    match op {
        MigrationOp::AddIndex {
            table,
            name,
            unique: true,
            ..
        }
        | MigrationOp::RedefineIndex { table, name }
        | MigrationOp::RebuildIndex {
            table,
            name,
            unique: true,
            ..
        } => match ferro_migrate::declared_index(declared, table, name) {
            Some((columns, true)) => Some((name.clone(), columns)),
            _ => None,
        },
        _ => None,
    }
}

/// The error for `sql` of `op` on `table` failing with `e`: a unique build
/// refused by duplicates is counted and names the fix; anything else is the
/// statement's own failure.
async fn map_op_statement_error(
    engine: &EngineHandle,
    table: &str,
    op: &MigrationOp,
    declared: &IrEnvelope<SchemaIrPayload>,
    sql: &str,
    e: sqlx::Error,
) -> PyErr {
    match unique_build_of(op, declared) {
        Some((index, columns)) if crate::errors::is_unique_violation(&e) => {
            crate::errors::pass_unique_build_failure(engine, table, &index, &columns, e).await
        }
        _ => map_statement_error(table, sql, e),
    }
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
    executed: &mut Executed,
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
        executed
            .send_on(engine, Door::Pass(table_lower), Role::Schema, &sql)
            .await
            .map_err(|e| {
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

    executed
        .send_on(engine, Door::Pass(table_lower), Role::Schema, drop_sql)
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

/// The enum type an op without a table changes: the subject its statements
/// are reported under. A type rename is reported under its new name.
fn type_name_of(op: &MigrationOp) -> &str {
    match op {
        MigrationOp::AddEnumLabel { type_name, .. }
        | MigrationOp::CreateEnumType { type_name, .. }
        | MigrationOp::DropEnumType { type_name }
        | MigrationOp::RenameEnumLabel { type_name, .. }
        | MigrationOp::RemoveEnumLabel { type_name, .. } => type_name,
        MigrationOp::RenameEnumType { new, .. } => new,
        _ => "",
    }
}

/// Execute one table's rendered ops. On Postgres the whole group runs in one
/// transaction (FF-G G3) under the DDL lock timeout (ADR-0044): `SET LOCAL
/// lock_timeout` first, and a statement that times out waiting for a lock
/// rolls the group back and runs it again from the top, up to ten attempts.
/// A mid-plan failure leaves the table exactly as it was, so a failed run is
/// safely re-runnable. SQLite runs statement at a time, with each column
/// drop going through its index-dependency path, and sets no timeout.
/// What ran (or committed before a failure) goes into `report`. Returns how
/// many statements ran and how many columns were dropped.
async fn execute_table_ops(
    engine: &EngineHandle,
    table: &str,
    ops: &[&RenderedOp],
    declared: &IrEnvelope<SchemaIrPayload>,
    backend: Dialect,
    ddl: &DdlExecutor,
    report: &mut PassReport,
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
        execute_sqlite_table_ops(engine, table, ops, declared, &mut report.executed).await?;
        return Ok((statements - drops, drops));
    }

    // The executor owns the transaction: BEGIN, `SET LOCAL lock_timeout`,
    // this group, COMMIT; on a failure ROLLBACK, and a connection whose
    // ROLLBACK failed is discarded rather than returned to the pool (#416).
    // A failing statement is found by its place in the group, and so is the
    // op it renders.
    let ops_statements: Vec<(&RenderedOp, &String)> = ops
        .iter()
        .flat_map(|op| op.statements.iter().map(move |sql| (*op, sql)))
        .collect();
    let sqls: Vec<&String> = ops_statements.iter().map(|(_, sql)| *sql).collect();
    let result = ddl
        .run(
            engine,
            Unit::Transactional,
            Door::Pass(table),
            &sqls,
            |attempt| report.warn(pass_attempt_warning(Subject::table(table), &attempt)),
            None,
        )
        .await;
    match report.unit(result) {
        Ok(()) => Ok((statements - drops, drops)),
        Err(DdlError::LockTimeout(timeout)) => Err(pass_lock_timeout_error(table, &timeout)),
        Err(DdlError::Failed(Failed { index, error, .. })) => {
            Err(match index.and_then(|index| ops_statements.get(index)) {
                Some((
                    RenderedOp {
                        op: MigrationOp::DropColumn { column, .. },
                        ..
                    },
                    _,
                )) => map_drop_column_error(table, column, error),
                Some((op, sql)) => {
                    map_op_statement_error(engine, table, &op.op, declared, sql, error).await
                }
                None => crate::errors::map_db_error(
                    &format!("Auto-migrate failed to apply DDL to table '{table}'"),
                    error,
                ),
            })
        }
    }
}

/// One table's rendered ops on SQLite, outside the executor on purpose:
/// statement at a time (SQLite sets no lock timeout and retries nothing),
/// each column drop through its index-dependency path, which reads the
/// catalog between statements ([`execute_sqlite_drop_column`]). Each
/// statement is recorded in `executed` as it commits, so a failure leaves
/// the ones ahead of it there.
async fn execute_sqlite_table_ops(
    engine: &EngineHandle,
    table: &str,
    ops: &[&RenderedOp],
    declared: &IrEnvelope<SchemaIrPayload>,
    executed: &mut Executed,
) -> PyResult<()> {
    for op in ops {
        if let MigrationOp::DropColumn { column, .. } = &op.op {
            for sql in &op.statements {
                execute_sqlite_drop_column(engine, executed, table, column, sql).await?;
            }
            continue;
        }
        for sql in &op.statements {
            if let Err(e) = executed
                .send_on(engine, Door::Pass(table), Role::Schema, sql)
                .await
            {
                return Err(map_op_statement_error(engine, table, &op.op, declared, sql, e).await);
            }
        }
    }
    Ok(())
}

/// How long an auto-migrate pass waits for the run lock. The wait has no
/// practical bound on purpose: the lock is held only by a live run (the
/// database or the operating system releases a dead one), and a boot that
/// gave up while another boot or a `ferro migrate up` was mid-pass would
/// fail a start that is about to succeed. One year is "until it is free"
/// while staying far inside `Instant`'s range.
const AUTO_MIGRATE_LOCK_WAIT: Duration = Duration::from_secs(60 * 60 * 24 * 365);

/// Which public call is running the auto-migrate passes: the waiting
/// warning names it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AutoMigrateDoor {
    /// `connect()` with an auto-migrate flag.
    Connect,
    /// `ferro.create_tables()`.
    CreateTables,
    /// `ferro.migrate()`.
    Migrate,
}

impl AutoMigrateDoor {
    pub(crate) fn call(self) -> &'static str {
        match self {
            AutoMigrateDoor::Connect => "connect(auto_migrate=…)",
            AutoMigrateDoor::CreateTables => "create_tables()",
            AutoMigrateDoor::Migrate => "migrate()",
        }
    }
}

/// The refusal for an auto-migrate flag on a database ferro migrations
/// governs (ADR-0038): `governed` is the schema the pass would change,
/// `home` the schema holding its tracking table.
pub fn tracked_schema_refusal(governed: &str, home: &str) -> String {
    format!(
        "connect(auto_migrate=…) is refused: {governed} is governed by ferro migrations \
         ({home}.{TRACKING_TABLE}). Use ferro migrate up, or drop the tracking tables to \
         leave migrations."
    )
}

/// The refusal for declared tables whose names something other than a base
/// table holds live (a view, a virtual table, …): `CREATE TABLE IF NOT
/// EXISTS` would skip each in silence and leave the model without a table.
/// `held` is `(model identity, table name, holder)` per table, in create
/// order; a many-to-many join table's identity is its table name, and its
/// line says so without naming a `__ferro_table__` it has no class for.
///
/// ```text
/// Table creation is refused: a declared table's name is held by something that is not a table, so CREATE TABLE would skip it and leave the model without one.
///   "card" is a view: rename or drop the view, or declare a different __ferro_table__ on app.models.Card.
/// Nothing was created.
/// ```
pub fn table_name_held_refusal(
    held: &[(&str, &str, crate::introspect::NonTableHolder)],
) -> String {
    let mut lines = vec![
        "Table creation is refused: a declared table's name is held by something that is \
         not a table, so CREATE TABLE would skip it and leave the model without one."
            .to_string(),
    ];
    for (model, table, holder) in held {
        let rename = if model == table {
            "or declare the table under a different name".to_string()
        } else {
            format!("or declare a different __ferro_table__ on {model}")
        };
        lines.push(format!(
            "  \"{table}\" is {}: rename or drop the {}, {rename}.",
            holder.with_article(),
            holder.noun()
        ));
    }
    lines.push("Nothing was created.".to_string());
    lines.join("\n")
}

/// Every table named like a tracking table, as `(schema, table)`: SQLite's
/// are in `main`.
async fn tracking_named_tables(engine: &EngineHandle) -> PyResult<HashSet<(String, String)>> {
    let names = [
        EngineBindValue::String(TRACKING_TABLE.to_string()),
        EngineBindValue::String(FORMAT_TABLE.to_string()),
    ];
    let (sql, schema_column) = match engine.backend() {
        Dialect::Sqlite => (
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN (?, ?)",
            None,
        ),
        Dialect::Postgres => (
            "SELECT table_name::text, table_schema::text FROM information_schema.tables \
             WHERE table_name IN ($1, $2)",
            Some(1),
        ),
    };
    let rows = engine
        .fetch_all_sql_unprepared_with_binds(sql, &names)
        .await
        .map_err(|e| crate::errors::map_db_error("auto-migrate reading the catalog", e))?;
    let text = |row: &crate::backend::EngineRow, index: usize| match row.values.get(index) {
        Some((_, crate::backend::EngineValue::String(value))) => Some(value.clone()),
        _ => None,
    };
    Ok(rows
        .iter()
        .filter_map(|row| {
            let table = text(row, 0)?;
            let schema = match schema_column {
                Some(index) => text(row, index)?,
                None => "main".to_string(),
            };
            Some((schema, table))
        })
        .collect())
}

/// Refuse when ferro migrations govern the schema auto-migrate is about to
/// change (ADR-0038), before any DDL. Two sources, either one refuses:
///
/// - **where the project keeps its tracking tables**: a tracking table in
///   the connection's current schema, or a `_ferro_migrations` without its
///   format table in a configured `tracking_schema` (`settings_schemas`,
///   from `FerroSettings`), whose governed schema cannot be read;
/// - **the catalog**: every format table whose `governed_schema` is the
///   current schema, wherever it sits. This one holds in an image built
///   without its config file.
///
/// A neighbour schema governed by its own migrations is not this schema's
/// business and refuses nothing.
///
/// # Errors
/// `RunRefused` with [`tracked_schema_refusal`]'s text; a database error.
pub async fn guard_tracked_schema(
    engine: &EngineHandle,
    settings_schemas: &[String],
) -> PyResult<()> {
    let governed = governed_schema(engine).await?;
    let present = tracking_named_tables(engine).await?;
    let has =
        |schema: &str, table: &str| present.contains(&(schema.to_string(), table.to_string()));
    if has(&governed, TRACKING_TABLE) || has(&governed, FORMAT_TABLE) {
        return Err(refused(tracked_schema_refusal(&governed, &governed)));
    }
    if engine.backend() == Dialect::Postgres {
        for schema in settings_schemas {
            if has(schema, TRACKING_TABLE) && !has(schema, FORMAT_TABLE) {
                return Err(refused(tracked_schema_refusal(&governed, schema)));
            }
        }
    }
    if let Some(table) = tracking_tables_for(engine, Some(&governed)).await?.first() {
        return Err(refused(tracked_schema_refusal(&governed, &table.schema)));
    }
    Ok(())
}

/// Run the full auto-migrate pass under the run lock (ADR-0038): take the
/// lock a migration run takes, refuse a schema ferro migrations govern
/// ([`guard_tracked_schema`]), then create missing tables and (per
/// `MigrateOptions`) reconcile existing ones. Two processes booting
/// together serialize here, and the second sees the first's DDL. The lock
/// is released on every exit path.
///
/// `door` names the public call for the waiting warning
/// ([`run_lock_wait_warning`]). What the passes execute and warn goes into
/// `report`, failure or not.
///
/// # Errors
/// The guard's refusal; the pooler refusal behind a transaction-mode
/// pooler; whatever the passes raise.
pub async fn internal_migrate(
    engine: Arc<EngineHandle>,
    opts: MigrateOptions,
    tracking_schemas: &[String],
    door: AutoMigrateDoor,
    report: &mut PassReport,
) -> PyResult<()> {
    let lock = RunLock::acquire(&engine, None, AUTO_MIGRATE_LOCK_WAIT, |_| {
        report.warn(run_lock_wait_warning(door.call()));
    })
    .await?;
    let outcome = async {
        guard_tracked_schema(&engine, tracking_schemas).await?;
        run_passes(engine.clone(), opts, report).await
    }
    .await;
    let released = lock.release().await;
    outcome.and(released)
}

/// [`internal_migrate`] as a Python door returns it: the report's wire form,
/// or the pass's error carrying what committed before it
/// ([`with_pass_report`]).
///
/// # Errors
/// The pass's error, with its report attached.
pub async fn run_pass_for_python(
    engine: Arc<EngineHandle>,
    opts: MigrateOptions,
    tracking_schemas: &[String],
    door: AutoMigrateDoor,
) -> PyResult<String> {
    let mut report = PassReport::default();
    match internal_migrate(engine, opts, tracking_schemas, door, &mut report).await {
        Ok(()) => report.to_json(),
        Err(err) => Err(with_pass_report(err, &report)),
    }
}

/// The create pass, then (per `MigrateOptions`) the reconciliation of
/// existing tables with the registered models.
///
/// The reconciliation is one plan for the whole modelset: the live database
/// is read into an IR plus its live facts ([`live_schema_ir`]), the one
/// planner decides every change ([`plan_from_ir`]), the plan renders itself
/// against the sides it was planned between, and this function executes it — enum type statements in autocommit
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
async fn run_passes(
    engine: Arc<EngineHandle>,
    opts: MigrateOptions,
    report: &mut PassReport,
) -> PyResult<()> {
    // One lock-timeout policy for every DDL statement of both passes (ADR-0044).
    let ddl = DdlExecutor::new(opts.ddl_lock_timeout);
    let created = internal_create_tables(engine.clone(), opts.updates, &ddl, report).await?;
    let tables_before_create = &created.existing;
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
    if !opts.updates {
        // The create pass stays silent about drift (ADR-0011, ADR-0047): a
        // label rename's stranded rows are `migrate_updates`'s to report, so
        // a plain connect reads no row for a hint.
        return Ok(());
    }

    // ADR-0010: the reconciliation pass owns tables that already existed. A
    // table the create pass built in this same run is already exactly the
    // model, so it is not read live: the plan sees it as an add, which the
    // create pass has executed. The old table of a live table rename hint
    // (ADR-0032) is read too, so the planner renames it; a refused hint
    // renames nothing, reads nothing more, and leaves every table the create
    // pass held back for it uncreated, the planner's refusal saying why.
    // Which tables are read is the one rule every door reads by
    // ([`tables_to_read`], ADR-0047).
    let live_names: BTreeSet<String> = tables_before_create.iter().cloned().collect();
    let (renames, adds_after_renames) = match live_table_renames(&live_names, &modelset.payload) {
        Ok(renames) => (renames, created.held_back.clone()),
        Err(_) => (Vec::new(), BTreeSet::new()),
    };
    let built: Vec<String> = modelset
        .payload
        .models
        .iter()
        .map(|model| model.table_name.clone())
        .filter(|table| !tables_before_create.contains(table) && !created.held_back.contains(table))
        .collect();
    let existing = tables_to_read(&engine, &modelset, &built, &[]).await?;
    // The tables this pass reconciles, by their declared names: the renamed
    // ones under their new name.
    let reconciled: HashSet<&str> = modelset
        .payload
        .models
        .iter()
        .map(|model| model.table_name.as_str())
        .filter(|table| {
            tables_before_create.contains(*table) || renames.iter().any(|(_, new)| new == table)
        })
        .collect();
    let (live, facts) = live_schema_ir(&engine, &existing).await?;
    let plan = plan_from_ir(
        &Side::live(live, facts).map_err(plan_error)?,
        &Side::declared(modelset.clone()),
        backend,
        opts.plan_options(),
    );
    let rendered = plan.render().map_err(emission_error)?;

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
                    && reconciled.contains(model.table_name.as_str())
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
            report.warn(warning);
        }
    }

    // The plan's one-off reports and every rendering's are said once, after
    // the DDL; its recurring ones (every row-security note, a refused hint)
    // last, every time.
    let (recurring, mut reports): (Vec<&Report>, Vec<&Report>) =
        plan.reports.iter().partition(|report| report.recurs);
    let mut ddl_ran = false;
    let mut index = 0;
    while index < rendered.len() {
        let current = &rendered[index];
        // An add is the create pass's, which has already run — except a
        // table it held back for a rename, which is created here, after the
        // renames it waits on.
        if is_create_pass_add(&current.op, &adds_after_renames) {
            index += 1;
            continue;
        }
        let Some(table) = current.op.table() else {
            // Enum type statements run in autocommit: `ALTER TYPE ... ADD
            // VALUE` is non-transactional before PG12 and its label is
            // unusable until commit on PG12+. On their own connection, under
            // `SET lock_timeout` / `RESET lock_timeout` (ADR-0044).
            if !current.statements.is_empty() {
                let subject = type_name_of(&current.op);
                let result = ddl
                    .run(
                        &engine,
                        Unit::Unwrapped,
                        Door::Pass(subject),
                        &current.statements,
                        |attempt| {
                            report.warn(pass_attempt_warning(
                                Subject::enum_type(subject),
                                &attempt,
                            ))
                        },
                        None,
                    )
                    .await;
                report.unit(result).map_err(|err| match err {
                    DdlError::LockTimeout(timeout) => pass_lock_timeout_error(subject, &timeout),
                    DdlError::Failed(failure) => type_statement_error(&current.op, failure.error),
                })?;
                ddl_ran = true;
            }
            reports.extend(&current.reports);
            index += 1;
            continue;
        };
        let group: Vec<&RenderedOp> = rendered[index..]
            .iter()
            .take_while(|op| op.op.table() == Some(table))
            .filter(|op| !is_create_pass_add(&op.op, &adds_after_renames))
            .collect();
        let consumed = rendered[index..]
            .iter()
            .take_while(|op| op.op.table() == Some(table))
            .count();
        let (statements, dropped) =
            execute_table_ops(&engine, table, &group, &modelset, backend, &ddl, report).await?;
        if statements + dropped > 0 {
            ddl_ran = true;
            crate::log_debug(format!(
                "✅ Ferro Engine: Table '{}' migrated ({} statement(s), {} column(s) dropped)",
                table, statements, dropped
            ));
        }
        for op in &group {
            reports.extend(&op.reports);
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

    for warning in reports {
        report.warn(warning.clone());
    }
    // After the renames ran, so each table and column is read by its
    // declared name. A refused hint renames nothing and is the planner's to
    // report, so no row is read for one.
    let reconciled: HashSet<String> = reconciled.into_iter().map(str::to_string).collect();
    let hint_refused = plan
        .reports
        .iter()
        .any(|warning| matches!(warning.kind, ReportKind::HintRefused(_)));
    if !hint_refused {
        for warning in
            stranded_label_warnings(&engine, &modelset, &reconciled, &mut report.executed).await?
        {
            report.warn(warning);
        }
    }
    // Row-security notes describe whether THIS connect left rows fenced, so
    // the warning registry must never quiet them down after the first boot.
    for warning in recurring {
        report.warn(warning.clone());
    }

    Ok(())
}

/// Say `report` as a Python warning: every time when it [`Report::recurs`]
/// (a standing condition the registry must never quiet after the first
/// run), once otherwise.
pub(crate) fn emit_report(report: &Report) {
    if report.recurs {
        crate::emit_user_warning_always(&report.text);
    } else {
        crate::emit_user_warning(&report.text);
    }
}

/// The warnings for the live label renames (`__ferro_renamed_labels__`) on
/// SQLite, where every enum keeps its labels as text in the rows and the live
/// side carries none, so the planner sees no label to rename. A hint is live
/// (ADR-0032) while the database still holds its old label. A column with a
/// `db_check` answers from the check alone, since it bounds every row: it
/// lists the old label (live) or does not (inert), and no row is read. Only
/// a column with no check is probed (`render_label_held_probe`, recorded in
/// `executed` as [`Role::Probe`]). Called under `migrate_updates` only: the
/// probe reads the whole column once nothing matches, which a plain connect
/// must never pay for a hint that may stay (ADR-0047, ADR-0011, ADR-0032).
/// The pass changes the schema, never rows (ADR-0014), so a live hint is one
/// warning naming `ferro migrate new`, whose migration relabels the rows; an
/// inert one is silent. A refused hint is the planner's to report
/// ([`ReportKind::HintRefused`]); the caller reads none then.
///
/// `existing` are the tables that stood before this connect's create pass, as
/// they stand now; a declared column they lack (its rename still pending) is
/// not read.
async fn stranded_label_warnings(
    engine: &EngineHandle,
    modelset: &IrEnvelope<SchemaIrPayload>,
    existing: &HashSet<String>,
    executed: &mut Executed,
) -> PyResult<Vec<Report>> {
    use ferro_ddl_lowering::{
        check_lists_label, db_check_constraint_name, stranded_label_rename_warning,
    };
    let mut warnings = Vec::new();
    if engine.backend() != Dialect::Sqlite {
        return Ok(warnings);
    }
    for model in &modelset.payload.models {
        let table = model.table_name.as_str();
        let hinted: Vec<_> = model
            .columns
            .iter()
            .filter_map(|col| Some((col, col.enum_renamed_labels.as_ref()?)))
            .filter(|(_, renamed)| !renamed.labels.is_empty())
            .collect();
        if hinted.is_empty() || !existing.contains(table) {
            continue;
        }
        let live_columns = live_table_columns(engine, table).await?.unwrap_or_default();
        let checks = live_table_checks(engine, table).await?;
        for (col, renamed) in hinted {
            if !live_columns.iter().any(|live| live.name == col.name) {
                continue;
            }
            let check_name = db_check_constraint_name(table, &col.name);
            let check = checks.iter().find(|check| check.name == check_name);
            for (new, old) in &renamed.labels {
                // The check bounds every row, so it answers alone: no row is
                // read for a checked column.
                let held = match check {
                    Some(check) => check_lists_label(&check.definition, old),
                    None => {
                        let probe =
                            ferro_ddl_lowering::render_label_held_probe(table, &col.name, old);
                        !executed
                            .probe_on(engine, Door::Pass(table), &probe)
                            .await
                            .map_err(|e| {
                                crate::errors::map_db_error(
                                    &format!(
                                        "Auto-migrate failed reading rows of '{table}' for a \
                                         label rename hint"
                                    ),
                                    e,
                                )
                            })?
                            .is_empty()
                    }
                };
                if held {
                    warnings.push(stranded_label_rename_warning(table, &col.name, old, new));
                }
            }
        }
    }
    Ok(warnings)
}

/// An `AddTable` the create pass executed: every add but those of the tables
/// it held back for a rename (`after_renames`).
fn is_create_pass_add(op: &MigrationOp, after_renames: &BTreeSet<String>) -> bool {
    matches!(op, MigrationOp::AddTable { table } if !after_renames.contains(table))
}

/// Manually run the auto-migrate pass against a connected engine.
///
/// Mirrors `connect(auto_migrate=True, migrate_updates=..., migrate_destructive=...)`
/// for consumers that want explicit control over when DDL runs. `updates`
/// defaults to true — calling `migrate()` and getting create-only behavior
/// would be surprising; use `create_tables()` for that. The reconciliation
/// runs under the project's `ddl_lock_timeout` (`ddl_lock_timeout_s`
/// seconds, read by `ferro.migrate` from `FerroSettings`; `0` disables).
///
/// On Postgres each table's plan runs in one transaction (a mid-plan failure
/// rolls that table back); SQLite applies statements one at a time. Like
/// `connect()`'s flags it runs under the run lock and refuses a database
/// governed by ferro migrations; `tracking_schemas` are the project's
/// configured `tracking_schema`s.
///
/// # Errors
/// Returns the pass's report as JSON ([`PassReport::to_json`]), which
/// `ferro.migrate` reads into a `PassReport`.
///
/// # Errors
/// Returns a `PyErr` if the engine is not initialized or the migration
/// fails; a failure of the pass carries its report ([`with_pass_report`]).
#[pyfunction]
#[pyo3(signature = (using=None, updates=true, destructive=false, tracking_schemas=Vec::new(), ddl_lock_timeout_s=5.0))]
pub fn migrate(
    py: Python<'_>,
    using: Option<String>,
    updates: bool,
    destructive: bool,
    tracking_schemas: Vec<String>,
    ddl_lock_timeout_s: f64,
) -> PyResult<Bound<'_, PyAny>> {
    let opts = MigrateOptions::laddered(updates, destructive)
        .with_ddl_lock_timeout_seconds(ddl_lock_timeout_s)?;
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let engine = engine_for_connection(using)?;
        run_pass_for_python(engine, opts, &tracking_schemas, AutoMigrateDoor::Migrate).await
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
/// the text of the plan's one-off reports, the rendering reports and the
/// plan's recurring reports. Column drops render as their plain `DROP COLUMN` (the SQLite
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
    let plan = plan_from_ir(
        &Side::live(live, facts).map_err(plan_error)?,
        &Side::declared(declared),
        backend,
        opts.plan_options(),
    );
    let rendered = plan.render().map_err(emission_error)?;

    let mut statements = Vec::new();
    let (recurring, mut warnings): (Vec<Report>, Vec<Report>) =
        plan.reports.into_iter().partition(|report| report.recurs);
    for op in rendered {
        statements.extend(op.statements);
        warnings.extend(op.reports);
    }
    warnings.extend(recurring);
    Ok((
        statements,
        warnings.into_iter().map(|report| report.text).collect(),
    ))
}

/// The down of the live-origin plan over FFI (ADR-0050): what turns the
/// database `_plan_from_ir(live_json, declared_json, …, facts_json)` leaves
/// back into the live one — `ferro_migrate::plan_down(up, models, live)`,
/// the one down every door uses, run back from the models (declared) to the
/// database (live) and scoped to the artifacts the upgrade touched. It is
/// the Alembic bridge's `downgrade()`. `facts_json` is the facts
/// `_live_schema_ir` returned beside `live_json`.
///
/// The result has `_plan_from_ir`'s shape: `{"operations": [{"kind": …,
/// <fields>, "verdict": {…}}], "reports": […]}`. An op the live database
/// cannot express carries the verdict's `{"execution": {"irreversible":
/// <reason>}}`. With `render`, each op also carries its `statements` and
/// `reports` but the ops at the `unrendered` indexes, which carry none:
/// those the bridge writes itself, a re-added column that demands values of
/// existing rows (the same subset its upgrade leaves `unrendered` in
/// `_plan_from_ir`).
///
/// # Errors
/// `ValueError` when a JSON argument is malformed, an envelope is not a
/// `schema` IR, the dialect is unknown, a live table has no facts entry, or
/// an op cannot render.
#[pyfunction]
#[pyo3(name = "_plan_reverse_from_ir")]
#[pyo3(signature = (live_json, declared_json, dialect, options_json, facts_json, render=true, unrendered=None))]
pub fn _plan_reverse_from_ir(
    live_json: String,
    declared_json: String,
    dialect: String,
    options_json: String,
    facts_json: String,
    render: bool,
    unrendered: Option<Vec<usize>>,
) -> PyResult<String> {
    let backend = parse_dialect(&dialect)?;
    let live = parse_schema_envelope(&live_json, "live_json")?;
    let declared = parse_schema_envelope(&declared_json, "declared_json")?;
    let options: PlanOptions = serde_json::from_str(&options_json).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("invalid options_json: {e}"))
    })?;
    let facts: LiveFacts = serde_json::from_str(&facts_json).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("invalid facts_json: {e}"))
    })?;
    validate_schema_ir(&declared).map_err(emission_error)?;
    let live = Side::live(live, facts).map_err(plan_error)?;
    let declared = Side::declared(declared);
    let up: Vec<MigrationOp> = plan_from_ir(&live, &declared, backend, options)
        .ops()
        .cloned()
        .collect();
    let down = plan_down(&up, &declared, &live, backend);
    plan_json(&down, render, unrendered)
}

/// Reports as plan JSON: each `{"kind", "subject", "text", "recurs",
/// "blocks"}`, its kind a name or `{name: fields}`.
fn reports_json(reports: &[Report]) -> PyResult<serde_json::Value> {
    serde_json::to_value(reports).map_err(|e| {
        pyo3::exceptions::PyRuntimeError::new_err(format!("could not serialize the plan: {e}"))
    })
}

/// One rendered op as plan JSON: the op, its `statements` and `reports`,
/// and an `AddTable`'s `row_security_statements`.
fn rendered_op_json(rendered: RenderedOp) -> PyResult<serde_json::Value> {
    let mut op = serde_json::to_value(&rendered.op).map_err(|e| {
        pyo3::exceptions::PyRuntimeError::new_err(format!("could not serialize the plan: {e}"))
    })?;
    if let Some(fields) = op.as_object_mut() {
        fields.insert("statements".into(), rendered.statements.into());
        fields.insert("reports".into(), reports_json(&rendered.reports)?);
        if matches!(rendered.op, MigrationOp::AddTable { .. }) {
            fields.insert(
                "row_security_statements".into(),
                rendered.row_security_statements.into(),
            );
        }
    }
    Ok(op)
}

pub(crate) fn parse_schema_envelope(
    json: &str,
    what: &str,
) -> PyResult<IrEnvelope<SchemaIrPayload>> {
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
/// `options_json` is `{"destructive": bool}`. `facts_json` says which side
/// `old_ir_json` is: given (even `"{}"`), a live database whose side-table
/// `_live_schema_ir` returned beside it, with an entry for every table
/// (`Side::live`); omitted, a declared snapshot (`Side::declared`). The result is
/// `{"operations": [{"kind": …, <op fields>, "verdict": {…}}], "reports":
/// [{"kind", "subject", "text", "recurs", "blocks"}]}` — each op beside its
/// verdict ([`ferro_migrate::OpVerdict`]); with `render`, each op also carries the
/// `statements` and `reports` it renders to, but the ops at the `unrendered`
/// indexes, which carry none and render nothing: the ones a caller writes
/// its own way (the Alembic bridge's column adds that demand values of
/// existing rows, which the pass has no statement for and the revision
/// writes as the plain Alembic op under `# ferro: data-dependent`; ADR-0041).
/// The rest render as that subset alone (`Plan::render_ops`).
///
/// # Errors
/// `ValueError` when a JSON argument is malformed, an envelope is not a
/// `schema` IR, the dialect is unknown, a live table of `old_ir_json` has no
/// entry in `facts_json`, or an op cannot render.
#[pyfunction]
#[pyo3(name = "_plan_from_ir")]
#[pyo3(signature = (old_ir_json, new_ir_json, dialect, options_json, render=false, facts_json=None, unrendered=None))]
pub fn _plan_from_ir(
    old_ir_json: String,
    new_ir_json: String,
    dialect: String,
    options_json: String,
    render: bool,
    facts_json: Option<String>,
    unrendered: Option<Vec<usize>>,
) -> PyResult<String> {
    let backend = parse_dialect(&dialect)?;
    let old = parse_schema_envelope(&old_ir_json, "old_ir_json")?;
    let new = parse_schema_envelope(&new_ir_json, "new_ir_json")?;
    let options: PlanOptions = serde_json::from_str(&options_json).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("invalid options_json: {e}"))
    })?;
    validate_schema_ir(&old).map_err(emission_error)?;
    validate_schema_ir(&new).map_err(emission_error)?;
    let old = match facts_json {
        Some(json) => {
            let facts: LiveFacts = serde_json::from_str(&json).map_err(|e| {
                pyo3::exceptions::PyValueError::new_err(format!("invalid facts_json: {e}"))
            })?;
            Side::live(old, facts).map_err(plan_error)?
        }
        None => Side::declared(old),
    };

    let plan = plan_from_ir(&old, &Side::declared(new), backend, options);
    plan_json(&plan, render, unrendered)
}

/// `plan` as the FFI's plan JSON: `{"operations": [{"kind": …, <op fields>,
/// "verdict": {…}}], "reports": […]}` — each op beside its verdict (the one
/// every door reads, ADR-0050). With `render`, each op also carries the
/// `statements` and `reports` it renders to (and an `AddTable` its
/// `row_security_statements`), but the ops at the `unrendered` indexes,
/// which carry none and render nothing; the rest render as that subset
/// alone (`Plan::render_ops`).
fn plan_json(plan: &Plan, render: bool, unrendered: Option<Vec<usize>>) -> PyResult<String> {
    let to_value = |value: serde_json::Result<serde_json::Value>| {
        value.map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("could not serialize the plan: {e}"))
        })
    };
    let unrendered: BTreeSet<usize> = unrendered.unwrap_or_default().into_iter().collect();
    let mut rendered = if render {
        let kept: Vec<usize> = (0..plan.operations.len())
            .filter(|index| !unrendered.contains(index))
            .collect();
        Some(plan.render_ops(&kept).map_err(emission_error)?.into_iter())
    } else {
        None
    };
    let operations: Vec<serde_json::Value> = plan
        .operations
        .iter()
        .enumerate()
        .map(|(index, planned)| {
            let mut value = match rendered.as_mut() {
                Some(_) if unrendered.contains(&index) => {
                    let mut value = to_value(serde_json::to_value(&planned.op))?;
                    if let Some(fields) = value.as_object_mut() {
                        fields.insert("statements".into(), serde_json::json!([]));
                        fields.insert("reports".into(), serde_json::json!([]));
                    }
                    value
                }
                Some(rendered) => rendered_op_json(rendered.next().ok_or_else(|| {
                    pyo3::exceptions::PyRuntimeError::new_err(
                        "the plan rendered fewer ops than it holds",
                    )
                })?)?,
                None => to_value(serde_json::to_value(&planned.op))?,
            };
            if let Some(fields) = value.as_object_mut() {
                fields.insert(
                    "verdict".into(),
                    to_value(serde_json::to_value(&planned.verdict))?,
                );
            }
            Ok(value)
        })
        .collect::<PyResult<_>>()?;
    let out = serde_json::json!({
        "operations": operations,
        "reports": reports_json(&plan.reports)?,
    });
    Ok(out.to_string())
}
