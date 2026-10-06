//! The in-house migration system's offline half over FFI: generating a
//! migration from two snapshots, reading a migrations directory, and the
//! offline check. JSON in, JSON out; every failure is a `ValueError` whose
//! message names the fix (AGENTS.md § I-3: nothing here panics).

use ferro_migrate::directory::MigrationsDir;
use ferro_migrate::snapshot::{Snapshot, encode_checksum};
use ferro_migrate::{Dialect, check_migrations, generate};
use ferro_schema_ir::{IrEnvelope, SchemaIrPayload};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use std::path::Path;

/// Register every function of this module on the `_core` module.
pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(_generate_migration, m)?)?;
    m.add_function(wrap_pyfunction!(_check_migrations, m)?)?;
    m.add_function(wrap_pyfunction!(_read_migrations_dir, m)?)?;
    m.add_function(wrap_pyfunction!(_load_snapshot, m)?)?;
    m.add_function(wrap_pyfunction!(_store_snapshot, m)?)?;
    m.add_function(wrap_pyfunction!(_run_plan, m)?)?;
    m.add_function(wrap_pyfunction!(_run_status, m)?)?;
    m.add_function(wrap_pyfunction!(_acquire_run_lock, m)?)?;
    m.add_function(wrap_pyfunction!(_verify_run_lock, m)?)?;
    m.add_function(wrap_pyfunction!(_release_run_lock, m)?)?;
    m.add_function(wrap_pyfunction!(_run_lock_is_held, m)?)?;
    m.add_function(wrap_pyfunction!(_close_run_lock_connection_for_test, m)?)?;
    m.add_function(wrap_pyfunction!(_ensure_tracking_tables, m)?)?;
    m.add_function(wrap_pyfunction!(_read_records, m)?)?;
    m.add_function(wrap_pyfunction!(_write_record, m)?)?;
    m.add_function(wrap_pyfunction!(_execute_sql_step, m)?)?;
    m.add_function(wrap_pyfunction!(_tracking_tables_for, m)?)?;
    m.add_function(wrap_pyfunction!(_live_tables, m)?)?;
    Ok(())
}

fn parse_dialects(dialects: &[String]) -> PyResult<Vec<Dialect>> {
    dialects
        .iter()
        .map(|dialect| match dialect.as_str() {
            "postgres" => Ok(Dialect::Postgres),
            "sqlite" => Ok(Dialect::Sqlite),
            other => Err(PyValueError::new_err(format!(
                "Unknown dialect {other:?}; expected 'postgres' or 'sqlite'"
            ))),
        })
        .collect()
}

fn parse_target(json: &str) -> PyResult<IrEnvelope<SchemaIrPayload>> {
    let envelope: IrEnvelope<SchemaIrPayload> = serde_json::from_str(json)
        .map_err(|e| PyValueError::new_err(format!("invalid target_ir_json: {e}")))?;
    if envelope.ir_kind != "schema" {
        return Err(PyValueError::new_err(format!(
            "target_ir_json: expected ir_kind 'schema', got '{}'",
            envelope.ir_kind
        )));
    }
    Ok(envelope)
}

fn load_snapshot(bytes: &[u8], what: &str) -> PyResult<Snapshot> {
    Snapshot::load(bytes).map_err(|err| PyValueError::new_err(format!("{what} {err}")))
}

fn to_json<T: serde::Serialize>(value: &T) -> PyResult<String> {
    serde_json::to_string(value)
        .map_err(|e| PyRuntimeError::new_err(format!("could not serialize the result: {e}")))
}

fn snapshot_json(snapshot: &Snapshot) -> serde_json::Value {
    serde_json::json!({
        "checksum": encode_checksum(&snapshot.checksum),
        "parent_checksum": snapshot.parent_checksum.as_ref().map(encode_checksum),
        "ir": snapshot.ir,
    })
}

/// Generate the migration that turns the head snapshot into the declared
/// modelset.
///
/// `parent_ir_json` is the head migration's `ir.json` text exactly as stored
/// (its checksum is the new snapshot's parent link), or `None` before the
/// first migration. Returns the JSON of the `GeneratedMigration`, or `None`
/// when nothing renders DDL (no schema change).
///
/// # Errors
/// `ValueError` naming the refusal: a change this generator does not generate
/// yet (`not generated yet: <op> on <table> (ticket #N)`), an unreadable
/// snapshot, an unknown dialect, or an op that cannot render.
#[pyfunction]
#[pyo3(name = "_generate_migration")]
#[pyo3(signature = (parent_ir_json, target_ir_json, dialects))]
pub fn _generate_migration(
    parent_ir_json: Option<String>,
    target_ir_json: String,
    dialects: Vec<String>,
) -> PyResult<Option<String>> {
    let dialects = parse_dialects(&dialects)?;
    let target = parse_target(&target_ir_json)?;
    let parent = parent_ir_json
        .map(|json| load_snapshot(json.as_bytes(), "the head snapshot"))
        .transpose()?;
    let generated = generate(parent.as_ref(), &target, &dialects)
        .map_err(|err| PyValueError::new_err(err.to_string()))?;
    generated.as_ref().map(to_json).transpose()
}

/// Check a migrations directory against the declared modelset, reading files
/// only. Returns JSON `{"ok": bool, "head": str | null, "problems":
/// [{"kind": str, "message": str}]}`.
///
/// # Errors
/// `ValueError` when the target IR or a dialect is malformed.
#[pyfunction]
#[pyo3(name = "_check_migrations")]
pub fn _check_migrations(
    directory: String,
    target_ir_json: String,
    dialects: Vec<String>,
) -> PyResult<String> {
    let dialects = parse_dialects(&dialects)?;
    let target = parse_target(&target_ir_json)?;
    to_json(&check_migrations(Path::new(&directory), &target, &dialects))
}

/// Read and verify a migrations directory. Returns the JSON of the
/// `MigrationsDir`: `{"path", "migrations": [{"number", "name", "dir",
/// "steps": [{"ordinal", "name", "kind", "files": {<dialect>: {"up", "down",
/// "up_checksum", "headers"}}}], "snapshot": {"checksum", "parent_checksum",
/// "ir"}}]}`.
///
/// # Errors
/// `ValueError` naming the problem and its fix: a duplicate or missing
/// number, a broken snapshot chain, a step with both suffixed and unsuffixed
/// files, an unparseable header, an unexpected entry.
#[pyfunction]
#[pyo3(name = "_read_migrations_dir")]
pub fn _read_migrations_dir(directory: String) -> PyResult<String> {
    let dir = MigrationsDir::read(Path::new(&directory))
        .map_err(|err| PyValueError::new_err(err.to_string()))?;
    to_json(&dir)
}

/// Load one `ir.json` (any shipped `ir_version`). Returns JSON
/// `{"checksum", "parent_checksum", "ir"}`.
///
/// # Errors
/// `ValueError` when the text is not a loadable snapshot.
#[pyfunction]
#[pyo3(name = "_load_snapshot")]
pub fn _load_snapshot(ir_json: String) -> PyResult<String> {
    let snapshot = load_snapshot(ir_json.as_bytes(), "the snapshot")?;
    to_json(&snapshot_json(&snapshot))
}

/// The canonical `ir.json` text of a migration that changes no schema: a
/// full copy of the parent's modelset, linked to the parent by its checksum
/// (ADR-0037). `parent_ir_json` is the parent's `ir.json` text as stored.
///
/// # Errors
/// `ValueError` when the parent is not a loadable snapshot.
#[pyfunction]
#[pyo3(name = "_store_snapshot")]
pub fn _store_snapshot(parent_ir_json: String) -> PyResult<String> {
    let parent = load_snapshot(parent_ir_json.as_bytes(), "the head snapshot")?;
    let bytes = Snapshot::store(&parent.ir, Some(parent.checksum))
        .map_err(|err| PyValueError::new_err(format!("the copied snapshot {err}")))?;
    String::from_utf8(bytes)
        .map_err(|e| PyRuntimeError::new_err(format!("the stored snapshot is not UTF-8: {e}")))
}

// -- the runner (#519): planning, the run lock, records, SQL steps ------------------

fn parse_json<T: serde::de::DeserializeOwned>(json: &str, what: &str) -> PyResult<T> {
    serde_json::from_str(json).map_err(|e| PyValueError::new_err(format!("invalid {what}: {e}")))
}

fn parse_dialect(dialect: &str) -> PyResult<Dialect> {
    Ok(parse_dialects(&[dialect.to_string()])?[0])
}

/// Plan a run over the migrations directory against the applied records
/// (`_read_records`'s `records`). `direction_json` is `{"direction": "up"}`
/// or `{"direction": "down", "target": {...}}`; `live_tables_json` (the
/// governed schema's tables, from `_live_tables`) turns on the adoption
/// refusal for a database with no records. Returns the JSON of the
/// `RunPlan`.
///
/// # Errors
/// `RunRefused` carrying the refusal's text; `ValueError` for malformed
/// arguments.
#[pyfunction]
#[pyo3(name = "_run_plan")]
#[pyo3(signature = (directory, records_json, dialect, direction_json, allow_ahead, live_tables_json=None))]
pub fn _run_plan(
    directory: String,
    records_json: String,
    dialect: String,
    direction_json: String,
    allow_ahead: bool,
    live_tables_json: Option<String>,
) -> PyResult<String> {
    use ferro_migrate::run_plan::{Direction, StepRecord, check_adoption, plan_run, read_for_run};
    let records: Vec<StepRecord> = parse_json(&records_json, "records_json")?;
    let direction: Direction = parse_json(&direction_json, "direction_json")?;
    let dialect = parse_dialect(&dialect)?;
    let refuse = |r: ferro_migrate::RunRefusal| crate::run::refused(r.to_string());
    let dir = read_for_run(Path::new(&directory)).map_err(refuse)?;
    let plan = plan_run(&dir, &records, dialect, direction, allow_ahead).map_err(refuse)?;
    if let Some(json) = live_tables_json {
        let live: Vec<String> = parse_json(&json, "live_tables_json")?;
        check_adoption(&dir, &records, &live).map_err(refuse)?;
    }
    to_json(&plan)
}

/// Answer `ferro migrate status` from the directory and the records,
/// read-only. Returns the JSON of the `RunStatus`; a directory that cannot be
/// read is reported as its refusal with no migrations.
///
/// # Errors
/// `ValueError` for malformed arguments.
#[pyfunction]
#[pyo3(name = "_run_status")]
pub fn _run_status(
    directory: String,
    records_json: String,
    dialect: String,
    lock_held: bool,
) -> PyResult<String> {
    use ferro_migrate::run_plan::{RunStatus, StepRecord, read_for_run, run_status};
    let records: Vec<StepRecord> = parse_json(&records_json, "records_json")?;
    let dialect = parse_dialect(&dialect)?;
    match read_for_run(Path::new(&directory)) {
        Ok(dir) => to_json(&run_status(&dir, &records, dialect, lock_held)),
        Err(refusal) => to_json(&RunStatus {
            refusal_needs_attention: refusal.needs_attention(),
            refusal: Some(refusal.to_string()),
            ..RunStatus::default()
        }),
    }
}

/// Take the run lock on connection `using` for `governed_schema` (the
/// connection's current schema when `None`), waiting up to `timeout_s`
/// seconds (`0` tries once). `on_wait(text)` is called once, at once, when
/// another run holds it. Returns a handle for the other lock calls.
///
/// # Errors
/// `RunRefused` on timeout or behind a transaction-mode pooler.
#[pyfunction]
#[pyo3(name = "_acquire_run_lock")]
#[pyo3(signature = (using, governed_schema=None, timeout_s=30.0, on_wait=None))]
pub fn _acquire_run_lock(
    py: Python<'_>,
    using: Option<String>,
    governed_schema: Option<String>,
    timeout_s: f64,
    on_wait: Option<Py<PyAny>>,
) -> PyResult<Bound<'_, PyAny>> {
    if !(timeout_s.is_finite() && timeout_s >= 0.0) {
        return Err(PyValueError::new_err(format!(
            "timeout_s must be a non-negative number of seconds; got {timeout_s}"
        )));
    }
    let timeout = std::time::Duration::from_secs_f64(timeout_s);
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let engine = crate::state::engine_for_connection(using)?;
        let lock =
            crate::run::RunLock::acquire(&engine, governed_schema.as_deref(), timeout, |text| {
                if let Some(callback) = &on_wait {
                    Python::attach(|py| {
                        if let Err(err) = callback.call1(py, (text,)) {
                            err.print(py);
                        }
                    });
                }
            })
            .await?;
        crate::run::register_lock(lock)
    })
}

/// Check that the lock behind `handle` is still this run's.
///
/// # Errors
/// `RunRefused` with the pooler or the dropped-lock text.
#[pyfunction]
#[pyo3(name = "_verify_run_lock")]
pub fn _verify_run_lock(py: Python<'_>, handle: u64) -> PyResult<Bound<'_, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let lock = crate::run::registered_lock(handle)?;
        lock.lock().await.verify().await
    })
}

/// Release the lock behind `handle`.
///
/// # Errors
/// `ValueError` for an unknown handle.
#[pyfunction]
#[pyo3(name = "_release_run_lock")]
pub fn _release_run_lock(py: Python<'_>, handle: u64) -> PyResult<Bound<'_, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let lock = crate::run::unregister_lock(handle)?;
        let lock = std::sync::Arc::try_unwrap(lock).map_err(|_| {
            PyRuntimeError::new_err(
                "the run lock is in use by another call; release it after that call returns",
            )
        })?;
        lock.into_inner().release().await
    })
}

/// Whether any run holds the run lock for `governed_schema` (the current
/// schema when `None`), without taking it.
///
/// # Errors
/// A database error.
#[pyfunction]
#[pyo3(name = "_run_lock_is_held")]
#[pyo3(signature = (using, governed_schema=None))]
pub fn _run_lock_is_held(
    py: Python<'_>,
    using: Option<String>,
    governed_schema: Option<String>,
) -> PyResult<Bound<'_, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let engine = crate::state::engine_for_connection(using)?;
        crate::run::RunLock::is_held(&engine, governed_schema.as_deref()).await
    })
}

/// Close the Postgres lock connection behind `handle` without releasing the
/// lock, as a dropped network connection would. Test-only.
///
/// # Errors
/// `ValueError` for an unknown handle.
#[pyfunction]
#[pyo3(name = "_close_run_lock_connection_for_test")]
pub fn _close_run_lock_connection_for_test(
    py: Python<'_>,
    handle: u64,
) -> PyResult<Bound<'_, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let lock = crate::run::registered_lock(handle)?;
        lock.lock().await.close_connection().await;
        Ok(())
    })
}

/// Create the tracking tables where missing (in `tracking_schema` when set).
///
/// # Errors
/// `RunRefused` naming `CREATE SCHEMA` for a missing `tracking_schema`.
#[pyfunction]
#[pyo3(name = "_ensure_tracking_tables")]
#[pyo3(signature = (using, tracking_schema=None))]
pub fn _ensure_tracking_tables(
    py: Python<'_>,
    using: Option<String>,
    tracking_schema: Option<String>,
) -> PyResult<Bound<'_, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let engine = crate::state::engine_for_connection(using)?;
        crate::run::ensure_tracking_tables(&engine, tracking_schema.as_deref()).await
    })
}

/// Read the format and the step records, creating nothing. Returns JSON
/// `{"table", "exists", "format", "governed_schema", "records": [...],
/// "refusal"}`; `refusal` is the newer-format text, which every verb stops on.
///
/// # Errors
/// A database error.
#[pyfunction]
#[pyo3(name = "_read_records")]
#[pyo3(signature = (using, tracking_schema=None))]
pub fn _read_records(
    py: Python<'_>,
    using: Option<String>,
    tracking_schema: Option<String>,
) -> PyResult<Bound<'_, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let engine = crate::state::engine_for_connection(using)?;
        to_json(&crate::run::read_records(&engine, tracking_schema.as_deref()).await?)
    })
}

/// Write one step record as given (upsert on migration and step).
///
/// # Errors
/// `ValueError` for a malformed record; a database error.
#[pyfunction]
#[pyo3(name = "_write_record")]
#[pyo3(signature = (using, record_json, tracking_schema=None))]
pub fn _write_record(
    py: Python<'_>,
    using: Option<String>,
    record_json: String,
    tracking_schema: Option<String>,
) -> PyResult<Bound<'_, PyAny>> {
    let record: ferro_migrate::StepRecord = parse_json(&record_json, "record_json")?;
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let engine = crate::state::engine_for_connection(using)?;
        crate::run::write_record(&engine, tracking_schema.as_deref(), &record, None).await
    })
}

/// Execute one planned SQL step (`sql` is the up file's text) and write its
/// record (`record_json`, the plan's record with `ferro_version` set). With
/// `lock`, the run lock behind that handle is verified first. Returns JSON
/// `{"ok", "ms", "error", "message"}`.
///
/// # Errors
/// `RunRefused` when the lock was lost or the file changed since it was
/// planned; a database error writing a record.
#[pyfunction]
#[pyo3(name = "_execute_sql_step")]
#[pyo3(signature = (using, planned_step_json, sql, record_json, tracking_schema=None, lock=None))]
pub fn _execute_sql_step(
    py: Python<'_>,
    using: Option<String>,
    planned_step_json: String,
    sql: String,
    record_json: String,
    tracking_schema: Option<String>,
    lock: Option<u64>,
) -> PyResult<Bound<'_, PyAny>> {
    let step: ferro_migrate::PlannedStep = parse_json(&planned_step_json, "planned_step_json")?;
    let record: ferro_migrate::StepRecord = parse_json(&record_json, "record_json")?;
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let engine = crate::state::engine_for_connection(using)?;
        if let Some(handle) = lock {
            crate::run::registered_lock(handle)?
                .lock()
                .await
                .verify()
                .await?;
        }
        let outcome =
            crate::run::execute_sql_step(&engine, tracking_schema.as_deref(), &step, &sql, record)
                .await?;
        to_json(&outcome)
    })
}

/// Every tracking table governing `schema` (the current schema when
/// `None`). Returns JSON `[{"schema", "governed_schema", "format"}]`.
///
/// # Errors
/// A database error.
#[pyfunction]
#[pyo3(name = "_tracking_tables_for")]
#[pyo3(signature = (using, schema=None))]
pub fn _tracking_tables_for(
    py: Python<'_>,
    using: Option<String>,
    schema: Option<String>,
) -> PyResult<Bound<'_, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let engine = crate::state::engine_for_connection(using)?;
        to_json(&crate::run::tracking_tables_for(&engine, schema.as_deref()).await?)
    })
}

/// The governed schema's tables, as a JSON list.
///
/// # Errors
/// A database error.
#[pyfunction]
#[pyo3(name = "_live_tables")]
#[pyo3(signature = (using=None))]
pub fn _live_tables(py: Python<'_>, using: Option<String>) -> PyResult<Bound<'_, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let engine = crate::state::engine_for_connection(using)?;
        to_json(&crate::run::live_tables(&engine).await?)
    })
}
