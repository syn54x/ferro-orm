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
//!
//! I-1: the runner renders no schema DDL. The only DDL built here is the
//! tracking tables' own (#466); every other statement comes from a step file.

use crate::backend::{
    EngineBindValue, EngineConnection, EngineHandle, EngineRow, EngineValue, NullKind,
};
use ferro_ddl_lowering::Dialect;
use ferro_migrate::run_plan::{
    ExecMode, Origin, PlannedStep, RecordKind, StepRecord, TRACKING_FORMAT, check_format,
    run_lock_key, split_statements,
};
use ferro_migrate::snapshot::{encode_checksum, sha384};
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

fn show_duration(duration: Duration) -> String {
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
                        let inserted = MEMORY_LOCKS
                            .lock()
                            .map(|mut held| held.insert(name.clone()))
                            .unwrap_or(false);
                        if inserted {
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
    /// The pooler refusal on the first check after acquiring, the
    /// dropped-lock refusal on a later one.
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
                sqlx::query(HELD_BY_THIS_SESSION)
                    .bind(classid)
                    .bind(objid)
                    .persistent(false)
                    .fetch_one(conn)
                    .await
                    .and_then(|row| row.try_get::<bool, _>(0))
                    .unwrap_or(false)
            }
        };
        lock_verification_outcome(held, first).map_err(|r| refused(r.to_string()))?;
        *verified = true;
        Ok(())
    }

    /// Release the lock and close its connection.
    ///
    /// # Errors
    /// None today; a lock whose connection is already gone was released by
    /// the server.
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
                if let Ok(mut held) = MEMORY_LOCKS.lock() {
                    held.remove(name);
                }
            }
        }
        Ok(())
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
                SqliteTarget::Memory(name) => Ok(MEMORY_LOCKS
                    .lock()
                    .map(|held| held.contains(&name))
                    .unwrap_or(false)),
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

/// Forget `handle` and return its lock for release.
pub fn unregister_lock(handle: u64) -> PyResult<Arc<tokio::sync::Mutex<RunLock>>> {
    RUN_LOCKS
        .lock()
        .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("run lock registry poisoned"))?
        .remove(&handle)
        .ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err(format!("no run lock with handle {handle}"))
        })
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

/// A failure inside a step: its text, for the record and the operator.
struct StepFailure(String);

impl From<sqlx::Error> for StepFailure {
    fn from(err: sqlx::Error) -> Self {
        StepFailure(error_text(&err))
    }
}

async fn run_statements(
    conn: &mut EngineConnection,
    statements: &[String],
    file: &str,
) -> Result<(), StepFailure> {
    for statement in statements {
        crate::log_debug(format!("ferro migrate: {file}: {statement}"));
        conn.execute_sql_unprepared(statement).await?;
    }
    Ok(())
}

async fn pool_connection(engine: &EngineHandle) -> Result<EngineConnection, sqlx::Error> {
    if let Some(pool) = engine.sqlite_pool() {
        return Ok(EngineConnection::Sqlite(pool.acquire().await?));
    }
    match engine.postgres_pool() {
        Some(pool) => Ok(EngineConnection::Postgres(pool.acquire().await?)),
        None => Err(sqlx::Error::PoolClosed),
    }
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

async fn foreign_keys_off(
    conn: &mut EngineConnection,
    statements: &[String],
    file: &str,
    upsert: &str,
    finished: impl FnOnce() -> StepRecord,
) -> Result<(), StepFailure> {
    conn.execute_sql_unprepared("PRAGMA foreign_keys = OFF")
        .await?;
    let read = conn
        .fetch_all_sql_unprepared_with_binds("PRAGMA foreign_keys", &[])
        .await?;
    if read.first().and_then(|row| int(column(row, 0))) != Some(0) {
        return Err(StepFailure(
            "PRAGMA foreign_keys = OFF did not take effect on the step's connection".to_string(),
        ));
    }
    conn.execute_sql_unprepared("BEGIN IMMEDIATE").await?;
    run_statements(conn, statements, file).await?;
    let violations = conn
        .fetch_all_sql_unprepared_with_binds("PRAGMA foreign_key_check", &[])
        .await?;
    if !violations.is_empty() {
        return Err(StepFailure(format!(
            "PRAGMA foreign_key_check found rows that violate a foreign key: {}",
            describe_fk_violations(&violations)
        )));
    }
    conn.fetch_all_sql_unprepared_with_binds(upsert, &record_binds(&finished()))
        .await?;
    conn.execute_sql_unprepared("COMMIT").await?;
    conn.execute_sql_unprepared("PRAGMA foreign_keys = ON")
        .await?;
    Ok(())
}

fn failure_message(step: &PlannedStep, error: &str) -> String {
    let after = match step.mode {
        ExecMode::NoTransaction => {
            "It runs without a transaction, so the statements before the failing one stay \
             applied; fix the file or the database and run `ferro migrate up` again: the step \
             re-runs from its first statement."
        }
        ExecMode::Transactional | ExecMode::ForeignKeysOff => {
            "The step was rolled back; fix the file or the database and run `ferro migrate up` \
             again to resume at it."
        }
    };
    format!(
        "ferro migrate: {}/{} failed: {error}\n{after} Nothing after it ran.",
        step.migration_name, step.file
    )
}

/// Execute one SQL step and write its record, in the step's mode:
///
/// - **Transactional**: the started record commits first (so `status`
///   sees the attempt), then one transaction runs the file's statements and
///   the finished mark and commits them together.
/// - **NoTransaction** (Postgres): the started record, then each statement
///   in autocommit (what lets `CREATE INDEX CONCURRENTLY` run), then the
///   finished mark.
/// - **ForeignKeysOff** (SQLite): one dedicated connection —
///   `PRAGMA foreign_keys = OFF` (read back), `BEGIN IMMEDIATE`, the file,
///   `PRAGMA foreign_key_check` as a failure, the finished mark, `COMMIT`,
///   the pragma restored; the connection is closed on failure.
///
/// A failure rolls back what the mode can roll back, then writes `failed_at`
/// and `error` on the started record in a separate transaction; the next run
/// resumes at the step. `sql` must be the bytes the planner hashed.
///
/// # Errors
/// A refusal when `sql` is not the planned file (edited mid-run) or the
/// record is not the planned step's; a database error writing a record. A
/// failing statement is not an error: it is the returned [`StepOutcome`].
pub async fn execute_sql_step(
    engine: &EngineHandle,
    tracking_schema: Option<&str>,
    step: &PlannedStep,
    sql: &str,
    record: StepRecord,
) -> PyResult<StepOutcome> {
    let shown = format!("{}/{}", step.migration_name, step.file);
    if encode_checksum(&sha384(sql.as_bytes())) != step.checksum {
        return Err(refused(format!(
            "ferro migrate: {shown} changed while this run was in progress; it was planned \
             with sha384:{}. Run `ferro migrate up` again. Nothing more was applied.",
            step.checksum
        )));
    }
    if (record.migration, record.step, &record.checksum)
        != (step.migration, step.step, &step.checksum)
    {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "the record for {}:{} does not describe the planned step {shown}",
            record.migration, record.step
        )));
    }
    let dialect = engine.backend();
    let tracking = Tracking::new(dialect, tracking_schema);
    let statements = split_statements(sql);
    let started_at = now_iso();
    let started = StepRecord {
        started_at: started_at.clone(),
        finished_at: None,
        failed_at: None,
        error: None,
        duration_ms: 0,
        kind: step.mode.record_kind(),
        ..record
    };
    write_record(engine, tracking_schema, &started, None).await?;

    let clock = Instant::now();
    let finish = |ms: i64| StepRecord {
        finished_at: Some(now_iso()),
        duration_ms: ms,
        ..started.clone()
    };
    let upsert = upsert_sql(dialect, &tracking);
    let elapsed = |clock: Instant| i64::try_from(clock.elapsed().as_millis()).unwrap_or(i64::MAX);

    let outcome: Result<(), StepFailure> = match step.mode {
        ExecMode::Transactional => match engine.begin_transaction_connection().await {
            Err(err) => Err(err.into()),
            Ok(mut conn) => {
                let result = async {
                    run_statements(&mut conn, &statements, &shown).await?;
                    let finished = finish(elapsed(clock));
                    conn.fetch_all_sql_unprepared_with_binds(&upsert, &record_binds(&finished))
                        .await?;
                    conn.commit().await?;
                    Ok::<(), StepFailure>(())
                }
                .await;
                if result.is_err() && conn.rollback().await.is_err() {
                    let _ = conn.detach_and_close().await;
                }
                result
            }
        },
        ExecMode::NoTransaction => match pool_connection(engine).await {
            Err(err) => Err(err.into()),
            Ok(mut conn) => {
                let result = run_statements(&mut conn, &statements, &shown).await;
                drop(conn);
                match result {
                    Ok(()) => write_record(engine, tracking_schema, &finish(elapsed(clock)), None)
                        .await
                        .map_err(|e| StepFailure(e.to_string())),
                    Err(err) => Err(err),
                }
            }
        },
        ExecMode::ForeignKeysOff => match pool_connection(engine).await {
            Err(err) => Err(err.into()),
            Ok(mut conn) => {
                let result = foreign_keys_off(&mut conn, &statements, &shown, &upsert, || {
                    finish(elapsed(clock))
                })
                .await;
                if result.is_err() {
                    let _ = conn.execute_sql_unprepared("ROLLBACK").await;
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
        Err(StepFailure(error)) => {
            let failed = StepRecord {
                failed_at: Some(now_iso()),
                error: Some(error.clone()),
                duration_ms: ms,
                ..started
            };
            write_record(engine, tracking_schema, &failed, None).await?;
            Ok(StepOutcome {
                ok: false,
                ms,
                message: Some(failure_message(step, &error)),
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
}
