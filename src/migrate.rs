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

use crate::backend::{EngineBindValue, EngineHandle};
use crate::ddl_exec::{DdlError, DdlExecutor, DdlFailure};
use crate::introspect::{
    LiveCheck, LiveColumn, LiveForeignKey, LiveIndex, column_holds_label,
    connected_role_bypasses_row_security, live_table_checks, live_table_columns, quote_ident,
    sqlite_indexes_covering_column,
};
use crate::live_ir::{
    LiveTable, live_schema_ir, live_table_renames, live_tables_to_schema_ir, tables_to_read,
};
use crate::run::{
    FORMAT_TABLE, RunLock, TRACKING_TABLE, governed_schema, refused, tracking_tables_for,
};
use crate::schema::internal_create_tables;
use crate::state::{MODEL_REGISTRY, engine_for_connection};
use ferro_ddl_lowering::{Dialect, LiveRowSecurity, row_security_migrator_warning};
use ferro_migrate::{
    LiveFacts, MigrationOp, PlanOptions, RenderedOp, plan_from_ir, render_plan, validate_schema_ir,
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

/// Prefix of the debug line logged before each statement the DDL lock
/// timeout (ADR-0044) adds around a reconciliation unit on Postgres —
/// `SET LOCAL lock_timeout = '5000ms'`, or `SET` / `RESET lock_timeout`
/// around an enum type's statements. A line of its own, not
/// [`RECONCILE_STATEMENT_LOG_PREFIX`]'s: it is the session's lock policy,
/// not a statement of the pass, so every recording and parity check of the
/// pass's DDL (AGENTS.md § I-1) reads exactly what it did before.
const LOCK_TIMEOUT_LOG_PREFIX: &str = "Ferro Engine: auto-migrate lock timeout on";

pub(crate) fn log_lock_timeout_statement(subject: &str, sql: &str) {
    crate::log_debug(format!("{LOCK_TIMEOUT_LOG_PREFIX} '{subject}': {sql}"));
}

/// Prefix of the debug line logged before each row probe a SQLite label
/// rename hint costs (`render_label_held_probe`). A line of its own: the
/// probe reads rows and changes nothing, so the pass's DDL recordings read
/// exactly what they did before, while the probe's cost stays observable.
const LABEL_PROBE_LOG_PREFIX: &str = "Ferro Engine: auto-migrate reading rows of";

fn log_label_probe(table: &str, sql: &str) {
    crate::log_debug(format!("{LABEL_PROBE_LOG_PREFIX} '{table}': {sql}"));
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

/// A reconciliation statement's failure: the database error, the statement,
/// and the column when the statement drops one (its message differs).
struct PassFailure {
    error: sqlx::Error,
    statement: Option<String>,
    dropped_column: Option<String>,
}

impl From<sqlx::Error> for PassFailure {
    fn from(error: sqlx::Error) -> Self {
        Self {
            error,
            statement: None,
            dropped_column: None,
        }
    }
}

impl DdlFailure for PassFailure {
    fn database_error(&self) -> Option<&sqlx::Error> {
        Some(&self.error)
    }

    fn statement(&self) -> Option<&str> {
        self.statement.as_deref()
    }
}

/// The warning the create and reconciliation passes raise for each attempt
/// that timed out waiting for a lock: `migrating 'author': waiting for a lock
/// on "author" (attempt 1 of 10, retry in 1s)`.
pub(crate) fn pass_attempt_warning(
    subject: &str,
    attempt: &crate::ddl_exec::Attempt,
    of: u8,
) -> String {
    format!("migrating '{subject}': {}", attempt.describe(of))
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
/// transaction (FF-G G3) under the DDL lock timeout (ADR-0044): `SET LOCAL
/// lock_timeout` first, and a statement that times out waiting for a lock
/// rolls the group back and runs it again from the top, up to ten attempts.
/// A mid-plan failure leaves the table exactly as it was, so a failed run is
/// safely re-runnable. SQLite runs statement at a time, with each column
/// drop going through its index-dependency path, and sets no timeout.
/// Returns how many statements ran and how many columns were dropped.
async fn execute_table_ops(
    engine: &EngineHandle,
    table: &str,
    ops: &[&RenderedOp],
    backend: Dialect,
    ddl: &DdlExecutor,
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

    // The executor owns the transaction: BEGIN, `SET LOCAL lock_timeout`,
    // this group, COMMIT; on a failure ROLLBACK, and a connection whose
    // ROLLBACK failed is discarded rather than returned to the pool (#416).
    let of = ddl.max_attempts;
    let result = ddl
        .transactional(
            engine,
            |sql| log_lock_timeout_statement(table, sql),
            |attempt| crate::emit_user_warning_always(&pass_attempt_warning(table, &attempt, of)),
            |mut conn| async move {
                let result = async {
                    for op in ops {
                        for sql in &op.statements {
                            log_reconcile_statement(table, sql);
                            conn.execute_sql_unprepared(sql).await.map_err(|error| {
                                PassFailure {
                                    error,
                                    statement: Some(sql.clone()),
                                    dropped_column: match &op.op {
                                        MigrationOp::DropColumn { column, .. } => {
                                            Some(column.clone())
                                        }
                                        _ => None,
                                    },
                                }
                            })?;
                        }
                    }
                    Ok::<(), PassFailure>(())
                }
                .await;
                (conn, result)
            },
        )
        .await;
    match result {
        Ok(()) => Ok((statements - drops, drops)),
        Err(DdlError::LockTimeout(timeout)) => Err(pass_lock_timeout_error(table, &timeout)),
        Err(DdlError::Failed(failure)) => Err(match (failure.dropped_column, failure.statement) {
            (Some(column), _) => map_drop_column_error(table, &column, failure.error),
            (None, Some(sql)) => map_statement_error(table, &sql, failure.error),
            (None, None) => crate::errors::map_db_error(
                &format!("Auto-migrate failed to apply DDL to table '{table}'"),
                failure.error,
            ),
        }),
    }
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
    fn call(self) -> &'static str {
        match self {
            AutoMigrateDoor::Connect => "connect(auto_migrate=…)",
            AutoMigrateDoor::CreateTables => "create_tables()",
            AutoMigrateDoor::Migrate => "migrate()",
        }
    }
}

/// The warning an auto-migrate pass raises the moment it finds the run lock
/// held, so a caller that is waiting says why.
pub fn auto_migrate_waiting_text(door: AutoMigrateDoor) -> String {
    format!(
        "{} is waiting: another ferro migration run or auto-migrate pass holds the run lock \
         on this database. It goes on once that one finishes.",
        door.call()
    )
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
/// `door` names the public call for the waiting warning.
///
/// # Errors
/// The guard's refusal; the pooler refusal behind a transaction-mode
/// pooler; whatever the passes raise.
pub async fn internal_migrate(
    engine: Arc<EngineHandle>,
    opts: MigrateOptions,
    tracking_schemas: &[String],
    door: AutoMigrateDoor,
) -> PyResult<()> {
    let lock = RunLock::acquire(&engine, None, AUTO_MIGRATE_LOCK_WAIT, |_| {
        crate::emit_user_warning_always(&auto_migrate_waiting_text(door));
    })
    .await?;
    let outcome = async {
        guard_tracked_schema(&engine, tracking_schemas).await?;
        run_passes(engine.clone(), opts).await
    }
    .await;
    let released = lock.release().await;
    outcome.and(released)
}

/// The create pass, then (per `MigrateOptions`) the reconciliation of
/// existing tables with the registered models.
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
async fn run_passes(engine: Arc<EngineHandle>, opts: MigrateOptions) -> PyResult<()> {
    // One lock-timeout policy for every DDL statement of both passes (ADR-0044).
    let ddl = DdlExecutor::new(opts.ddl_lock_timeout);
    let created = internal_create_tables(engine.clone(), opts.updates, &ddl).await?;
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
    let plan =
        plan_from_ir(&live, &modelset, backend, &facts, opts.plan_options()).map_err(plan_error)?;
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
            crate::emit_user_warning_always(&warning);
        }
    }

    let mut warnings = plan.warnings.clone();
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
                let of = ddl.max_attempts;
                let statements = &current.statements;
                ddl.unwrapped(
                    &engine,
                    |sql| log_lock_timeout_statement(subject, sql),
                    |attempt| {
                        crate::emit_user_warning_always(&pass_attempt_warning(
                            subject, &attempt, of,
                        ))
                    },
                    |mut conn| async move {
                        let mut result = Ok(());
                        for sql in statements {
                            log_reconcile_statement(subject, sql);
                            if let Err(error) = conn.execute_sql_unprepared(sql).await {
                                result = Err(crate::ddl_exec::StatementError::at(sql, error));
                                break;
                            }
                        }
                        (conn, result)
                    },
                )
                .await
                .map_err(|err| match err {
                    DdlError::LockTimeout(timeout) => pass_lock_timeout_error(subject, &timeout),
                    DdlError::Failed(failure) => type_statement_error(&current.op, failure.error),
                })?;
                ddl_ran = true;
            }
            warnings.extend(current.warnings.iter().cloned());
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
            execute_table_ops(&engine, table, &group, backend, &ddl).await?;
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
    // After the renames ran, so each table and column is read by its
    // declared name.
    let reconciled: HashSet<String> = reconciled.into_iter().map(str::to_string).collect();
    for warning in stranded_label_warnings(&engine, &modelset, &reconciled).await? {
        crate::emit_user_warning_always(&warning);
    }
    // Row-security notes describe whether THIS connect left rows fenced, so
    // the warning registry must never quiet them down after the first boot.
    for warning in &plan.always_warnings {
        crate::emit_user_warning_always(warning);
    }

    Ok(())
}

/// The warnings for the live label renames (`__ferro_renamed_labels__`) on
/// SQLite, where every enum keeps its labels as text in the rows and the live
/// side carries none, so the planner sees no label to rename. A hint is live
/// (ADR-0032) while the database still holds its old label. A column with a
/// `db_check` answers from the check alone, since it bounds every row: it
/// lists the old label (live) or does not (inert), and no row is read. Only
/// a column with no check is probed ([`column_holds_label`], logged with
/// [`LABEL_PROBE_LOG_PREFIX`]). Called under `migrate_updates` only: the
/// probe reads the whole column once nothing matches, which a plain connect
/// must never pay for a hint that may stay (ADR-0047, ADR-0011, ADR-0032).
/// The pass changes the schema, never rows (ADR-0014), so a live hint is one
/// warning naming `ferro migrate new`, whose migration relabels the rows; an
/// inert one is silent. A refused hint is the planner's to report.
///
/// `existing` are the tables that stood before this connect's create pass, as
/// they stand now; a declared column they lack (its rename still pending) is
/// not read.
async fn stranded_label_warnings(
    engine: &EngineHandle,
    modelset: &IrEnvelope<SchemaIrPayload>,
    existing: &HashSet<String>,
) -> PyResult<Vec<String>> {
    use ferro_ddl_lowering::{
        check_lists_label, db_check_constraint_name, stranded_label_rename_warning,
    };
    let mut warnings = Vec::new();
    if engine.backend() != Dialect::Sqlite
        || ferro_migrate::plan::refuse_hints(&modelset.payload).is_err()
    {
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
                        log_label_probe(
                            table,
                            &ferro_ddl_lowering::render_label_held_probe(table, &col.name, old),
                        );
                        column_holds_label(engine, table, &col.name, old).await?
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
/// Returns a `PyErr` if the engine is not initialized or the migration fails.
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
        internal_migrate(engine, opts, &tracking_schemas, AutoMigrateDoor::Migrate).await
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
    let plan =
        plan_from_ir(&live, &declared, backend, &facts, opts.plan_options()).map_err(plan_error)?;
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

/// The reverse of the live-origin plan over FFI (ADR-0041): what turns the
/// database `_plan_from_ir(live_json, declared_json, …, facts_json)` leaves
/// back into the live one (`ferro_migrate::plan::reverse_live_plan`) — the
/// Alembic bridge's `downgrade()`. `facts_json` is the facts
/// `_live_schema_ir` returned beside `live_json`.
///
/// The result has `_plan_from_ir`'s shape: `{"operations": [{"kind": …,
/// <fields>}], "warnings": [], "always_warnings": []}`, plus `before`: the
/// live envelope under the forward plan's renames, the side the reverse's
/// steps turn the database into (what `_plan_step_verdicts` reads them
/// against). A step nothing undoes
/// is the forward op carrying `"irreversible": {"reason": …}`; a check or
/// policy put back from the catalog is a `RestoreCheck` / `RestoreRowPolicy`;
/// a foreign key the forward plan added comes off as `DropForeignKey`. With
/// `render`, each op also carries its `statements` and `warnings`; the ops at
/// the `unrendered` indexes carry none: those the bridge writes itself, a
/// re-added column that demands values of existing rows (the same subset its
/// upgrade leaves out of `_render_plan_ops`).
///
/// # Errors
/// `ValueError` when a JSON argument is malformed, an envelope is not a
/// `schema` IR, the dialect is unknown, a live table has no facts entry, or a
/// step cannot render.
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
    use ferro_migrate::plan::{render_reverse_plan, reverse_live_plan};
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
    let forward = plan_from_ir(&live, &declared, backend, &facts, options).map_err(plan_error)?;
    let reverse =
        reverse_live_plan(&forward, &live, &facts, &declared, backend).map_err(plan_error)?;
    let operations: Vec<serde_json::Value> = if render {
        let unrendered: std::collections::BTreeSet<usize> =
            unrendered.unwrap_or_default().into_iter().collect();
        render_reverse_plan(&reverse, &declared, backend, &unrendered)
            .map_err(emission_error)?
            .into_iter()
            .map(|rendered| {
                let mut op = rendered.op.to_json();
                if let Some(fields) = op.as_object_mut() {
                    fields.insert("statements".into(), rendered.statements.into());
                    fields.insert("warnings".into(), rendered.warnings.into());
                }
                op
            })
            .collect()
    } else {
        reverse.operations.iter().map(|op| op.to_json()).collect()
    };
    let before = serde_json::to_value(&reverse.before).map_err(|e| {
        pyo3::exceptions::PyRuntimeError::new_err(format!("could not serialize the plan: {e}"))
    })?;
    let out = serde_json::json!({
        "operations": operations,
        "warnings": Vec::<String>::new(),
        "always_warnings": Vec::<String>::new(),
        "before": before,
    });
    Ok(out.to_string())
}

/// One rendered op as plan JSON: the op, its `statements` and `warnings`,
/// and an `AddTable`'s `row_security_statements`.
fn rendered_op_json(rendered: RenderedOp) -> PyResult<serde_json::Value> {
    let mut op = serde_json::to_value(&rendered.op).map_err(|e| {
        pyo3::exceptions::PyRuntimeError::new_err(format!("could not serialize the plan: {e}"))
    })?;
    if let Some(fields) = op.as_object_mut() {
        fields.insert("statements".into(), rendered.statements.into());
        fields.insert("warnings".into(), rendered.warnings.into());
        if matches!(rendered.op, MigrationOp::AddTable { .. }) {
            fields.insert(
                "row_security_statements".into(),
                rendered.row_security_statements.into(),
            );
        }
    }
    Ok(op)
}

/// Render the planner ops `operations_json` (a plan's `operations`, extra
/// keys ignored) as one plan from `old_ir_json` to `new_ir_json` on
/// `dialect`, in the given order: `_plan_from_ir(..., render=True)` for a
/// chosen subset of a plan. The Alembic bridge renders every op of its plan
/// but those that demand values of existing rows, which the pass has no
/// statement for and the revision writes as the plain Alembic op under
/// `# ferro: data-dependent` (ADR-0041).
///
/// # Errors
/// `ValueError` when a JSON argument is malformed, an envelope is not a
/// `schema` IR, the dialect is unknown, or an op cannot render.
#[pyfunction]
#[pyo3(name = "_render_plan_ops")]
pub fn _render_plan_ops(
    old_ir_json: String,
    new_ir_json: String,
    dialect: String,
    operations_json: String,
) -> PyResult<String> {
    let backend = parse_dialect(&dialect)?;
    let old = parse_schema_envelope(&old_ir_json, "old_ir_json")?;
    let new = parse_schema_envelope(&new_ir_json, "new_ir_json")?;
    let operations: Vec<MigrationOp> = serde_json::from_str(&operations_json).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("invalid operations_json: {e}"))
    })?;
    let plan = ferro_migrate::MigrationPlan {
        operations,
        ..Default::default()
    };
    let rendered: Vec<serde_json::Value> = render_plan(&plan, &old, &new, backend)
        .map_err(emission_error)?
        .into_iter()
        .map(rendered_op_json)
        .collect::<PyResult<_>>()?;
    Ok(serde_json::Value::Array(rendered).to_string())
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
/// `_live_schema_ir` returned beside it, with an entry for every table;
/// omitted, a declared snapshot (`LiveFacts::declared`). The result is
/// `{"operations": [{"kind": …, <op fields>}], "warnings": […],
/// "always_warnings": […]}`; with `render`, each op also carries the
/// `statements` and `warnings` it renders to.
///
/// # Errors
/// `ValueError` when a JSON argument is malformed, an envelope is not a
/// `schema` IR, the dialect is unknown, a live table of `old_ir_json` has no
/// entry in `facts_json`, or an op cannot render.
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

    let plan = plan_from_ir(&old, &new, backend, &facts, options).map_err(plan_error)?;
    let to_value = |value: serde_json::Result<serde_json::Value>| {
        value.map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("could not serialize the plan: {e}"))
        })
    };
    let operations: Vec<serde_json::Value> = if render {
        render_plan(&plan, &old, &new, backend)
            .map_err(emission_error)?
            .into_iter()
            .map(rendered_op_json)
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
