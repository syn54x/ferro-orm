//! The run's effects on the database (ADR-0028: Python sequences a run, Rust
//! decides and executes): the run lock, the two tracking tables, step
//! records, and executing a SQL step in each of its three modes.
//!
//! ```text
//! $ ferro migrate up
//! 0001_create_author  01_schema  applied (12 ms)
//! ```
//!
//! For that line the runner took the [`RunLock`], created
//! `_ferro_migrations` and `_ferro_migrations_format` ([`ensure_tracking_tables`]),
//! read the records ([`read_records`]), and ran
//! `0001_create_author/01_schema.up.postgres.sql` through
//! [`execute_sql_step`], whose record committed with the file's statements.
//! `ferro migrate down` runs the step's `.down` file through the same
//! executor, and [`remove_record`] deletes the record in the down's own
//! transaction.
//!
//! I-1: the runner renders no schema DDL. The only DDL built here is the
//! tracking tables' own (#466); every other statement comes from a step file.

use crate::backend::{
    EngineBindValue, EngineConnection, EngineHandle, EngineRow, EngineValue, NullKind,
};
use crate::ddl_exec::{Attempt, DdlError, DdlExecutor, StatementError, pool_connection};
use ferro_ddl_lowering::Dialect;
use ferro_migrate::generate::rebuild::rebuilt_tables;
use ferro_migrate::plan::{Hint, live_hints, reverse_hints};
use ferro_migrate::run_plan::{
    Direction, ExecMode, Origin, PlannedStep, RecordKind, StepRecord, TRACKING_FORMAT,
    check_format, run_lock_key, split_statements,
};
use ferro_migrate::snapshot::{encode_checksum, sha384};
use ferro_schema_ir::SchemaModel;
use once_cell::sync::Lazy;
use pyo3::prelude::*;
use sqlx::{Connection, Row};
use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

/// The step-record table.
pub const TRACKING_TABLE: &str = "_ferro_migrations";
/// The one-row format table (#466, ADR-0038).
pub const FORMAT_TABLE: &str = "_ferro_migrations_format";

/// The line a second run prints the moment it finds the lock held (#473).
pub const WAITING_TEXT: &str = "Another ferro migration run holds the lock on this database; waiting (--lock-timeout to bound it).";

/// How often a waiting run tries the lock again.
const LOCK_POLL: Duration = Duration::from_millis(100);

// -- refusals ------------------------------------------------------------------

/// A refusal: `ferro.migrations.report.RunRefused(text)`, a `FerroError` the
/// CLI prints and exits 1 on. Falls back to `RuntimeError` only if the
/// Python module cannot be imported.
pub fn refused(text: impl Into<String>) -> PyErr {
    let text = text.into();
    Python::attach(|py| {
        match py
            .import("ferro.migrations.report")
            .and_then(|module| module.getattr("RunRefused"))
            .and_then(|class| class.call1((text.clone(),)))
        {
            Ok(instance) => PyErr::from_value(instance),
            Err(_) => pyo3::exceptions::PyRuntimeError::new_err(text),
        }
    })
}

/// A run planner refusal as `RunRefused(text, kind=..., migration=...,
/// step=..., reason=...)`, so a caller matches on its kind rather than its
/// text. Falls back to `RuntimeError` only if the Python module cannot be
/// imported.
pub fn refused_by(refusal: &ferro_migrate::RunRefusal) -> PyErr {
    let text = refusal.to_string();
    Python::attach(|py| {
        let kwargs = pyo3::types::PyDict::new(py);
        let built = (|| {
            kwargs.set_item("kind", refusal.kind())?;
            kwargs.set_item("migration", refusal.migration())?;
            kwargs.set_item("step", refusal.step())?;
            kwargs.set_item("reason", refusal.reason())?;
            py.import("ferro.migrations.report")?
                .getattr("RunRefused")?
                .call((text.clone(),), Some(&kwargs))
        })();
        match built {
            Ok(instance) => PyErr::from_value(instance),
            Err(_) => pyo3::exceptions::PyRuntimeError::new_err(text),
        }
    })
}

fn db_error(context: &str, err: sqlx::Error) -> PyErr {
    crate::errors::map_db_error(context, err)
}

/// Why the run lock is not (or no longer) this run's.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RunLockRefusal {
    /// The first check after acquiring failed: the statements did not run
    /// on the session that took the lock — a transaction-mode pooler.
    Pooler,
    /// A later check failed: the lock's connection was lost mid-run.
    Dropped,
}

impl std::fmt::Display for RunLockRefusal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            RunLockRefusal::Pooler => f.write_str(
                "ferro migrate: the run lock was taken but its session does not hold it: \
                 migrations need a direct or session-mode connection. A transaction-mode \
                 pooler (PgBouncer pool_mode=transaction, for one) hands each statement to a \
                 different server session, so no lock survives between them. Point the URL at \
                 Postgres directly or at a session-mode pool. Nothing was applied.",
            ),
            RunLockRefusal::Dropped => f.write_str(
                "ferro migrate: the run lock was lost: its connection to the database closed, \
                 so another run could start. Stopped before the next step; nothing after the \
                 last applied step ran. Run `ferro migrate up` again to resume where this run \
                 stopped.",
            ),
        }
    }
}

/// Whether a failed lock probe means the lock's session is gone (the
/// connection broke, so the server released the lock) rather than a query
/// the database refused. Only the first is a lost lock; the second is the
/// database's error, reported as itself.
pub fn probe_lost_the_session(err: &sqlx::Error) -> bool {
    matches!(
        err,
        sqlx::Error::Io(_)
            | sqlx::Error::Tls(_)
            | sqlx::Error::Protocol(_)
            | sqlx::Error::PoolClosed
            | sqlx::Error::WorkerCrashed
    )
}

/// The decision behind [`RunLock::verify`]: whether this session holds the
/// lock, on the first check after acquiring or a later one.
///
/// # Errors
/// [`RunLockRefusal::Pooler`] when the first check fails,
/// [`RunLockRefusal::Dropped`] when a later one does.
pub fn lock_verification_outcome(held: bool, first: bool) -> Result<(), RunLockRefusal> {
    match (held, first) {
        (true, _) => Ok(()),
        (false, true) => Err(RunLockRefusal::Pooler),
        (false, false) => Err(RunLockRefusal::Dropped),
    }
}

/// The refusal when the wait for the lock outlasts `timeout`.
pub fn lock_timeout_text(timeout: Duration) -> String {
    format!(
        "ferro migrate: another ferro migration run held the run lock on this database for \
         longer than the lock timeout ({}). Nothing was applied; run again once it has \
         finished, or raise --lock-timeout.",
        show_duration(timeout)
    )
}

pub(crate) fn show_duration(duration: Duration) -> String {
    let ms = duration.as_millis();
    if ms.is_multiple_of(1000) {
        format!("{}s", ms / 1000)
    } else {
        format!("{ms}ms")
    }
}

// -- names -------------------------------------------------------------------------

fn quote_ident(name: &str) -> String {
    format!("\"{}\"", name.replace('"', "\"\""))
}

/// Where the tracking tables live: `tracking_schema` when set (Postgres
/// only), else the connection's current schema, unqualified.
#[derive(Clone, Debug)]
struct Tracking {
    schema: Option<String>,
}

impl Tracking {
    fn new(dialect: Dialect, schema: Option<&str>) -> Self {
        Self {
            schema: match dialect {
                Dialect::Postgres => schema.map(str::to_string),
                Dialect::Sqlite => None,
            },
        }
    }

    fn table(&self, name: &str) -> String {
        match &self.schema {
            Some(schema) => format!("{}.{}", quote_ident(schema), quote_ident(name)),
            None => quote_ident(name),
        }
    }
}

fn param(dialect: Dialect, n: usize) -> String {
    match dialect {
        Dialect::Postgres => format!("${n}"),
        Dialect::Sqlite => "?".to_string(),
    }
}

fn now_iso() -> String {
    sqlx::types::chrono::Utc::now()
        .format("%Y-%m-%dT%H:%M:%S%.6fZ")
        .to_string()
}

fn text(value: Option<&EngineValue>) -> Option<String> {
    match value? {
        EngineValue::String(s) => Some(s.clone()),
        EngineValue::TimestampTz(ts) => Some(ts.format("%Y-%m-%dT%H:%M:%S%.6fZ").to_string()),
        EngineValue::Timestamp(ts) => Some(ts.format("%Y-%m-%dT%H:%M:%S%.6fZ").to_string()),
        EngineValue::I64(i) => Some(i.to_string()),
        EngineValue::Null => None,
        other => Some(format!("{other:?}")),
    }
}

fn int(value: Option<&EngineValue>) -> Option<i64> {
    match value? {
        EngineValue::Bool(b) => Some(i64::from(*b)),
        other => other.as_i64(),
    }
}

fn column(row: &EngineRow, index: usize) -> Option<&EngineValue> {
    row.values.get(index).map(|(_, value)| value)
}

// -- the governed schema and the catalog --------------------------------------------

/// The governed schema (ADR-0038): Postgres' current schema (first entry
/// of `search_path`), SQLite's `main`.
///
/// # Errors
/// A database error, or a refusal when the `search_path` names no existing
/// schema.
pub async fn governed_schema(engine: &EngineHandle) -> PyResult<String> {
    match engine.backend() {
        Dialect::Sqlite => Ok("main".to_string()),
        Dialect::Postgres => {
            let rows = engine
                .fetch_all_sql_unprepared("SELECT current_schema()::text")
                .await
                .map_err(|e| db_error("reading the current schema", e))?;
            rows.first()
                .and_then(|row| text(column(row, 0)))
                .ok_or_else(|| {
                    refused(
                        "ferro migrate: this connection has no current schema: no schema in its \
                         search_path exists. Create the schema or fix the search_path. Nothing \
                         was applied.",
                    )
                })
        }
    }
}

async fn table_exists(engine: &EngineHandle, tracking: &Tracking, name: &str) -> PyResult<bool> {
    let rows = match engine.backend() {
        Dialect::Sqlite => {
            engine
                .fetch_all_sql_unprepared_with_binds(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                    &[EngineBindValue::String(name.to_string())],
                )
                .await
        }
        Dialect::Postgres => {
            engine
                .fetch_all_sql_unprepared_with_binds(
                    "SELECT 1 FROM information_schema.tables \
                 WHERE table_schema = COALESCE($1, current_schema()) AND table_name = $2",
                    &[
                        match &tracking.schema {
                            Some(schema) => EngineBindValue::String(schema.clone()),
                            None => EngineBindValue::Null(NullKind::String),
                        },
                        EngineBindValue::String(name.to_string()),
                    ],
                )
                .await
        }
    }
    .map_err(|e| db_error("reading the catalog", e))?;
    Ok(!rows.is_empty())
}

/// The tables in the governed schema (SQLite: `main`, without its own
/// `sqlite_*` tables) — what the adoption refusal compares against the first
/// migration's snapshot.
///
/// # Errors
/// A database error.
pub async fn live_tables(engine: &EngineHandle) -> PyResult<Vec<String>> {
    let sql = match engine.backend() {
        Dialect::Sqlite => {
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
        }
        Dialect::Postgres => {
            "SELECT table_name::text FROM information_schema.tables \
             WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
        }
    };
    let rows = engine
        .fetch_all_sql_unprepared(sql)
        .await
        .map_err(|e| db_error("reading the catalog", e))?;
    Ok(rows.iter().filter_map(|row| text(column(row, 0))).collect())
}

// -- the pre-rebuild live check (ADR-0034) ---------------------------------------------

/// What a SQLite table rebuild of `table` would discard: each live object the
/// table holds that `parent` (the table as the step's file finds it in the
/// schema snapshot) does not declare, as one line naming it and its fix.
async fn rebuild_obstacles(
    engine: &EngineHandle,
    table: &str,
    parent: &SchemaModel,
) -> PyResult<Vec<String>> {
    let mut out = Vec::new();
    for live in crate::introspect::live_table_columns(engine, table)
        .await?
        .unwrap_or_default()
    {
        if !parent.columns.iter().any(|col| col.name == live.name) {
            out.push(format!(
                "column {}: declare it on the model in a migration, or drop it",
                quote_ident(&live.name)
            ));
        }
    }
    for index in crate::introspect::sqlite_foreign_indexes(engine, table).await? {
        out.push(format!(
            "index {}: drop it, or declare it on the model in a migration (ferro builds the \
             indexes it declares, named idx_/uq_)",
            quote_ident(&index)
        ));
    }
    for trigger in crate::introspect::sqlite_table_triggers(engine, table).await? {
        out.push(format!(
            "trigger {}: drop it; a rebuild's DROP TABLE would drop it, and ferro cannot \
             carry it across",
            quote_ident(&trigger)
        ));
    }
    Ok(out)
}

/// The refusal for a rebuild of `table` over `obstacles` (its lines), when
/// there are any.
fn rebuild_refusal(table: &str, obstacles: &[String]) -> Option<String> {
    if obstacles.is_empty() {
        return None;
    }
    let mut text = format!(
        "ferro migrate: a SQLite rebuild of table {} copies only the columns and recreates \
         only the indexes the schema snapshot declares, and the live table also holds:",
        quote_ident(table)
    );
    for line in obstacles {
        text.push_str(&format!("\n  - {line}"));
    }
    text.push_str("\nNothing was applied.");
    Some(text)
}

/// Refuse a SQLite table rebuild of `table` while the live table holds
/// anything `parent` (the table as the rebuilding file finds it in the
/// schema snapshot) does not declare: an undeclared column, an index ferro
/// does not own, a trigger. Each is named with its fix; nothing is carried
/// and there is no override (ADR-0034).
///
/// # Errors
/// `RunRefused` naming each object; a database error.
pub async fn check_rebuild_preconditions(
    engine: &EngineHandle,
    table: &str,
    parent: &SchemaModel,
) -> PyResult<()> {
    let obstacles = rebuild_obstacles(engine, table, parent).await?;
    match rebuild_refusal(table, &obstacles) {
        Some(text) => Err(refused(text)),
        None => Ok(()),
    }
}

/// The schema snapshots on either side of a step's migration, the one its
/// file starts from first: going up, the parent migration's (`None` before
/// the first migration) then the migration's own; going down, the other way.
fn adjacent_snapshots(
    step: &PlannedStep,
    down: bool,
) -> PyResult<[Option<IrEnvelope<SchemaIrPayload>>; 2]> {
    let shown = format!("{}/{}", step.migration_name, step.file);
    let directory = step
        .path
        .parent()
        .and_then(std::path::Path::parent)
        .ok_or_else(|| {
            refused(format!(
                "ferro migrate: cannot find the migrations directory of {shown}. Nothing was \
                 applied."
            ))
        })?;
    let dir = MigrationsDir::read(directory)
        .map_err(|err| refused(format!("ferro migrate: {err}. Nothing was applied.")))?;
    let parent = step.migration.checked_sub(1).filter(|n| *n > 0);
    let snapshot = |number: Option<u16>| {
        number.and_then(|number| {
            dir.migrations
                .iter()
                .find(|migration| migration.number == number)
                .map(|migration| migration.snapshot.ir.clone())
        })
    };
    let (own, parent) = (snapshot(Some(step.migration)), snapshot(parent));
    Ok(if down { [own, parent] } else { [parent, own] })
}

/// Before a SQLite `foreign-keys-off` step, check every table its file
/// rebuilds (each `CREATE TABLE "_ferro_new_<table>"` it holds) against the
/// schema snapshot the file starts from ([`check_rebuild_preconditions`]),
/// in the file's order. Any other step passes.
///
/// # Errors
/// `RunRefused` naming each undeclared column, foreign index and trigger of
/// the first table holding one, or a rebuilt table the snapshot does not
/// declare; a database error.
pub async fn check_rebuild_step(
    engine: &EngineHandle,
    step: &PlannedStep,
    sql: &str,
    down: bool,
) -> PyResult<()> {
    if step.mode != ExecMode::ForeignKeysOff
        || engine.backend() != Dialect::Sqlite
        || (down && step.nothing_to_reverse.is_some())
    {
        return Ok(());
    }
    let tables = rebuilt_tables(&split_statements(sql, engine.backend()));
    if tables.is_empty() {
        return Ok(());
    }
    let shown = format!("{}/{}", step.migration_name, step.file);
    let [snapshot, other] = adjacent_snapshots(step, down)?;
    let renames = step_table_renames(step, down)?;
    for table in tables {
        // A step that renames a table rebuilds it under its new name, after
        // the rename (ADR-0032, ADR-0046); before the step runs it still has
        // its starting name.
        let starting = renames
            .iter()
            .find_map(|hint| match hint {
                Hint::Table { old, new } if *new == table => Some(old.clone()),
                _ => None,
            })
            .unwrap_or_else(|| table.clone());
        let mut model = snapshot
            .as_ref()
            .and_then(|ir| ir.payload.models.iter().find(|m| m.table_name == starting))
            .cloned()
            .ok_or_else(|| {
                refused(format!(
                    "ferro migrate: {shown} rebuilds table {}, which the schema snapshot it \
                     starts from does not declare. Nothing was applied.",
                    quote_ident(&table)
                ))
            })?;
        // A later step of the migration (a contract after its expand) finds
        // the table as an earlier step left it: every column either side of
        // the migration declares is a declared column (ADR-0025), never one
        // the rebuild discards unannounced.
        if let Some(other) = other
            .as_ref()
            .and_then(|ir| ir.payload.models.iter().find(|m| m.table_name == table))
        {
            for col in &other.columns {
                if !model.columns.iter().any(|known| known.name == col.name) {
                    model.columns.push(col.clone());
                }
            }
        }
        check_rebuild_preconditions(engine, &starting, &model).await?;
    }
    Ok(())
}

/// The table renames a step's migration makes, in the direction the file
/// runs: the live rename hints of its snapshot against its parent's
/// ([`live_hints`], the generator's own decision), reversed going down.
///
/// # Errors
/// `RunRefused` when the migrations directory cannot be read, or when its
/// snapshot carries a hint the generator refuses (a hand-edited `ir.json`).
fn step_table_renames(step: &PlannedStep, down: bool) -> PyResult<Vec<Hint>> {
    let shown = format!("{}/{}", step.migration_name, step.file);
    let directory = step
        .path
        .parent()
        .and_then(std::path::Path::parent)
        .ok_or_else(|| {
            refused(format!(
                "ferro migrate: cannot find the migrations directory of {shown}. Nothing was \
                 applied."
            ))
        })?;
    let dir = MigrationsDir::read(directory)
        .map_err(|err| refused(format!("ferro migrate: {err}. Nothing was applied.")))?;
    let snapshot_of = |number: u16| {
        dir.migrations
            .iter()
            .find(|migration| migration.number == number)
            .map(|migration| migration.snapshot.ir.payload.clone())
    };
    let Some(own) = snapshot_of(step.migration) else {
        return Ok(Vec::new());
    };
    let parent = step
        .migration
        .checked_sub(1)
        .and_then(snapshot_of)
        .unwrap_or(SchemaIrPayload {
            dialect_agnostic: own.dialect_agnostic,
            models: Vec::new(),
        });
    let hints = live_hints(&parent, &own).map_err(|err| {
        refused(format!(
            "ferro migrate: {shown}: its schema snapshot carries a rename hint ferro refuses: \
             {err}. Nothing was applied."
        ))
    })?;
    Ok(if down { reverse_hints(&hints) } else { hints })
}

// -- the run lock --------------------------------------------------------------------

enum LockState {
    /// A session advisory lock on a dedicated connection taken out of the pool.
    Postgres {
        conn: Option<sqlx::PgConnection>,
        key: i64,
        verified: bool,
    },
    /// An OS file lock on `<database>.ferro-migrate.lock`, held while the file is open.
    SqliteFile { file: Option<std::fs::File> },
    /// An in-memory database: one process, an in-process lock.
    Memory { name: String },
}

/// The run lock (ADR-0029, ADR-0038). Released by [`RunLock::release`], and
/// by the database or the operating system when the process dies.
pub struct RunLock {
    state: LockState,
}

static MEMORY_LOCKS: Lazy<std::sync::Mutex<HashSet<String>>> =
    Lazy::new(|| std::sync::Mutex::new(HashSet::new()));

/// The fault a poisoned in-process lock registry names, instead of reading
/// as "held" and waiting out the timeout.
const POISONED_MEMORY_LOCKS: &str = "ferro migrate: the in-process run lock registry is \
     unusable: a thread panicked while holding it. Restart the process. Nothing was applied.";

/// Take the in-process lock `name` in `registry`: `true` when taken, `false`
/// when another run holds it.
///
/// # Errors
/// A refusal naming the fault when the registry is poisoned.
fn take_memory_lock(registry: &std::sync::Mutex<HashSet<String>>, name: &str) -> PyResult<bool> {
    registry
        .lock()
        .map(|mut held| held.insert(name.to_string()))
        .map_err(|_| refused(POISONED_MEMORY_LOCKS))
}

/// Whether the in-process lock `name` in `registry` is held.
///
/// # Errors
/// A refusal naming the fault when the registry is poisoned.
fn memory_lock_held(registry: &std::sync::Mutex<HashSet<String>>, name: &str) -> PyResult<bool> {
    registry
        .lock()
        .map(|held| held.contains(name))
        .map_err(|_| refused(POISONED_MEMORY_LOCKS))
}

/// What a SQLite database's lock is: a sidecar file, or (in memory) a name.
enum SqliteTarget {
    File(PathBuf),
    Memory(String),
}

async fn sqlite_target(engine: &EngineHandle) -> PyResult<SqliteTarget> {
    let rows = engine
        .fetch_all_sql_unprepared("PRAGMA database_list")
        .await
        .map_err(|e| db_error("reading the database file", e))?;
    let file = rows
        .iter()
        .find(|row| text(column(row, 1)).as_deref() == Some("main"))
        .and_then(|row| text(column(row, 2)))
        .unwrap_or_default();
    if file.is_empty() {
        let name = engine
            .sqlite_pool()
            .map(|pool| pool.connect_options().get_filename().display().to_string())
            .unwrap_or_default();
        return Ok(SqliteTarget::Memory(name));
    }
    Ok(SqliteTarget::File(PathBuf::from(format!(
        "{file}.ferro-migrate.lock"
    ))))
}

fn lock_ids(key: i64) -> (i64, i64) {
    // pg_locks shows a bigint advisory key as classid (high half) and objid
    // (low half), with objsubid = 1.
    let bits = key as u64;
    ((bits >> 32) as i64, (bits & 0xffff_ffff) as i64)
}

const HELD_BY_THIS_SESSION: &str = "SELECT EXISTS (SELECT 1 FROM pg_locks \
     WHERE locktype = 'advisory' AND granted AND objsubid = 1 \
     AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) \
     AND classid::bigint = $1 AND objid::bigint = $2 AND pid = pg_backend_pid())";

const HELD_BY_ANY_SESSION: &str = "SELECT EXISTS (SELECT 1 FROM pg_locks \
     WHERE locktype = 'advisory' AND granted AND objsubid = 1 \
     AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) \
     AND classid::bigint = $1 AND objid::bigint = $2)";

impl RunLock {
    /// Take the run lock for `governed_schema` (the connection's current
    /// schema when `None`), waiting up to `timeout`. `on_wait` is called once,
    /// at once, with [`WAITING_TEXT`] when the lock is held by another run.
    ///
    /// # Errors
    /// A refusal naming the timeout; the pooler refusal when the Postgres
    /// lock cannot be verified on its own session; a database error.
    pub async fn acquire(
        engine: &EngineHandle,
        governed_schema: Option<&str>,
        timeout: Duration,
        on_wait: impl FnOnce(&str),
    ) -> PyResult<RunLock> {
        let deadline = Instant::now() + timeout;
        let mut on_wait = Some(on_wait);
        let mut waited = || {
            if let Some(callback) = on_wait.take()
                && !timeout.is_zero()
            {
                callback(WAITING_TEXT);
            }
            Instant::now() < deadline
        };
        match engine.backend() {
            Dialect::Postgres => {
                let governed = match governed_schema {
                    Some(schema) => schema.to_string(),
                    None => governed_schema_of(engine).await?,
                };
                let key = run_lock_key(&governed);
                let pool = engine
                    .postgres_pool()
                    .ok_or_else(|| refused("ferro migrate: the engine has no Postgres pool"))?;
                let mut conn = pool
                    .acquire()
                    .await
                    .map_err(|e| db_error("opening the run lock's connection", e))?
                    .detach();
                loop {
                    let taken: bool = sqlx::query("SELECT pg_try_advisory_lock($1)")
                        .bind(key)
                        .persistent(false)
                        .fetch_one(&mut conn)
                        .await
                        .and_then(|row| row.try_get(0))
                        .map_err(|e| db_error("taking the run lock", e))?;
                    if taken {
                        break;
                    }
                    if !waited() {
                        let _ = conn.close().await;
                        return Err(refused(lock_timeout_text(timeout)));
                    }
                    tokio::time::sleep(LOCK_POLL).await;
                }
                let mut lock = RunLock {
                    state: LockState::Postgres {
                        conn: Some(conn),
                        key,
                        verified: false,
                    },
                };
                if let Err(err) = lock.verify().await {
                    lock.release().await?;
                    return Err(err);
                }
                Ok(lock)
            }
            Dialect::Sqlite => match sqlite_target(engine).await? {
                SqliteTarget::File(path) => {
                    let file = std::fs::OpenOptions::new()
                        .create(true)
                        .truncate(false)
                        .write(true)
                        .open(&path)
                        .map_err(|e| {
                            refused(format!(
                                "ferro migrate: cannot open the run lock file {}: {e}. Nothing \
                                 was applied.",
                                path.display()
                            ))
                        })?;
                    loop {
                        match file.try_lock() {
                            Ok(()) => break,
                            Err(std::fs::TryLockError::WouldBlock) => {
                                if !waited() {
                                    return Err(refused(lock_timeout_text(timeout)));
                                }
                                tokio::time::sleep(LOCK_POLL).await;
                            }
                            Err(std::fs::TryLockError::Error(e)) => {
                                return Err(refused(format!(
                                    "ferro migrate: cannot lock {}: {e}. Nothing was applied.",
                                    path.display()
                                )));
                            }
                        }
                    }
                    Ok(RunLock {
                        state: LockState::SqliteFile { file: Some(file) },
                    })
                }
                SqliteTarget::Memory(name) => {
                    loop {
                        if take_memory_lock(&MEMORY_LOCKS, &name)? {
                            break;
                        }
                        if !waited() {
                            return Err(refused(lock_timeout_text(timeout)));
                        }
                        tokio::time::sleep(LOCK_POLL).await;
                    }
                    Ok(RunLock {
                        state: LockState::Memory { name },
                    })
                }
            },
        }
    }

    /// Check that this run still holds the lock: on Postgres, ask its own
    /// session; a file or in-process lock cannot be lost while held.
    ///
    /// # Errors
    /// The pooler refusal when the first check after acquiring answers "not
    /// held", the dropped-lock refusal when a later one does or its session
    /// is gone; the database's own error when it refuses the probe.
    pub async fn verify(&mut self) -> PyResult<()> {
        let LockState::Postgres {
            conn,
            key,
            verified,
        } = &mut self.state
        else {
            return Ok(());
        };
        let first = !*verified;
        let held = match conn.as_mut() {
            None => false,
            Some(conn) => {
                let (classid, objid) = lock_ids(*key);
                let probe = sqlx::query(HELD_BY_THIS_SESSION)
                    .bind(classid)
                    .bind(objid)
                    .persistent(false)
                    .fetch_one(conn)
                    .await
                    .and_then(|row| row.try_get::<bool, _>(0));
                match probe {
                    Ok(held) => held,
                    Err(err) if !first && probe_lost_the_session(&err) => false,
                    Err(err) => return Err(db_error("verifying the run lock", err)),
                }
            }
        };
        lock_verification_outcome(held, first).map_err(|r| refused(r.to_string()))?;
        *verified = true;
        Ok(())
    }

    /// Release the lock and close its connection.
    ///
    /// # Errors
    /// The poisoned-registry refusal for an in-process lock; a lock whose
    /// connection is already gone was released by the server.
    pub async fn release(mut self) -> PyResult<()> {
        match &mut self.state {
            LockState::Postgres { conn, key, .. } => {
                if let Some(mut conn) = conn.take() {
                    let _ = sqlx::query("SELECT pg_advisory_unlock($1)")
                        .bind(*key)
                        .persistent(false)
                        .execute(&mut conn)
                        .await;
                    let _ = conn.close().await;
                }
            }
            LockState::SqliteFile { file } => {
                if let Some(file) = file.take() {
                    let _ = file.unlock();
                }
            }
            LockState::Memory { name } => {
                MEMORY_LOCKS
                    .lock()
                    .map_err(|_| refused(POISONED_MEMORY_LOCKS))?
                    .remove(name);
            }
        }
        Ok(())
    }

    /// A Postgres lock on a fresh connection of `engine` that never took the
    /// advisory lock, not yet verified: what a transaction-mode pooler hands
    /// back. Tests drive the pooler refusal (the first check) with it.
    ///
    /// # Errors
    /// A refusal for a non-Postgres engine; a database error.
    pub async fn unacquired_for_test(engine: &EngineHandle) -> PyResult<RunLock> {
        let governed = governed_schema_of(engine).await?;
        let pool = engine
            .postgres_pool()
            .ok_or_else(|| refused("ferro migrate: the engine has no Postgres pool"))?;
        let conn = pool
            .acquire()
            .await
            .map_err(|e| db_error("opening a connection", e))?
            .detach();
        Ok(RunLock {
            state: LockState::Postgres {
                conn: Some(conn),
                key: run_lock_key(&governed),
                verified: false,
            },
        })
    }

    /// Close the lock's connection without releasing it first, as a dropped
    /// network connection would. Tests drive the dropped-lock refusal with it.
    pub async fn close_connection(&mut self) {
        if let LockState::Postgres { conn, .. } = &mut self.state
            && let Some(conn) = conn.take()
        {
            let _ = conn.close().await;
        }
    }

    /// Whether any run holds the lock for `governed_schema` (the current
    /// schema when `None`), without taking it: `status`'s `running` probe.
    ///
    /// # Errors
    /// A database error.
    pub async fn is_held(engine: &EngineHandle, governed_schema: Option<&str>) -> PyResult<bool> {
        match engine.backend() {
            Dialect::Postgres => {
                let governed = match governed_schema {
                    Some(schema) => schema.to_string(),
                    None => governed_schema_of(engine).await?,
                };
                let (classid, objid) = lock_ids(run_lock_key(&governed));
                let rows = engine
                    .fetch_all_sql_unprepared_with_binds(
                        HELD_BY_ANY_SESSION,
                        &[EngineBindValue::I64(classid), EngineBindValue::I64(objid)],
                    )
                    .await
                    .map_err(|e| db_error("probing the run lock", e))?;
                Ok(rows.first().and_then(|row| int(column(row, 0))) == Some(1))
            }
            Dialect::Sqlite => match sqlite_target(engine).await? {
                SqliteTarget::File(path) => {
                    let Ok(file) = std::fs::OpenOptions::new().write(true).open(&path) else {
                        return Ok(false);
                    };
                    match file.try_lock() {
                        Ok(()) => {
                            let _ = file.unlock();
                            Ok(false)
                        }
                        Err(_) => Ok(true),
                    }
                }
                SqliteTarget::Memory(name) => memory_lock_held(&MEMORY_LOCKS, &name),
            },
        }
    }
}

async fn governed_schema_of(engine: &EngineHandle) -> PyResult<String> {
    governed_schema(engine).await
}

/// Locks held across FFI calls, by handle.
static RUN_LOCKS: Lazy<std::sync::Mutex<HashMap<u64, Arc<tokio::sync::Mutex<RunLock>>>>> =
    Lazy::new(|| std::sync::Mutex::new(HashMap::new()));
static NEXT_HANDLE: AtomicU64 = AtomicU64::new(1);

/// Keep `lock` across FFI calls; returns its handle.
pub fn register_lock(lock: RunLock) -> PyResult<u64> {
    let handle = NEXT_HANDLE.fetch_add(1, Ordering::Relaxed);
    RUN_LOCKS
        .lock()
        .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("run lock registry poisoned"))?
        .insert(handle, Arc::new(tokio::sync::Mutex::new(lock)));
    Ok(handle)
}

/// The lock behind `handle`.
pub fn registered_lock(handle: u64) -> PyResult<Arc<tokio::sync::Mutex<RunLock>>> {
    RUN_LOCKS
        .lock()
        .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("run lock registry poisoned"))?
        .get(&handle)
        .cloned()
        .ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err(format!("no run lock with handle {handle}"))
        })
}

const LOCK_IN_USE: &str =
    "the run lock is in use by another call; release it after that call returns";

/// Take the lock behind `handle` out of `registry` for release, only once
/// no other call holds it: a release refused while the lock is in use
/// leaves the handle registered, so it can be retried.
///
/// # Errors
/// `RuntimeError` while another call holds the lock (the handle stays) or
/// on a poisoned registry; `ValueError` for an unknown handle.
fn take_registered_lock(
    registry: &std::sync::Mutex<HashMap<u64, Arc<tokio::sync::Mutex<RunLock>>>>,
    handle: u64,
) -> PyResult<RunLock> {
    let mut locks = registry
        .lock()
        .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("run lock registry poisoned"))?;
    let unknown =
        || pyo3::exceptions::PyValueError::new_err(format!("no run lock with handle {handle}"));
    // Every clone is made from the registry under this mutex, so a count of
    // one cannot rise before the removal below.
    if Arc::strong_count(locks.get(&handle).ok_or_else(unknown)?) != 1 {
        return Err(pyo3::exceptions::PyRuntimeError::new_err(LOCK_IN_USE));
    }
    let lock = locks.remove(&handle).ok_or_else(unknown)?;
    Arc::try_unwrap(lock)
        .map(tokio::sync::Mutex::into_inner)
        .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err(LOCK_IN_USE))
}

/// Forget `handle` and return its lock for release; while another call
/// holds the lock, refuse and keep the handle, so the release can be retried.
///
/// # Errors
/// As [`take_registered_lock`].
pub fn unregister_lock(handle: u64) -> PyResult<RunLock> {
    take_registered_lock(&RUN_LOCKS, handle)
}

// -- the tracking tables ------------------------------------------------------------

fn tracking_ddl(dialect: Dialect, tracking: &Tracking) -> [String; 2] {
    let (timestamp, bigint, boolean) = match dialect {
        Dialect::Postgres => ("TIMESTAMPTZ", "BIGINT", "BOOLEAN NOT NULL DEFAULT FALSE"),
        Dialect::Sqlite => ("TEXT", "INTEGER", "INTEGER NOT NULL DEFAULT 0"),
    };
    [
        format!(
            "CREATE TABLE IF NOT EXISTS {} (\n\
             \x20   migration          INTEGER     NOT NULL,\n\
             \x20   step               INTEGER     NOT NULL,\n\
             \x20   migration_name     TEXT        NOT NULL,\n\
             \x20   file               TEXT        NOT NULL,\n\
             \x20   kind               TEXT        NOT NULL,\n\
             \x20   checksum           TEXT        NOT NULL,\n\
             \x20   snapshot_checksum  TEXT        NOT NULL,\n\
             \x20   started_at         {timestamp} NOT NULL,\n\
             \x20   finished_at        {timestamp},\n\
             \x20   failed_at          {timestamp},\n\
             \x20   error              TEXT,\n\
             \x20   resume_cursor      TEXT,\n\
             \x20   rows_done          {bigint},\n\
             \x20   duration_ms        {bigint} NOT NULL DEFAULT 0,\n\
             \x20   ferro_version      TEXT        NOT NULL,\n\
             \x20   origin             TEXT        NOT NULL,\n\
             \x20   reverting          {boolean},\n\
             \x20   revert_cursor      TEXT,\n\
             \x20   PRIMARY KEY (migration, step)\n)",
            tracking.table(TRACKING_TABLE)
        ),
        format!(
            "CREATE TABLE IF NOT EXISTS {} (format INTEGER NOT NULL, governed_schema TEXT NOT NULL)",
            tracking.table(FORMAT_TABLE)
        ),
    ]
}

async fn schema_exists(engine: &EngineHandle, schema: &str) -> PyResult<bool> {
    let rows = engine
        .fetch_all_sql_unprepared_with_binds(
            "SELECT 1 FROM pg_namespace WHERE nspname = $1",
            &[EngineBindValue::String(schema.to_string())],
        )
        .await
        .map_err(|e| db_error("reading the catalog", e))?;
    Ok(!rows.is_empty())
}

async fn refuse_missing_schema(engine: &EngineHandle, tracking: &Tracking) -> PyResult<()> {
    if let Some(schema) = &tracking.schema
        && !schema_exists(engine, schema).await?
    {
        return Err(refused(format!(
            "ferro migrate: tracking_schema {} does not exist on this database. Create it \
             first:\n  CREATE SCHEMA {};\nNothing was applied.",
            quote_ident(schema),
            quote_ident(schema)
        )));
    }
    Ok(())
}

/// Create `_ferro_migrations` and `_ferro_migrations_format` (with its one
/// row: format 1 and the governed schema) where they are missing, in
/// `tracking_schema` when set. Run under the run lock by the first mutating
/// verb.
///
/// # Errors
/// A refusal naming `CREATE SCHEMA` when `tracking_schema` does not exist;
/// the newer-format refusal; a database error.
pub async fn ensure_tracking_tables(
    engine: &EngineHandle,
    tracking_schema: Option<&str>,
) -> PyResult<()> {
    let dialect = engine.backend();
    let tracking = Tracking::new(dialect, tracking_schema);
    refuse_missing_schema(engine, &tracking).await?;
    let governed = governed_schema(engine).await?;
    let mut conn = engine
        .begin_transaction_connection()
        .await
        .map_err(|e| db_error("creating the tracking tables", e))?;
    let result: Result<(), sqlx::Error> = async {
        for ddl in tracking_ddl(dialect, &tracking) {
            crate::log_debug(format!("ferro migrate: {ddl}"));
            conn.execute_sql_unprepared(&ddl).await?;
        }
        let format_table = tracking.table(FORMAT_TABLE);
        let present = conn
            .fetch_all_sql_unprepared_with_binds(&format!("SELECT format FROM {format_table}"), &[])
            .await?;
        if present.is_empty() {
            conn.fetch_all_sql_unprepared_with_binds(
                &format!(
                    "INSERT INTO {format_table} (format, governed_schema) VALUES ({}, {})",
                    param(dialect, 1),
                    param(dialect, 2)
                ),
                &[
                    EngineBindValue::I64(TRACKING_FORMAT),
                    EngineBindValue::String(governed.clone()),
                ],
            )
            .await?;
        }
        conn.commit().await
    }
    .await;
    if let Err(err) = result {
        let _ = conn.rollback().await;
        return Err(db_error("creating the tracking tables", err));
    }
    Ok(())
}

/// What [`read_records`] found.
#[derive(Clone, Debug, serde::Serialize)]
pub struct TrackingState {
    /// `schema._ferro_migrations`, as `status` names it.
    pub table: String,
    /// Whether the tracking table exists.
    pub exists: bool,
    /// The format number, when the format table exists.
    pub format: Option<i64>,
    /// The connection's governed schema.
    pub governed_schema: String,
    /// The step records, by migration and step.
    pub records: Vec<StepRecord>,
    /// The newer-format refusal (#466), when the format is one this ferro
    /// does not understand: every verb stops on it, `status` reports it.
    pub refusal: Option<String>,
}

const RECORD_COLUMNS: &str = "migration, step, migration_name, file, kind, checksum, \
     snapshot_checksum, started_at, finished_at, failed_at, error, resume_cursor, rows_done, \
     duration_ms, ferro_version, origin, reverting, revert_cursor";

fn record_from_row(row: &EngineRow) -> PyResult<StepRecord> {
    let bad = |what: &str| {
        refused(format!(
            "ferro migrate: the tracking table holds a row ferro cannot read ({what}). Nothing \
             was applied."
        ))
    };
    let get = |i: usize| column(row, i);
    Ok(StepRecord {
        migration: int(get(0))
            .and_then(|v| u16::try_from(v).ok())
            .ok_or_else(|| bad("migration"))?,
        step: int(get(1))
            .and_then(|v| u8::try_from(v).ok())
            .ok_or_else(|| bad("step"))?,
        migration_name: text(get(2)).ok_or_else(|| bad("migration_name"))?,
        file: text(get(3)).ok_or_else(|| bad("file"))?,
        kind: text(get(4))
            .and_then(|k| RecordKind::parse(&k))
            .ok_or_else(|| bad("kind"))?,
        checksum: text(get(5)).ok_or_else(|| bad("checksum"))?,
        snapshot_checksum: text(get(6)).ok_or_else(|| bad("snapshot_checksum"))?,
        started_at: text(get(7)).unwrap_or_default(),
        finished_at: text(get(8)),
        failed_at: text(get(9)),
        error: text(get(10)),
        resume_cursor: text(get(11)),
        rows_done: int(get(12)),
        duration_ms: int(get(13)).unwrap_or(0),
        ferro_version: text(get(14)).unwrap_or_default(),
        origin: text(get(15))
            .and_then(|o| Origin::parse(&o))
            .ok_or_else(|| bad("origin"))?,
        reverting: int(get(16)).unwrap_or(0) != 0,
        revert_cursor: text(get(17)),
    })
}

/// Read the format table first, then every step record (#466: every verb
/// reads the format first). Creates nothing and takes no lock: a database
/// without the tables holds no records.
///
/// A newer format is returned as [`TrackingState::refusal`] (naming the
/// ferro that last migrated the database), so `status` can report it.
///
/// # Errors
/// A database error, or a row this format should hold that ferro cannot read.
pub async fn read_records(
    engine: &EngineHandle,
    tracking_schema: Option<&str>,
) -> PyResult<TrackingState> {
    let dialect = engine.backend();
    let tracking = Tracking::new(dialect, tracking_schema);
    let governed = governed_schema(engine).await?;
    let shown_schema = tracking.schema.clone().unwrap_or_else(|| governed.clone());
    let mut state = TrackingState {
        table: format!("{shown_schema}.{TRACKING_TABLE}"),
        exists: false,
        format: None,
        governed_schema: governed,
        records: Vec::new(),
        refusal: None,
    };
    if tracking.schema.is_some() && !schema_exists(engine, &shown_schema).await? {
        return Ok(state);
    }
    if table_exists(engine, &tracking, FORMAT_TABLE).await? {
        let rows = engine
            .fetch_all_sql_unprepared(&format!(
                "SELECT format FROM {}",
                tracking.table(FORMAT_TABLE)
            ))
            .await
            .map_err(|e| db_error("reading the tracking table's format", e))?;
        state.format = rows.first().and_then(|row| int(column(row, 0)));
    }
    state.exists = table_exists(engine, &tracking, TRACKING_TABLE).await?;
    if state.exists {
        let rows = engine
            .fetch_all_sql_unprepared(&format!(
                "SELECT {RECORD_COLUMNS} FROM {} ORDER BY migration, step",
                tracking.table(TRACKING_TABLE)
            ))
            .await;
        let newer = state.format.unwrap_or(TRACKING_FORMAT) > TRACKING_FORMAT;
        match rows {
            // A newer format's rows are read only to name the ferro that wrote
            // them; one this ferro cannot read is skipped, the refusal stands.
            Ok(rows) if newer => {
                state.records = rows
                    .iter()
                    .filter_map(|r| record_from_row(r).ok())
                    .collect();
            }
            Ok(rows) => {
                state.records = rows.iter().map(record_from_row).collect::<PyResult<_>>()?;
            }
            Err(_) if newer => {}
            Err(err) => return Err(db_error("reading the step records", err)),
        }
    }
    if let Some(format) = state.format {
        state.refusal = check_format(format, &state.records, env!("CARGO_PKG_VERSION"))
            .err()
            .map(|r| r.to_string());
    }
    Ok(state)
}

fn record_binds(record: &StepRecord) -> Vec<EngineBindValue> {
    let opt = |value: &Option<String>| match value {
        Some(v) => EngineBindValue::String(v.clone()),
        None => EngineBindValue::Null(NullKind::String),
    };
    vec![
        EngineBindValue::I64(i64::from(record.migration)),
        EngineBindValue::I64(i64::from(record.step)),
        EngineBindValue::String(record.migration_name.clone()),
        EngineBindValue::String(record.file.clone()),
        EngineBindValue::String(record.kind.as_str().to_string()),
        EngineBindValue::String(record.checksum.clone()),
        EngineBindValue::String(record.snapshot_checksum.clone()),
        EngineBindValue::String(record.started_at.clone()),
        opt(&record.finished_at),
        opt(&record.failed_at),
        opt(&record.error),
        opt(&record.resume_cursor),
        match record.rows_done {
            Some(rows) => EngineBindValue::I64(rows),
            None => EngineBindValue::Null(NullKind::I64),
        },
        EngineBindValue::I64(record.duration_ms),
        EngineBindValue::String(record.ferro_version.clone()),
        EngineBindValue::String(record.origin.as_str().to_string()),
        EngineBindValue::Bool(record.reverting),
        opt(&record.revert_cursor),
    ]
}

/// The upsert that writes one record. On conflict it keeps the first
/// `started_at` and adds `duration_ms` to the time already spent, so a
/// resumed step's duration sums across runs (#466).
fn upsert_sql(dialect: Dialect, tracking: &Tracking) -> String {
    let ts = |n: usize| match dialect {
        Dialect::Postgres => format!("CAST(${n} AS TIMESTAMPTZ)"),
        Dialect::Sqlite => "?".to_string(),
    };
    let p = |n: usize| param(dialect, n);
    let values = [
        p(1),
        p(2),
        p(3),
        p(4),
        p(5),
        p(6),
        p(7),
        ts(8),
        ts(9),
        ts(10),
        p(11),
        p(12),
        p(13),
        p(14),
        p(15),
        p(16),
        p(17),
        p(18),
    ]
    .join(", ");
    let updated = [
        "migration_name",
        "file",
        "kind",
        "checksum",
        "snapshot_checksum",
        "finished_at",
        "failed_at",
        "error",
        "resume_cursor",
        "rows_done",
        "ferro_version",
        "origin",
        "reverting",
        "revert_cursor",
    ]
    .iter()
    .map(|c| format!("{c} = excluded.{c}"))
    .collect::<Vec<_>>()
    .join(", ");
    format!(
        "INSERT INTO {} AS t ({RECORD_COLUMNS}) VALUES ({values}) \
         ON CONFLICT (migration, step) DO UPDATE SET {updated}, \
         duration_ms = t.duration_ms + excluded.duration_ms",
        tracking.table(TRACKING_TABLE)
    )
}

/// Write one step record, on `in_tx` when given (so it commits with the
/// step) or on its own otherwise.
///
/// # Errors
/// A database error.
pub async fn write_record(
    engine: &EngineHandle,
    tracking_schema: Option<&str>,
    record: &StepRecord,
    in_tx: Option<&mut EngineConnection>,
) -> PyResult<()> {
    let dialect = engine.backend();
    let sql = upsert_sql(dialect, &Tracking::new(dialect, tracking_schema));
    let binds = record_binds(record);
    match in_tx {
        Some(conn) => conn.fetch_all_sql_unprepared_with_binds(&sql, &binds).await,
        None => {
            engine
                .fetch_all_sql_unprepared_with_binds(&sql, &binds)
                .await
        }
    }
    .map(|_| ())
    .map_err(|e| db_error("writing the step record", e))
}

/// Every tracking table whose format table governs `schema` (the current
/// schema when `None`), found through the catalog (ADR-0038) — what the
/// `connect()` guard (#521) refuses on.
#[derive(Clone, Debug, serde::Serialize)]
pub struct TrackingTable {
    /// The schema the tracking tables live in.
    pub schema: String,
    /// The schema they govern.
    pub governed_schema: String,
    /// Their format number.
    pub format: i64,
}

/// See [`TrackingTable`].
///
/// # Errors
/// A database error.
pub async fn tracking_tables_for(
    engine: &EngineHandle,
    schema: Option<&str>,
) -> PyResult<Vec<TrackingTable>> {
    let wanted = match schema {
        Some(schema) => schema.to_string(),
        None => governed_schema(engine).await?,
    };
    let homes: Vec<Option<String>> = match engine.backend() {
        Dialect::Sqlite => {
            if table_exists(engine, &Tracking { schema: None }, FORMAT_TABLE).await? {
                vec![None]
            } else {
                Vec::new()
            }
        }
        Dialect::Postgres => engine
            .fetch_all_sql_unprepared_with_binds(
                "SELECT table_schema::text FROM information_schema.tables WHERE table_name = $1",
                &[EngineBindValue::String(FORMAT_TABLE.to_string())],
            )
            .await
            .map_err(|e| db_error("reading the catalog", e))?
            .iter()
            .map(|row| text(column(row, 0)))
            .collect(),
    };
    let mut found = Vec::new();
    for home in homes {
        let tracking = Tracking {
            schema: home.clone(),
        };
        let rows = engine
            .fetch_all_sql_unprepared(&format!(
                "SELECT format, governed_schema FROM {}",
                tracking.table(FORMAT_TABLE)
            ))
            .await
            .map_err(|e| db_error("reading a tracking table's format", e))?;
        for row in rows {
            let governed = text(column(&row, 1)).unwrap_or_default();
            if governed == wanted {
                found.push(TrackingTable {
                    schema: home.clone().unwrap_or_else(|| "main".to_string()),
                    governed_schema: governed,
                    format: int(column(&row, 0)).unwrap_or(0),
                });
            }
        }
    }
    Ok(found)
}

// -- executing a SQL step ---------------------------------------------------------------

/// What one step's execution came to.
#[derive(Clone, Debug, serde::Serialize)]
pub struct StepOutcome {
    /// Whether the step finished.
    pub ok: bool,
    /// Milliseconds the attempt took.
    pub ms: i64,
    /// The error recorded on the step, when it failed.
    pub error: Option<String>,
    /// The operator's text for a failure: the step, the error, what stayed
    /// applied and how to resume.
    pub message: Option<String>,
}

fn error_text(err: &sqlx::Error) -> String {
    match err {
        sqlx::Error::Database(db) => db.message().to_string(),
        other => other.to_string(),
    }
}

/// A failure inside a step: its text, for the record and the operator, and
/// what a failed validate or unique index step was stopped by, which the
/// runner counts after the failure (ADR-0043, ADR-0044).
struct StepFailure(String, Option<crate::errors::CountedFailure>);

impl From<sqlx::Error> for StepFailure {
    fn from(err: sqlx::Error) -> Self {
        StepFailure(error_text(&err), None)
    }
}

impl From<StatementError> for StepFailure {
    fn from(err: StatementError) -> Self {
        let counted = crate::errors::counted_failure_of_error(err.statement.as_deref(), &err.error);
        StepFailure(error_text(&err.error), counted)
    }
}

impl From<DdlError<StatementError>> for StepFailure {
    fn from(err: DdlError<StatementError>) -> Self {
        match err {
            DdlError::LockTimeout(timeout) => StepFailure(timeout.to_string(), None),
            DdlError::Failed(failure) => failure.into(),
        }
    }
}

fn log_step_statement(file: &str, statement: &str) {
    crate::log_debug(format!("ferro migrate: {file}: {statement}"));
}

async fn run_statements(
    conn: &mut EngineConnection,
    statements: &[String],
    file: &str,
) -> Result<(), StatementError> {
    for statement in statements {
        log_step_statement(file, statement);
        conn.execute_sql_unprepared(statement)
            .await
            .map_err(|error| StatementError::at(statement, error))?;
    }
    Ok(())
}

/// `PRAGMA foreign_key_check`'s rows as one readable line each.
fn describe_fk_violations(rows: &[EngineRow]) -> String {
    rows.iter()
        .map(|row| {
            format!(
                "table \"{}\" rowid {} references \"{}\" (foreign key {})",
                text(column(row, 0)).unwrap_or_default(),
                text(column(row, 1)).unwrap_or_else(|| "NULL".to_string()),
                text(column(row, 2)).unwrap_or_default(),
                text(column(row, 3)).unwrap_or_default(),
            )
        })
        .collect::<Vec<_>>()
        .join("; ")
}

/// What a step's own transaction does to its record when the step succeeds:
/// going up, write the finished record; going down, remove it.
enum Settle {
    Write(Box<StepRecord>),
    Remove { migration: u16, step: u8 },
}

async fn settle(
    conn: &mut EngineConnection,
    tracking_schema: Option<&str>,
    settle: Settle,
) -> Result<(), sqlx::Error> {
    match settle {
        Settle::Write(record) => {
            let dialect = conn.dialect();
            conn.fetch_all_sql_unprepared_with_binds(
                &upsert_sql(dialect, &Tracking::new(dialect, tracking_schema)),
                &record_binds(&record),
            )
            .await
            .map(|_| ())
        }
        Settle::Remove { migration, step } => {
            remove_record(conn, tracking_schema, migration, step).await
        }
    }
}

/// Remove the record of `(migration, step)` on `tx` — inside the transaction
/// of the step's down, so the record goes exactly when the down commits
/// (ADR-0033: the tracking table says where the database stands now).
///
/// # Errors
/// A database error.
pub async fn remove_record(
    tx: &mut EngineConnection,
    tracking_schema: Option<&str>,
    migration: u16,
    step: u8,
) -> Result<(), sqlx::Error> {
    let dialect = tx.dialect();
    let sql = format!(
        "DELETE FROM {} WHERE migration = {} AND step = {}",
        Tracking::new(dialect, tracking_schema).table(TRACKING_TABLE),
        param(dialect, 1),
        param(dialect, 2)
    );
    tx.fetch_all_sql_unprepared_with_binds(
        &sql,
        &[
            EngineBindValue::I64(i64::from(migration)),
            EngineBindValue::I64(i64::from(step)),
        ],
    )
    .await
    .map(|_| ())
}

/// The statement that commits one batch of a chunked step on its record
/// (ADR-0024): going up the batch's cursor lands in `resume_cursor`, going
/// down in `revert_cursor` with `reverting` set (ADR-0033). Either way
/// `rows_done` counts the rows this walk has committed, and the last
/// attempt's failure is cleared: a committed batch supersedes it.
fn write_cursor_sql(dialect: Dialect, tracking: &Tracking, reverting: bool) -> String {
    let p = |n: usize| param(dialect, n);
    let cursor = if reverting {
        let truth = match dialect {
            Dialect::Postgres => "TRUE",
            Dialect::Sqlite => "1",
        };
        format!("reverting = {truth}, revert_cursor = {}", p(1))
    } else {
        format!("resume_cursor = {}", p(1))
    };
    format!(
        "UPDATE {} SET {cursor}, rows_done = {}, failed_at = NULL, error = NULL \
         WHERE migration = {} AND step = {} RETURNING migration",
        tracking.table(TRACKING_TABLE),
        p(2),
        p(3),
        p(4)
    )
}

/// Commit a chunked step's batch on its record, on `tx` — inside the batch's
/// own transaction, so the cursor commits with the batch's rows or not at
/// all (ADR-0024). `cursor_json` is the batch's cursor
/// (`{"keys": [...], "rows_done": N}`, `None` before the first row), and
/// `reverting` says which walk it belongs to (see [`write_cursor_sql`]).
///
/// # Errors
/// A refusal when the step has no record (its started record is written
/// before its first batch); a database error.
pub async fn write_cursor(
    tx: &mut EngineConnection,
    tracking_schema: Option<&str>,
    migration: u16,
    step: u8,
    cursor_json: Option<&str>,
    rows_done: i64,
    reverting: bool,
) -> PyResult<()> {
    let dialect = tx.dialect();
    let sql = write_cursor_sql(dialect, &Tracking::new(dialect, tracking_schema), reverting);
    let rows = tx
        .fetch_all_sql_unprepared_with_binds(
            &sql,
            &[
                match cursor_json {
                    Some(cursor) => EngineBindValue::String(cursor.to_string()),
                    None => EngineBindValue::Null(NullKind::String),
                },
                EngineBindValue::I64(rows_done),
                EngineBindValue::I64(i64::from(migration)),
                EngineBindValue::I64(i64::from(step)),
            ],
        )
        .await
        .map_err(|e| db_error("writing the chunked step's cursor", e))?;
    if rows.is_empty() {
        return Err(refused(format!(
            "ferro migrate: {migration:04}:{step:02} has no step record to carry its cursor; \
             a chunked step's record is written before its first batch. Nothing more was run."
        )));
    }
    Ok(())
}

/// The statement `rerecord` runs: the record's `file`, `checksum` and
/// `kind`, and with `restart` its cursor cleared and `rows_done` zeroed,
/// guarded on the checksum the plan read so a record that moved since is
/// not rewritten.
fn rerecord_sql(dialect: Dialect, tracking: &Tracking, restart: bool) -> String {
    let p = |n: usize| param(dialect, n);
    let cursor = if restart {
        ", resume_cursor = NULL, rows_done = 0"
    } else {
        ""
    };
    format!(
        "UPDATE {} SET file = {}, checksum = {}, kind = {}{cursor} \
         WHERE migration = {} AND step = {} AND checksum = {} RETURNING migration",
        tracking.table(TRACKING_TABLE),
        p(1),
        p(2),
        p(3),
        p(4),
        p(5),
        p(6)
    )
}

/// Rewrite one step record as [`ferro_migrate::run_plan::rerecord_plan`]
/// planned it (ADR-0030): its `file`, `checksum` and `kind`, and with
/// `action.clear_cursor` (`--restart`) its `resume_cursor` and `rows_done`
/// — one statement, so it lands whole or not at all. Runs nothing of the
/// step. Called under the run lock.
///
/// # Errors
/// A refusal when the record no longer holds the checksum the plan read; a
/// database error.
pub async fn rerecord_checksum(
    engine: &EngineHandle,
    tracking_schema: Option<&str>,
    action: &ferro_migrate::run_plan::RerecordAction,
) -> PyResult<()> {
    let dialect = engine.backend();
    let sql = rerecord_sql(
        dialect,
        &Tracking::new(dialect, tracking_schema),
        action.clear_cursor,
    );
    let rows = engine
        .fetch_all_sql_unprepared_with_binds(
            &sql,
            &[
                EngineBindValue::String(action.file.clone()),
                EngineBindValue::String(action.new_checksum.clone()),
                EngineBindValue::String(action.kind.as_str().to_string()),
                EngineBindValue::I64(i64::from(action.migration)),
                EngineBindValue::I64(i64::from(action.step)),
                EngineBindValue::String(action.old_checksum.clone()),
            ],
        )
        .await
        .map_err(|e| db_error("re-recording the step", e))?;
    if rows.is_empty() {
        return Err(refused(format!(
            "ferro migrate: the record of {:04}:{:02} changed while rerecord ran; run `ferro \
             migrate status` and try again. Nothing was changed.",
            action.migration, action.step
        )));
    }
    Ok(())
}

async fn foreign_keys_off(
    conn: &mut EngineConnection,
    statements: &[String],
    file: &str,
    tracking_schema: Option<&str>,
    finished: impl FnOnce() -> Settle,
) -> Result<(), StepFailure> {
    conn.execute_sql_unprepared("PRAGMA foreign_keys = OFF")
        .await?;
    let read = conn
        .fetch_all_sql_unprepared_with_binds("PRAGMA foreign_keys", &[])
        .await?;
    if read.first().and_then(|row| int(column(row, 0))) != Some(0) {
        return Err(StepFailure(
            "PRAGMA foreign_keys = OFF did not take effect on the step's connection".to_string(),
            None,
        ));
    }
    conn.execute_sql_unprepared("BEGIN IMMEDIATE").await?;
    run_statements(conn, statements, file).await?;
    let violations = conn
        .fetch_all_sql_unprepared_with_binds("PRAGMA foreign_key_check", &[])
        .await?;
    if !violations.is_empty() {
        return Err(StepFailure(
            format!(
                "PRAGMA foreign_key_check found rows that violate a foreign key: {}",
                describe_fk_violations(&violations)
            ),
            None,
        ));
    }
    settle(conn, tracking_schema, finished()).await?;
    conn.execute_sql_unprepared("COMMIT").await?;
    Ok(())
}

/// A foreign-keys-off step's outcome and whether its connection must be
/// closed, given the step (through `COMMIT`) and the `PRAGMA foreign_keys =
/// ON` restore after it. The restore comes after the commit, so its failure
/// never turns a committed step into a failed one (that would overwrite a
/// finished record and re-run the step); it only keeps a connection with
/// foreign keys off out of the pool.
fn foreign_keys_off_outcome(
    step: Result<(), StepFailure>,
    restore: Result<(), sqlx::Error>,
) -> (Result<(), StepFailure>, bool) {
    match (step, restore) {
        (Ok(()), Ok(())) => (Ok(()), false),
        (Ok(()), Err(_)) => (Ok(()), true),
        (Err(failure), _) => (Err(failure), true),
    }
}

/// Whether a failed attempt writes `failed_at` and the error on the step's
/// record: always going up (the started record marks where `up` resumes);
/// going down only when the failure left part of the down applied, which a
/// no-transaction down does. A transactional down rolled back whole, so the
/// step stands exactly as applied and its record is left as it was.
fn failed_down_marks_record(down: bool, mode: ExecMode) -> bool {
    !down || mode == ExecMode::NoTransaction
}

fn failure_message(step: &PlannedStep, error: &str, down: bool) -> String {
    let after = match (step.mode, down) {
        (ExecMode::NoTransaction, false) => {
            "It runs without a transaction, so the statements before the failing one stay \
             applied; fix the file or the database and run `ferro migrate up` again: the step \
             re-runs from its first statement."
        }
        (ExecMode::Transactional | ExecMode::ForeignKeysOff, false) => {
            "The step was rolled back; fix the file or the database and run `ferro migrate up` \
             again to resume at it."
        }
        (ExecMode::NoTransaction, true) => {
            "It runs without a transaction, so the statements before the failing one stay \
             reverted, and the step's record stands; fix the file or the database and run \
             `ferro migrate down` again: the down re-runs from its first statement."
        }
        (ExecMode::Transactional | ExecMode::ForeignKeysOff, true) => {
            "The down was rolled back and the step's record stands; fix the file or the \
             database and run `ferro migrate down` again to resume at it."
        }
    };
    format!(
        "ferro migrate: {}/{} failed: {error}\n{after} Nothing after it ran.",
        step.migration_name, step.file
    )
}

/// Execute one planned SQL step in the step's mode, and settle its record:
/// going up the record is written, going down it is removed.
///
/// - **Transactional**: going up, the started record commits first (so
///   `status` sees the attempt); then one transaction runs the file's
///   statements and writes the finished mark (up) or removes the record
///   (down), and commits them together.
/// - **NoTransaction** (Postgres): (up: the started record, then) each
///   statement in autocommit (what lets `CREATE INDEX CONCURRENTLY` run),
///   then the finished mark or the removal.
/// - **ForeignKeysOff** (SQLite): one dedicated connection —
///   `PRAGMA foreign_keys = OFF` (read back), `BEGIN IMMEDIATE`, the file,
///   `PRAGMA foreign_key_check` as a failure, the finished mark or the
///   removal, `COMMIT`, the pragma restored; the connection is closed on
///   failure.
///
/// The transactional and no-transaction modes run under `ddl`, the DDL lock
/// timeout (ADR-0044): on Postgres a statement that waits longer than the
/// timeout for a lock gives up and the step is re-run from its first
/// statement, `on_attempt` hearing each attempt that timed out; the record
/// stays started (`running`) across attempts, and the finished record's
/// `duration_ms` covers every attempt and wait. After the last attempt the
/// step fails with a message naming `ddl_lock_timeout`.
///
/// A failure rolls back what the mode can roll back, then writes `failed_at`
/// and `error` on the step's record in a separate transaction: going up the
/// started record; going down the standing one, only when the down ran
/// without a transaction and so left part of itself applied (a rolled-back
/// down leaves the record exactly as it was: the step stays applied, see
/// [`failed_down_marks_record`]). The next run resumes at the step. A down with [`PlannedStep::nothing_to_reverse`] runs
/// no statement and removes the record. `sql` must be the bytes the planner
/// hashed (the up file going up, the down file going down).
///
/// # Errors
/// A refusal when `sql` is not the planned file (edited mid-run), when the
/// record is not the planned step's, or when a down declaring
/// `nothing-to-reverse` holds statements; a database error writing a
/// record. A failing statement is not an error: it is the returned
/// [`StepOutcome`].
#[allow(clippy::too_many_arguments)]
pub async fn execute_sql_step(
    engine: &EngineHandle,
    tracking_schema: Option<&str>,
    step: &PlannedStep,
    sql: &str,
    record: StepRecord,
    direction: Direction,
    ddl: &DdlExecutor,
    on_attempt: impl FnMut(Attempt),
) -> PyResult<StepOutcome> {
    let down = matches!(direction, Direction::Down { .. });
    let verb = if down { "down" } else { "up" };
    let shown = format!("{}/{}", step.migration_name, step.file);
    if encode_checksum(&sha384(sql.as_bytes())) != step.checksum {
        return Err(refused(format!(
            "ferro migrate: {shown} changed while this run was in progress; it was planned \
             with sha384:{}. Run `ferro migrate {verb}` again. Nothing more was {}.",
            step.checksum,
            if down { "reverted" } else { "applied" }
        )));
    }
    let planned_record = if down {
        (record.migration, record.step, &record.checksum)
            == (step.migration, step.step, &step.record.checksum)
    } else {
        (record.migration, record.step, &record.checksum)
            == (step.migration, step.step, &step.checksum)
    };
    if !planned_record {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "the record for {}:{} does not describe the planned step {shown}",
            record.migration, record.step
        )));
    }
    let mut statements = split_statements(sql, engine.backend());
    if down && step.nothing_to_reverse.is_some() {
        if step.headers.nothing_to_reverse.is_some() && !statements.is_empty() {
            return Err(refused(format!(
                "ferro migrate: {shown} declares nothing-to-reverse but holds statements; keep \
                 one: delete the statements, or the declaration. Nothing more was reverted."
            )));
        }
        statements.clear();
    }

    let started = if down {
        record
    } else {
        let started = StepRecord {
            started_at: now_iso(),
            finished_at: None,
            failed_at: None,
            error: None,
            duration_ms: 0,
            kind: step.mode.record_kind(),
            ..record
        };
        write_record(engine, tracking_schema, &started, None).await?;
        started
    };

    let clock = Instant::now();
    let elapsed = |clock: Instant| i64::try_from(clock.elapsed().as_millis()).unwrap_or(i64::MAX);
    let finish = |ms: i64| {
        if down {
            Settle::Remove {
                migration: started.migration,
                step: started.step,
            }
        } else {
            Settle::Write(Box::new(StepRecord {
                finished_at: Some(now_iso()),
                duration_ms: ms,
                ..started.clone()
            }))
        }
    };

    // One attempt of a transactional or no-transaction step: the file's
    // statements, then the record settled on the same connection (inside the
    // transaction, for a transactional step). The executor wraps it in the
    // DDL lock timeout and re-runs it from the first statement on a timeout.
    let (statements, shown, finish, elapsed) = (&statements, &shown, &finish, &elapsed);
    let attempt = |mut conn: EngineConnection| async move {
        let result = async {
            run_statements(&mut conn, statements, shown).await?;
            settle(&mut conn, tracking_schema, finish(elapsed(clock))).await?;
            Ok::<(), StatementError>(())
        }
        .await;
        (conn, result)
    };
    let log = |sql: &str| log_step_statement(shown, sql);
    let outcome: Result<(), StepFailure> = match step.mode {
        ExecMode::Transactional => ddl
            .transactional(engine, log, on_attempt, attempt)
            .await
            .map_err(StepFailure::from),
        ExecMode::NoTransaction => ddl
            .unwrapped(engine, log, on_attempt, attempt)
            .await
            .map_err(StepFailure::from),
        ExecMode::ForeignKeysOff => match pool_connection(engine).await {
            Err(err) => Err(err.into()),
            Ok(mut conn) => {
                let step = foreign_keys_off(&mut conn, statements, shown, tracking_schema, || {
                    finish(elapsed(clock))
                })
                .await;
                let restore = if step.is_ok() {
                    conn.execute_sql_unprepared("PRAGMA foreign_keys = ON")
                        .await
                        .map(|_| ())
                } else {
                    let _ = conn.execute_sql_unprepared("ROLLBACK").await;
                    Ok(())
                };
                if let Err(err) = &restore {
                    crate::log_debug(format!(
                        "ferro migrate: {shown} committed, but restoring PRAGMA foreign_keys = \
                         ON on its connection failed ({err}); closing the connection"
                    ));
                }
                let (result, close) = foreign_keys_off_outcome(step, restore);
                if close {
                    let _ = conn.detach_and_close().await;
                }
                result
            }
        },
    };
    let ms = elapsed(clock);
    match outcome {
        Ok(()) => {
            engine
                .refresh_pool()
                .await
                .map_err(|e| db_error("refreshing the pool after the step", e))?;
            Ok(StepOutcome {
                ok: true,
                ms,
                error: None,
                message: None,
            })
        }
        Err(StepFailure(error, counted)) => {
            // A failed validate or unique step names its count and where
            // `up` resumes; counted only now, never on the success path.
            let error = match counted {
                Some(failure) if !down => {
                    let resume_at = format!("{}:{:02}", step.migration_name, step.step);
                    // A contract's recipe re-runs its migration's backfill.
                    let rerun = crate::errors::backfill_rerun_step(&step.path)
                        .map(|before_backfill| (step.migration, before_backfill));
                    crate::errors::counted_failure_message(
                        engine, &failure, &error, &resume_at, rerun,
                    )
                    .await
                }
                _ => error,
            };
            // A down that rolled back changed nothing, so its record does not
            // change either (the tracking table says where the database stands
            // now): the step stays applied and the error is the run's to
            // report. A no-transaction down left the statements before the
            // failing one reverted, so its record carries the failure.
            if failed_down_marks_record(down, step.mode) {
                let failed = StepRecord {
                    failed_at: Some(now_iso()),
                    error: Some(error.clone()),
                    // The upsert adds this to the time already recorded; a
                    // failed down adds nothing to the time the step took to
                    // apply.
                    duration_ms: if down { 0 } else { ms },
                    ..started
                };
                write_record(engine, tracking_schema, &failed, None).await?;
            }
            Ok(StepOutcome {
                ok: false,
                ms,
                message: Some(failure_message(step, &error, down)),
                error: Some(error),
            })
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_first_failed_check_is_a_pooler_and_a_later_one_a_dropped_lock() {
        assert_eq!(lock_verification_outcome(true, true), Ok(()));
        assert_eq!(lock_verification_outcome(true, false), Ok(()));
        assert_eq!(
            lock_verification_outcome(false, true),
            Err(RunLockRefusal::Pooler)
        );
        assert_eq!(
            lock_verification_outcome(false, false),
            Err(RunLockRefusal::Dropped)
        );
        assert!(
            RunLockRefusal::Pooler
                .to_string()
                .contains("migrations need a direct or session-mode connection")
        );
        assert!(
            RunLockRefusal::Dropped
                .to_string()
                .contains("the run lock was lost")
        );
    }

    #[test]
    fn only_a_broken_session_reads_as_a_lost_lock_and_a_refused_probe_is_its_own_error() {
        let io = || sqlx::Error::Io(std::io::Error::other("connection reset"));
        assert!(probe_lost_the_session(&io()));
        assert!(probe_lost_the_session(&sqlx::Error::PoolClosed));
        // The database answered: whatever it said is reported as itself,
        // never as a pooler or a dropped lock.
        assert!(!probe_lost_the_session(&sqlx::Error::RowNotFound));
        assert!(!probe_lost_the_session(&sqlx::Error::ColumnNotFound(
            "exists".to_string()
        )));
    }

    #[test]
    fn a_poisoned_in_process_registry_names_the_fault_instead_of_reading_as_held() {
        let registry = std::sync::Mutex::new(HashSet::new());
        assert!(take_memory_lock(&registry, "db").is_ok_and(|taken| taken));
        assert!(take_memory_lock(&registry, "db").is_ok_and(|taken| !taken));
        assert!(memory_lock_held(&registry, "db").is_ok_and(|held| held));
        let _ = std::panic::catch_unwind(|| {
            let _guard = registry.lock();
            panic!("poison the registry");
        });
        Python::attach(|py| {
            for err in [
                take_memory_lock(&registry, "other").err(),
                memory_lock_held(&registry, "db").err(),
            ] {
                let text = err.map(|e| e.value(py).to_string()).unwrap_or_default();
                assert!(text.contains("run lock registry is unusable"), "{text}");
            }
        });
    }

    #[test]
    fn a_release_refused_while_the_lock_is_in_use_keeps_the_handle_for_a_retry() {
        let registry = std::sync::Mutex::new(HashMap::new());
        let lock = RunLock {
            state: LockState::Memory {
                name: "test".to_string(),
            },
        };
        registry
            .lock()
            .map(|mut locks| locks.insert(7, Arc::new(tokio::sync::Mutex::new(lock))))
            .ok();
        let in_use = registry
            .lock()
            .ok()
            .and_then(|locks| locks.get(&7).cloned());
        assert!(take_registered_lock(&registry, 7).is_err());
        assert!(registry.lock().is_ok_and(|locks| locks.contains_key(&7)));
        drop(in_use);
        assert!(take_registered_lock(&registry, 7).is_ok());
        assert!(registry.lock().is_ok_and(|locks| locks.is_empty()));
        assert!(take_registered_lock(&registry, 7).is_err());
    }

    #[test]
    fn a_failed_pragma_restore_after_commit_keeps_the_step_committed_and_closes_the_connection() {
        let restore_failed = || Err(sqlx::Error::PoolClosed);
        let (outcome, close) = foreign_keys_off_outcome(Ok(()), restore_failed());
        assert!(outcome.is_ok() && close);
        let (outcome, close) = foreign_keys_off_outcome(Ok(()), Ok(()));
        assert!(outcome.is_ok() && !close);
        let (outcome, close) =
            foreign_keys_off_outcome(Err(StepFailure("boom".to_string(), None)), Ok(()));
        assert!(outcome.is_err() && close);
    }

    #[test]
    fn a_rebuild_refusal_names_each_object_per_table_and_nothing_when_clean() {
        assert_eq!(rebuild_refusal("post", &[]), None);
        let found = [
            "index \"my_idx\": drop it".to_string(),
            "trigger \"t\": drop it".to_string(),
        ];
        assert_eq!(
            rebuild_refusal("post", &found).as_deref(),
            Some(
                "ferro migrate: a SQLite rebuild of table \"post\" copies only the columns and \
                 recreates only the indexes the schema snapshot declares, and the live table \
                 also holds:\n  - index \"my_idx\": drop it\n  - trigger \"t\": drop it\n\
                 Nothing was applied."
            )
        );
    }

    #[test]
    fn a_bigint_key_splits_into_the_halves_pg_locks_shows() {
        assert_eq!(lock_ids(0x0000_0001_0000_0002), (1, 2));
        assert_eq!(lock_ids(-1), (0xffff_ffff, 0xffff_ffff));
    }

    #[test]
    fn the_timeout_names_itself() {
        assert!(lock_timeout_text(Duration::from_secs(1)).contains("lock timeout (1s)"));
        assert!(lock_timeout_text(Duration::from_millis(500)).contains("(500ms)"));
    }

    #[test]
    fn the_tracking_table_has_the_466_columns_plus_origin_and_the_reserved_revert_ones() {
        let [table, format] = tracking_ddl(
            Dialect::Postgres,
            &Tracking::new(Dialect::Postgres, Some("audit")),
        );
        assert!(table.starts_with("CREATE TABLE IF NOT EXISTS \"audit\".\"_ferro_migrations\""));
        for column in RECORD_COLUMNS.split(", ") {
            assert!(table.contains(&format!("    {column} ")), "{column}");
        }
        assert!(table.contains("started_at         TIMESTAMPTZ NOT NULL"));
        assert!(table.contains("PRIMARY KEY (migration, step)"));
        assert_eq!(
            format,
            "CREATE TABLE IF NOT EXISTS \"audit\".\"_ferro_migrations_format\" (format INTEGER NOT NULL, governed_schema TEXT NOT NULL)"
        );
        let [sqlite, _] = tracking_ddl(
            Dialect::Sqlite,
            &Tracking::new(Dialect::Sqlite, Some("ignored")),
        );
        assert!(sqlite.starts_with("CREATE TABLE IF NOT EXISTS \"_ferro_migrations\""));
        assert!(sqlite.contains("started_at         TEXT NOT NULL"));
        assert!(sqlite.contains("rows_done          INTEGER,"));
    }

    #[test]
    fn a_batch_cursor_goes_to_its_walks_column_and_clears_the_last_failure() {
        let pg = Tracking::new(Dialect::Postgres, Some("audit"));
        assert_eq!(
            write_cursor_sql(Dialect::Postgres, &pg, false),
            "UPDATE \"audit\".\"_ferro_migrations\" SET resume_cursor = $1, rows_done = $2, \
             failed_at = NULL, error = NULL WHERE migration = $3 AND step = $4 RETURNING migration"
        );
        assert_eq!(
            write_cursor_sql(Dialect::Postgres, &pg, true),
            "UPDATE \"audit\".\"_ferro_migrations\" SET reverting = TRUE, revert_cursor = $1, \
             rows_done = $2, failed_at = NULL, error = NULL WHERE migration = $3 AND step = $4 \
             RETURNING migration"
        );
        let sqlite = Tracking::new(Dialect::Sqlite, None);
        assert_eq!(
            write_cursor_sql(Dialect::Sqlite, &sqlite, true),
            "UPDATE \"_ferro_migrations\" SET reverting = 1, revert_cursor = ?, rows_done = ?, \
             failed_at = NULL, error = NULL WHERE migration = ? AND step = ? RETURNING migration"
        );
    }

    #[test]
    fn only_a_down_that_left_part_of_itself_applied_marks_its_record() {
        for mode in [
            ExecMode::Transactional,
            ExecMode::NoTransaction,
            ExecMode::ForeignKeysOff,
        ] {
            assert!(failed_down_marks_record(false, mode), "every failed up");
        }
        assert!(failed_down_marks_record(true, ExecMode::NoTransaction));
        assert!(!failed_down_marks_record(true, ExecMode::Transactional));
        assert!(!failed_down_marks_record(true, ExecMode::ForeignKeysOff));
    }
}

// -- baseline (#525) ---------------------------------------------------------------------
//
// ```text
// $ ferro migrate baseline 0002
// recorded 0001_create_author … 0002_add_teams as baseline (3 steps)
// ```
//
// For that line the runner held the run lock, found no step records, checked
// the database against `0002_add_teams`'s snapshot (the drift check, in
// Python) and, the check being empty, wrote the [`plan_baseline`] records in
// one transaction ([`write_baseline_records`]). `baseline --remove` deletes
// them again ([`remove_baseline_records`]) while no run has applied anything
// above them (ADR-0031).

use ferro_migrate::directory::{MigrationsDir, StepKind};
use ferro_migrate::run_plan::{RunRefusal, exec_mode, file_name, step_file};
use ferro_schema_ir::{IrEnvelope, SchemaIrPayload};

/// What `ferro migrate baseline` records: one finished record per step of
/// every migration through the target, and what it tells the operator.
#[derive(Clone, Debug, serde::Serialize)]
pub struct BaselinePlan {
    /// `NNNN_<name>` of the target: the migration whose snapshot the
    /// database is checked against.
    pub target: String,
    /// The target's schema snapshot.
    pub snapshot: IrEnvelope<SchemaIrPayload>,
    /// The records to write, by migration and step: finished, `origin =
    /// baseline`, `duration_ms = 0`, `started_at = finished_at`.
    pub records: Vec<StepRecord>,
    /// `NNNN_<name>` of every migration recorded, in order.
    pub recorded: Vec<String>,
    /// `NNNN_<name>/<file>` of every data step recorded without running.
    pub data_steps: Vec<String>,
}

fn baseline_directory_label(dir: &MigrationsDir) -> String {
    dir.path
        .file_name()
        .map(|name| name.to_string_lossy().to_string())
        .unwrap_or_else(|| dir.path.display().to_string())
}

/// The baseline of `dir` through `target` (`None`: the head; `"0006"`, `"6"`
/// or `"0006_add_teams"`) on a `dialect` database whose tracking table holds
/// `existing`: each record carries the file this dialect runs and its
/// checksum, exactly as `up` would have recorded it, stamped `now`.
///
/// # Errors
/// The operator's text, ending in what was recorded (nothing): a database
/// that already has a record (naming `ferro migrate status`); a target the
/// directory lacks (naming the directory); an empty directory; a step with
/// no rendering for `dialect`, or headers it cannot honour.
pub fn plan_baseline(
    dir: &MigrationsDir,
    existing: &[StepRecord],
    dialect: Dialect,
    target: Option<&str>,
    now: &str,
    ferro_version: &str,
) -> Result<BaselinePlan, String> {
    const NOTHING: &str = "Nothing was recorded.";
    if !existing.is_empty() {
        let names: std::collections::BTreeSet<&str> =
            existing.iter().map(|r| r.migration_name.as_str()).collect();
        return Err(format!(
            "ferro migrate baseline: this database already has migration records ({}); \
             baseline records migrations only on a database that has none. Run `ferro migrate \
             status` to see where it stands. {NOTHING}",
            names.into_iter().collect::<Vec<_>>().join(", ")
        ));
    }
    let label = baseline_directory_label(dir);
    let Some(head) = dir.migrations.last() else {
        return Err(format!(
            "ferro migrate baseline: {label}/ holds no migration to record; generate the first \
             with `ferro migrate new <name>`. {NOTHING}"
        ));
    };
    let chosen = match target.map(str::trim) {
        None => head,
        Some(wanted) => dir
            .migrations
            .iter()
            .find(|m| {
                m.dir_name() == wanted
                    || (wanted.chars().all(|c| c.is_ascii_digit())
                        && wanted.parse::<u16>().ok() == Some(m.number))
            })
            .ok_or_else(|| {
                format!(
                    "ferro migrate baseline: {wanted} names no migration in {label}/: give a \
                     migration number from 0001 to {:04}, or a migration's full name ({}). \
                     {NOTHING}",
                    head.number,
                    head.dir_name()
                )
            })?,
    };
    let mut plan = BaselinePlan {
        target: chosen.dir_name(),
        snapshot: chosen.snapshot.ir.clone(),
        records: Vec::new(),
        recorded: Vec::new(),
        data_steps: Vec::new(),
    };
    for migration in dir
        .migrations
        .iter()
        .take_while(|m| m.number <= chosen.number)
    {
        let snapshot_checksum = encode_checksum(&migration.snapshot.checksum);
        for step in &migration.steps {
            // The file this dialect runs, by the run planner's own rule: the
            // file `up` checks every applied record against.
            let file = step_file(migration, step, dialect)
                .map_err(|missing| format!("ferro migrate baseline: {missing} {NOTHING}"))?;
            let name = file_name(&file.up);
            let shown = format!("{}/{name}", migration.dir_name());
            let kind = if step.kind == StepKind::Data {
                plan.data_steps.push(shown);
                RecordKind::Atomic
            } else {
                exec_mode(&file.headers, dialect, &shown)
                    .map_err(|refusal| match refusal {
                        RunRefusal::BadHeaders { file, reason } => {
                            format!("ferro migrate baseline: {file}: {reason}. {NOTHING}")
                        }
                        other => format!("ferro migrate baseline: {other}"),
                    })?
                    .record_kind()
            };
            plan.records.push(StepRecord {
                migration: migration.number,
                step: step.ordinal,
                migration_name: migration.dir_name(),
                file: name,
                kind,
                checksum: encode_checksum(&file.up_checksum),
                snapshot_checksum: snapshot_checksum.clone(),
                started_at: now.to_string(),
                finished_at: Some(now.to_string()),
                failed_at: None,
                error: None,
                resume_cursor: None,
                rows_done: None,
                duration_ms: 0,
                ferro_version: ferro_version.to_string(),
                origin: Origin::Baseline,
                reverting: false,
                revert_cursor: None,
            });
        }
        plan.recorded.push(migration.dir_name());
    }
    Ok(plan)
}

/// [`plan_baseline`] stamped with the current time.
///
/// # Errors
/// See [`plan_baseline`].
pub fn plan_baseline_now(
    dir: &MigrationsDir,
    existing: &[StepRecord],
    dialect: Dialect,
    target: Option<&str>,
    ferro_version: &str,
) -> Result<BaselinePlan, String> {
    plan_baseline(dir, existing, dialect, target, &now_iso(), ferro_version)
}

/// Which records `baseline --remove` deletes: every baseline-origin record,
/// by migration and step — none when there is no baseline.
///
/// # Errors
/// The operator's text when a run-origin record stands above the highest
/// baselined migration, naming each such migration and the `down` that
/// reverts it: removing the baseline under them would leave applied
/// migrations above pending ones.
pub fn baseline_removal(records: &[StepRecord]) -> Result<Vec<(u16, u8)>, String> {
    let baselined = || records.iter().filter(|r| r.origin == Origin::Baseline);
    let Some(floor) = baselined().max_by_key(|r| r.migration) else {
        return Ok(Vec::new());
    };
    let above: std::collections::BTreeSet<&str> = records
        .iter()
        .filter(|r| r.origin == Origin::Run && r.migration > floor.migration)
        .map(|r| r.migration_name.as_str())
        .collect();
    if !above.is_empty() {
        let (verb, pronoun) = if above.len() == 1 {
            ("was", "it")
        } else {
            ("were", "them")
        };
        return Err(format!(
            "ferro migrate baseline --remove: {} {verb} applied by a run above the baseline at \
             {}. Revert {pronoun} first with `ferro migrate down --to {:04}`, then remove the \
             baseline. Nothing was removed.",
            above.into_iter().collect::<Vec<_>>().join(", "),
            floor.migration_name,
            floor.migration
        ));
    }
    Ok(baselined().map(|r| (r.migration, r.step)).collect())
}

/// Write a baseline's records ([`plan_baseline`]) in one transaction,
/// creating the tracking tables first where they are missing. Called under
/// the run lock, after the drift check found nothing.
///
/// # Errors
/// A refusal for a record that is not a finished baseline record (nothing is
/// written); a database error, after which nothing is written either.
pub async fn write_baseline_records(
    engine: &EngineHandle,
    tracking_schema: Option<&str>,
    records: &[StepRecord],
) -> PyResult<()> {
    if let Some(record) = records
        .iter()
        .find(|r| r.origin != Origin::Baseline || !r.is_finished() || r.error.is_some())
    {
        return Err(refused(format!(
            "ferro migrate baseline: {} is not a finished baseline record; nothing was \
             recorded.",
            record.path()
        )));
    }
    ensure_tracking_tables(engine, tracking_schema).await?;
    let dialect = engine.backend();
    let sql = upsert_sql(dialect, &Tracking::new(dialect, tracking_schema));
    let mut conn = engine
        .begin_transaction_connection()
        .await
        .map_err(|e| db_error("writing the baseline records", e))?;
    let result: Result<(), sqlx::Error> = async {
        for record in records {
            conn.fetch_all_sql_unprepared_with_binds(&sql, &record_binds(record))
                .await?;
        }
        conn.commit().await
    }
    .await;
    if let Err(err) = result {
        let _ = conn.rollback().await;
        return Err(db_error("writing the baseline records", err));
    }
    Ok(())
}

/// Delete every baseline-origin record ([`baseline_removal`] decides), in
/// one statement. Returns the `(migration, step)` of each record removed.
/// Called under the run lock.
///
/// # Errors
/// The newer-format refusal; the run-origin-above refusal (nothing is
/// removed); a database error.
pub async fn remove_baseline_records(
    engine: &EngineHandle,
    tracking_schema: Option<&str>,
) -> PyResult<Vec<(u16, u8)>> {
    let state = read_records(engine, tracking_schema).await?;
    if let Some(refusal) = state.refusal {
        return Err(refused(refusal));
    }
    let removed = baseline_removal(&state.records).map_err(refused)?;
    if removed.is_empty() {
        return Ok(removed);
    }
    let dialect = engine.backend();
    engine
        .fetch_all_sql_unprepared_with_binds(
            &format!(
                "DELETE FROM {} WHERE origin = {}",
                Tracking::new(dialect, tracking_schema).table(TRACKING_TABLE),
                param(dialect, 1)
            ),
            &[EngineBindValue::String(
                Origin::Baseline.as_str().to_string(),
            )],
        )
        .await
        .map_err(|e| db_error("removing the baseline records", e))?;
    Ok(removed)
}

#[cfg(test)]
mod baseline_tests {
    use super::*;
    use ferro_migrate::directory::{
        Headers, Migration, MigrationsDir, Step, StepDialect, StepFile, StepKind,
    };
    use ferro_migrate::run_plan::{RunRefusal, StepState, plan_run, run_status};
    use ferro_migrate::snapshot::Snapshot;
    use ferro_schema_ir::{IrEnvelope, SchemaIrPayload, SchemaModel};
    use std::collections::BTreeMap;

    const NOW: &str = "2026-10-06T09:00:00.000000Z";

    fn ir(tables: &[&str]) -> IrEnvelope<SchemaIrPayload> {
        IrEnvelope {
            ir_kind: "schema".into(),
            ir_version: 1,
            payload: SchemaIrPayload {
                dialect_agnostic: true,
                models: tables
                    .iter()
                    .map(|t| SchemaModel {
                        renamed_from: None,
                        model_name: t.to_string(),
                        table_name: t.to_string(),
                        columns: Vec::new(),
                        foreign_keys: Vec::new(),
                        indexes: Vec::new(),
                        uniques: Vec::new(),
                        checks: Vec::new(),
                        table_checks: Vec::new(),
                        row_security: None,
                    })
                    .collect(),
            },
        }
    }

    fn file(name: &str, body: &str) -> StepFile {
        StepFile {
            up: PathBuf::from(format!("/m/{name}")),
            down: Some(PathBuf::from(format!("/m/{name}.down"))),
            up_checksum: sha384(body.as_bytes()),
            headers: Headers::parse(body).unwrap_or_default(),
            down_checksum: Some(sha384(b"SELECT 1;\n")),
            down_headers: Headers::default(),
        }
    }

    /// A generated step with one rendering per dialect.
    fn ddl(ordinal: u8, body: &str) -> Step {
        let mut files = BTreeMap::new();
        for (dialect, suffix) in [
            (StepDialect::Postgres, "postgres"),
            (StepDialect::Sqlite, "sqlite"),
        ] {
            let name = format!("{ordinal:02}_schema.up.{suffix}.sql");
            files.insert(dialect, file(&name, &format!("{body}-- {name}\n")));
        }
        Step {
            ordinal,
            name: "schema".into(),
            kind: StepKind::Ddl,
            files,
        }
    }

    /// A hand-placed data step (`NN_backfill.py`).
    fn data(ordinal: u8) -> Step {
        let mut py = file(&format!("{ordinal:02}_backfill.py"), "# data\n");
        py.down = None;
        py.down_checksum = None;
        Step {
            ordinal,
            name: "backfill".into(),
            kind: StepKind::Data,
            files: BTreeMap::from([(StepDialect::Portable, py)]),
        }
    }

    fn dir(migrations: Vec<(&str, Vec<Step>, &[&str])>) -> MigrationsDir {
        let mut out: Vec<Migration> = Vec::new();
        for (i, (name, steps, tables)) in migrations.into_iter().enumerate() {
            let parent = out.last().map(|m| m.snapshot.checksum);
            let bytes = Snapshot::store(&ir(tables), parent).expect("store");
            out.push(Migration {
                number: u16::try_from(i + 1).expect("number"),
                name: name.to_string(),
                dir: PathBuf::from(format!("/migrations/{:04}_{name}", i + 1)),
                steps,
                snapshot: Snapshot::load(&bytes).expect("load"),
            });
        }
        MigrationsDir {
            path: PathBuf::from("/proj/migrations"),
            migrations: out,
        }
    }

    /// `0001` creates `author` and carries a data step; `0002` and `0003`
    /// add a table each.
    fn three() -> MigrationsDir {
        dir(vec![
            (
                "create_author",
                vec![ddl(1, "CREATE TABLE a (id int);\n"), data(2)],
                &["author"],
            ),
            (
                "add_teams",
                vec![ddl(1, "CREATE TABLE t (id int);\n")],
                &["author", "team"],
            ),
            (
                "add_orgs",
                vec![ddl(1, "CREATE TABLE o (id int);\n")],
                &["author", "team", "org"],
            ),
        ])
    }

    fn records(dir: &MigrationsDir, target: &str) -> Vec<StepRecord> {
        plan_baseline(dir, &[], Dialect::Sqlite, Some(target), NOW, "v")
            .expect("plan")
            .records
    }

    #[test]
    fn a_baseline_records_every_step_up_to_the_target_as_finished_baseline_rows() {
        let dir = three();
        let plan =
            plan_baseline(&dir, &[], Dialect::Sqlite, Some("0002"), NOW, "0.30.0").expect("plan");
        assert_eq!(plan.target, "0002_add_teams");
        assert_eq!(plan.recorded, ["0001_create_author", "0002_add_teams"]);
        assert_eq!(plan.data_steps, ["0001_create_author/02_backfill.py"]);
        assert_eq!(plan.snapshot, dir.migrations[1].snapshot.ir);
        let keys: Vec<(u16, u8)> = plan.records.iter().map(|r| (r.migration, r.step)).collect();
        assert_eq!(keys, [(1, 1), (1, 2), (2, 1)]);
        for record in &plan.records {
            assert_eq!(record.origin, Origin::Baseline);
            assert_eq!(record.duration_ms, 0);
            assert_eq!(record.started_at, NOW);
            assert_eq!(record.finished_at.as_deref(), Some(NOW));
            assert_eq!(record.ferro_version, "0.30.0");
            assert_eq!(record.error, None);
        }
        assert_eq!(plan.records[0].file, "01_schema.up.sqlite.sql");
        assert_eq!(plan.records[1].file, "02_backfill.py");
        // Nothing ran, so a data step's kind steers nothing; a finished
        // record is never resumed. A DDL step's is its mode's.
        let kinds: Vec<RecordKind> = plan.records.iter().map(|r| r.kind).collect();
        assert_eq!(
            kinds,
            [RecordKind::Ddl, RecordKind::Atomic, RecordKind::Ddl]
        );

        // What `up` and `status` make of those records: everything through
        // the target applied (baseline), only 0003 left to apply.
        let up = plan_run(
            &dir,
            &plan.records,
            Dialect::Sqlite,
            Direction::Up,
            false,
            None,
        )
        .expect("up plans");
        let pending: Vec<(u16, u8)> = up.steps.iter().map(|s| (s.migration, s.step)).collect();
        assert_eq!(pending, [(3, 1)]);
        let status = run_status(&dir, &plan.records, Dialect::Sqlite, false, None);
        let states: Vec<Vec<StepState>> = status
            .migrations
            .iter()
            .map(|m| m.steps.iter().map(|s| s.state).collect())
            .collect();
        assert_eq!(
            states,
            [
                vec![StepState::AppliedBaseline, StepState::AppliedBaseline],
                vec![StepState::AppliedBaseline],
                vec![StepState::Pending],
            ]
        );
    }

    #[test]
    fn the_target_defaults_to_the_head_and_reads_a_number_or_a_full_name() {
        let dir = three();
        let plan = |target| plan_baseline(&dir, &[], Dialect::Postgres, target, NOW, "v");
        assert_eq!(plan(None).expect("head").target, "0003_add_orgs");
        assert_eq!(
            plan(Some("1")).expect("number").target,
            "0001_create_author"
        );
        assert_eq!(
            plan(Some("0002_add_teams")).expect("name").recorded,
            ["0001_create_author", "0002_add_teams"]
        );
        assert_eq!(
            plan(None).expect("head").records[0].file,
            "01_schema.up.postgres.sql"
        );
    }

    #[test]
    fn a_target_the_directory_lacks_is_refused_naming_the_directory() {
        let migrations = three();
        for target in ["0009", "0002_add_tames", "latest"] {
            let refusal = plan_baseline(&migrations, &[], Dialect::Sqlite, Some(target), NOW, "v")
                .expect_err("refused");
            assert_eq!(
                refusal,
                format!(
                    "ferro migrate baseline: {target} names no migration in migrations/: \
                     give a migration number from 0001 to 0003, or a migration's full name \
                     (0003_add_orgs). Nothing was recorded."
                )
            );
        }
        let empty = dir(Vec::new());
        assert_eq!(
            plan_baseline(&empty, &[], Dialect::Sqlite, None, NOW, "v").expect_err("empty"),
            "ferro migrate baseline: migrations/ holds no migration to record; generate the \
             first with `ferro migrate new <name>`. Nothing was recorded."
        );
    }

    #[test]
    fn a_database_with_any_record_is_refused_naming_status() {
        let dir = three();
        let mut existing = records(&dir, "0001");
        existing[1].origin = Origin::Run;
        existing[1].finished_at = None;
        let refusal =
            plan_baseline(&dir, &existing, Dialect::Sqlite, None, NOW, "v").expect_err("refused");
        assert_eq!(
            refusal,
            "ferro migrate baseline: this database already has migration records \
             (0001_create_author); baseline records migrations only on a database that has \
             none. Run `ferro migrate status` to see where it stands. Nothing was recorded."
        );
    }

    #[test]
    fn a_step_without_this_dialects_rendering_is_refused() {
        let mut step = ddl(1, "CREATE TABLE a (id int);\n");
        step.files.remove(&StepDialect::Sqlite);
        let dir = dir(vec![("create_author", vec![step], &["author"])]);
        let refusal =
            plan_baseline(&dir, &[], Dialect::Sqlite, None, NOW, "v").expect_err("refused");
        let missing = RunRefusal::MissingRendering {
            migration: 1,
            step: 1,
            step_name: "01_schema".into(),
            dialect: "sqlite",
            rendered_for: vec!["postgres"],
        };
        assert_eq!(
            refusal,
            format!("ferro migrate baseline: {missing} Nothing was recorded.")
        );
    }

    #[test]
    fn a_baseline_record_names_the_file_up_would_run() {
        // A step offering both this dialect's rendering and a portable file:
        // the record names whichever one the run planner picks.
        let mut step = ddl(1, "CREATE TABLE a (id int);\n");
        step.files.insert(
            StepDialect::Portable,
            file("01_schema.up.sql", "CREATE TABLE a (id int);\n"),
        );
        step.files.remove(&StepDialect::Postgres);
        let migrations = dir(vec![("create_author", vec![step], &["author"])]);
        for dialect in [Dialect::Sqlite, Dialect::Postgres] {
            let planned = plan_run(&migrations, &[], dialect, Direction::Up, false, None)
                .expect("up plans")
                .steps
                .remove(0)
                .record;
            let baseline = plan_baseline(&migrations, &[], dialect, None, NOW, "v")
                .expect("plan")
                .records
                .remove(0);
            assert_eq!(
                (&baseline.file, &baseline.checksum, baseline.kind),
                (&planned.file, &planned.checksum, planned.kind),
                "{dialect:?}"
            );
        }
    }

    #[test]
    fn removing_a_baseline_removes_its_records_and_nothing_else() {
        let dir = three();
        assert_eq!(
            baseline_removal(&records(&dir, "0002")),
            Ok(vec![(1, 1), (1, 2), (2, 1)])
        );
        assert_eq!(baseline_removal(&[]), Ok(Vec::new()));
    }

    #[test]
    fn removing_a_baseline_under_a_run_applied_above_it_is_refused_naming_it() {
        let dir = three();
        let mut standing = records(&dir, "0002");
        let mut above = records(&dir, "0003").remove(3);
        above.origin = Origin::Run;
        standing.push(above);
        assert_eq!(
            baseline_removal(&standing),
            Err(
                "ferro migrate baseline --remove: 0003_add_orgs was applied by a run above \
                 the baseline at 0002_add_teams. Revert it first with `ferro migrate down --to \
                 0002`, then remove the baseline. Nothing was removed."
                    .to_string()
            )
        );
    }
}
