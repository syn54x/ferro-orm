//! The in-house migration system over FFI. The offline half (generating a
//! migration from two snapshots, reading a migrations directory, the offline
//! check) is JSON in, JSON out; every failure is a `ValueError` whose message
//! names the fix. A run is two objects (ADR-0048): `_open_tracked` returns a
//! `TrackedDatabase` (the records and one read of the directory, held), and
//! its `locked(...)` block a `LockedDatabase` that alone writes. AGENTS.md §
//! I-3: nothing here panics.

use crate::migrate::parse_dialect;
use ferro_migrate::directory::MigrationsDir;
use ferro_migrate::snapshot::{Snapshot, encode_checksum};
use ferro_migrate::{Dialect, GenerateOptions, check_migrations, generate_with};
use ferro_schema_ir::{IrEnvelope, SchemaIrPayload};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use std::path::Path;
use std::sync::Arc;

/// Register every function of this module on the `_core` module.
pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(_generate_migration, m)?)?;
    m.add_function(wrap_pyfunction!(_check_migrations, m)?)?;
    m.add_function(wrap_pyfunction!(_read_migrations_dir, m)?)?;
    m.add_function(wrap_pyfunction!(_load_snapshot, m)?)?;
    m.add_function(wrap_pyfunction!(_store_snapshot, m)?)?;
    m.add_function(wrap_pyfunction!(_open_tracked, m)?)?;
    m.add_function(wrap_pyfunction!(_tracking_tables_for, m)?)?;
    m.add_class::<TrackedDatabase>()?;
    m.add_class::<LockScope>()?;
    m.add_class::<LockedDatabase>()?;
    m.add_class::<Plan>()?;
    m.add_class::<StepHandle>()?;
    m.add_class::<BaselinePlan>()?;
    m.add_class::<RerecordPlan>()?;
    Ok(())
}

fn parse_dialects(dialects: &[String]) -> PyResult<Vec<Dialect>> {
    dialects
        .iter()
        .map(|dialect| parse_dialect(dialect))
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
/// when nothing renders DDL (no schema change). `options_json` carries
/// `ferro migrate new`'s options: `{"no_backfill": ["<table>.<column>", ...]}`.
///
/// # Errors
/// `ValueError` naming the refusal: a change this generator does not generate
/// yet (`not generated yet: <op> on <table> (ticket #N)`), an unreadable
/// snapshot, an unknown dialect, or an op that cannot render.
#[pyfunction]
#[pyo3(name = "_generate_migration")]
#[pyo3(signature = (parent_ir_json, target_ir_json, dialects, options_json = None))]
pub fn _generate_migration(
    parent_ir_json: Option<String>,
    target_ir_json: String,
    dialects: Vec<String>,
    options_json: Option<String>,
) -> PyResult<Option<String>> {
    let dialects = parse_dialects(&dialects)?;
    let target = parse_target(&target_ir_json)?;
    let parent = parent_ir_json
        .map(|json| load_snapshot(json.as_bytes(), "the head snapshot"))
        .transpose()?;
    // `{"no_backfill": ["author.slug"]}` (`ferro migrate new --no-backfill`).
    let options: GenerateOptions = options_json
        .map(|json| serde_json::from_str(&json))
        .transpose()
        .map_err(|err| PyValueError::new_err(format!("the generate options: {err}")))?
        .unwrap_or_default();
    let generated = generate_with(parent.as_ref(), &target, &dialects, &options)
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

// -- the run objects (ADR-0048) ------------------------------------------------------

fn parse_json<T: serde::de::DeserializeOwned>(json: &str, what: &str) -> PyResult<T> {
    serde_json::from_str(json).map_err(|e| PyValueError::new_err(format!("invalid {what}: {e}")))
}

/// `[[migration, step, ["author.id", ...]], ...]` → the planner's
/// `OrderKeys`.
fn parse_order_keys(json: &str) -> PyResult<ferro_migrate::run_plan::OrderKeys> {
    let entries: Vec<(u16, u8, Vec<String>)> = parse_json(json, "order_keys_json")?;
    Ok(entries
        .into_iter()
        .map(|(migration, step, keys)| ((migration, step), keys))
        .collect())
}

/// The live connection of the transaction `route` names (a
/// `ferro.transaction()` block's route), refused when it has none.
fn transaction_slot(
    route: &crate::state::RouteHandle,
) -> PyResult<crate::state::TransactionConnection> {
    let refused = || {
        PyRuntimeError::new_err(
            "a step record written in a transaction needs that transaction's route; this \
             route has no open transaction",
        )
    };
    let tx_id = route.tx_id.as_deref().ok_or_else(refused)?;
    let handle = match route.session_id.as_deref() {
        Some(session_id) => crate::state::session_state(session_id)?
            .transaction_registry
            .get(tx_id)
            .map(|entry| entry.value().clone()),
        None => crate::state::TRANSACTION_REGISTRY
            .get(tx_id)
            .map(|entry| entry.value().clone()),
    };
    handle.map(|handle| handle.conn).ok_or_else(refused)
}

/// Refuse a cursor that is not `{"keys": [...], "order_by": ["author.id",
/// ...], "rows_done": rows_done}`: the record's two copies of the count never
/// disagree, and the cursor names the order keys it is a position in (what
/// `rerecord --continue` compares an edited file's against, ADR-0030).
fn check_cursor_json(cursor_json: &str, rows_done: i64) -> PyResult<()> {
    let bad = |why: &str| {
        PyValueError::new_err(format!(
            "cursor_json must be {{\"keys\": [...], \"order_by\": [...], \"rows_done\": \
             {rows_done}}} ({why}): {cursor_json}"
        ))
    };
    let value: serde_json::Value =
        serde_json::from_str(cursor_json).map_err(|e| bad(&e.to_string()))?;
    let object = value.as_object().ok_or_else(|| bad("not an object"))?;
    let Some(keys) = object.get("keys").and_then(serde_json::Value::as_array) else {
        return Err(bad("no keys array"));
    };
    let Some(order_by) = object.get("order_by").and_then(serde_json::Value::as_array) else {
        return Err(bad("no order_by array"));
    };
    if order_by.len() != keys.len() || !order_by.iter().all(serde_json::Value::is_string) {
        return Err(bad("order_by is not one key name per value"));
    }
    if object.get("rows_done").and_then(serde_json::Value::as_i64) != Some(rows_done) {
        return Err(bad("its rows_done differs"));
    }
    if object.len() != 3 {
        return Err(bad("unexpected members"));
    }
    Ok(())
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

/// A value as the Python object its JSON reads as (`dict`, `list`, ...).
fn to_py<T: serde::Serialize>(py: Python<'_>, value: &T) -> PyResult<Py<PyAny>> {
    let json = to_json(value)?;
    Ok(py.import("json")?.call_method1("loads", (json,))?.unbind())
}

/// [`to_py`] from inside a future, where the GIL is not held.
fn to_py_attached<T: serde::Serialize>(value: &T) -> PyResult<Py<PyAny>> {
    Python::attach(|py| to_py(py, value))
}

/// A Python value as the JSON text it dumps to.
fn dumps(value: &Bound<'_, PyAny>) -> PyResult<String> {
    value
        .py()
        .import("json")?
        .call_method1("dumps", (value,))?
        .extract()
}

/// A run's direction from Python: `{"direction": "up"}`, `{"direction":
/// "up", "through": "0007"}`, `{"direction": "down", "target": "latest" |
/// "all" | {"migration": 5} | {"step": [7, 2]}}`. `through` is read by the
/// planner's own rule ([`ferro_migrate::run_plan::parse_through`]).
fn parse_direction(direction: &Bound<'_, PyAny>) -> PyResult<ferro_migrate::Direction> {
    let mut value: serde_json::Value = parse_json(&dumps(direction)?, "direction")?;
    if let Some(through) = value.get("through").and_then(serde_json::Value::as_str) {
        let number = ferro_migrate::run_plan::parse_through(through)
            .map_err(|refusal| crate::run::refused_by(&refusal))?;
        value["through"] = number.into();
    }
    serde_json::from_value(value)
        .map_err(|e| PyValueError::new_err(format!("invalid direction: {e}")))
}

/// `[[migration, step, ["author.id", ...]], ...]` from Python, or `None`.
fn order_keys_arg(
    order_keys: Option<&Bound<'_, PyAny>>,
) -> PyResult<Option<ferro_migrate::run_plan::OrderKeys>> {
    order_keys
        .filter(|keys| !keys.is_none())
        .map(|keys| parse_order_keys(&dumps(keys)?))
        .transpose()
}

/// A run lock's wait from Python seconds.
fn lock_timeout(timeout_s: f64) -> PyResult<std::time::Duration> {
    if !(timeout_s.is_finite() && timeout_s >= 0.0) {
        return Err(PyValueError::new_err(format!(
            "timeout_s must be a non-negative number of seconds; got {timeout_s}"
        )));
    }
    std::time::Duration::try_from_secs_f64(timeout_s).map_err(|_| {
        PyValueError::new_err(format!(
            "timeout_s {timeout_s:e} is too large to be a duration of seconds"
        ))
    })
}

/// Open the tracking tables of connection `using` (in `tracking_schema`
/// when set) and the migrations directory at `directory` (ADR-0048): one
/// read of the records and one read of the directory, held. Creates nothing
/// and takes no lock. `ddl_lock_timeout_s` is the database's DDL lock
/// timeout (ADR-0044), which every SQL step the run executes waits under
/// (`0` sets none and never retries).
///
/// # Errors
/// `ValueError` for a negative or non-finite timeout; a database error.
#[pyfunction]
#[pyo3(name = "_open_tracked")]
#[pyo3(signature = (using, tracking_schema, directory, ddl_lock_timeout_s=5.0))]
pub fn _open_tracked(
    py: Python<'_>,
    using: Option<String>,
    tracking_schema: Option<String>,
    directory: String,
    ddl_lock_timeout_s: f64,
) -> PyResult<Bound<'_, PyAny>> {
    let ddl = crate::ddl_exec::DdlExecutor::from_seconds(ddl_lock_timeout_s)?;
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let engine = crate::state::engine_for_connection(using)?;
        let tracked =
            crate::run::Tracked::open(engine, tracking_schema, Path::new(&directory)).await?;
        Ok(TrackedDatabase {
            inner: Arc::new(tracked),
            ddl: Arc::new(ddl),
        })
    })
}

/// One planned step, opaque to Python: what it may read of it, and the
/// handle the locked run executes or records it by. Nothing about it crosses
/// back to be checked.
#[pyclass(frozen, module = "ferro._core")]
pub struct StepHandle {
    step: Arc<ferro_migrate::PlannedStep>,
    /// Going up, the step's unfinished record (it resumes); going down, the
    /// record its down removes.
    standing: Option<ferro_migrate::StepRecord>,
    down: bool,
    /// The locked run whose plan holds it; `0` for a preview.
    owner: u64,
}

#[pymethods]
impl StepHandle {
    /// `NNNN`.
    #[getter]
    fn migration(&self) -> u16 {
        self.step.migration
    }

    /// `NNNN_<name>`.
    #[getter]
    fn migration_name(&self) -> &str {
        &self.step.migration_name
    }

    /// `NN`.
    #[getter]
    fn step(&self) -> u8 {
        self.step.step
    }

    /// The file the run executes: the up file going up, the down file going
    /// down (a data step's own file either way).
    #[getter]
    fn file(&self) -> &str {
        &self.step.file
    }

    /// That file's path.
    #[getter]
    fn path(&self) -> String {
        self.step.path.display().to_string()
    }

    /// SHA-384 of that file's bytes, lowercase hex: what a data step's
    /// loaded source is checked against.
    #[getter]
    fn checksum(&self) -> &str {
        &self.step.checksum
    }

    /// A Python data step.
    #[getter]
    fn data(&self) -> bool {
        self.step.data
    }

    /// Going down: why the step's down runs no statement.
    #[getter]
    fn nothing_to_reverse(&self) -> Option<&str> {
        self.step.nothing_to_reverse.as_deref()
    }

    /// `{"recorded", "recorded_file"}` when the unfinished attempt ran a
    /// different file (accepted and re-recorded, ADR-0030), else `None`.
    #[getter]
    fn edited(&self, py: Python<'_>) -> PyResult<Option<Py<PyAny>>> {
        self.step.edited.as_ref().map(|e| to_py(py, e)).transpose()
    }

    /// Whether an unfinished record exists: the run resumes at this step.
    #[getter]
    fn resumes(&self) -> bool {
        self.step.resumes
    }

    /// The chunked cursor this walk resumes from: going up a chunked
    /// record's `resume_cursor`, going down a reverting record's
    /// `revert_cursor`; `None` otherwise.
    #[getter]
    fn resume_cursor(&self) -> Option<&str> {
        let record = self.standing.as_ref()?;
        if self.down {
            record.reverting.then_some(record.revert_cursor.as_deref()?)
        } else {
            (record.kind == ferro_migrate::RecordKind::Chunked)
                .then_some(record.resume_cursor.as_deref()?)
        }
    }

    /// The rows the walk resumed from has committed, when it resumes one.
    #[getter]
    fn rows_done(&self) -> Option<i64> {
        let record = self.standing.as_ref()?;
        let resumes = if self.down {
            record.reverting
        } else {
            record.kind == ferro_migrate::RecordKind::Chunked
        };
        if resumes { record.rows_done } else { None }
    }

    /// The step's record as it stands (a dict), or `None` going up for a
    /// step with no record.
    #[getter]
    fn standing(&self, py: Python<'_>) -> PyResult<Option<Py<PyAny>>> {
        self.standing.as_ref().map(|r| to_py(py, r)).transpose()
    }

    fn __repr__(&self) -> String {
        format!(
            "<StepHandle {}/{}>",
            self.step.migration_name, self.step.file
        )
    }
}

/// A run's plan: its step handles in order, the migrations `allow_ahead`
/// let through, and which way it goes.
#[pyclass(frozen, module = "ferro._core")]
pub struct Plan {
    steps: Vec<Py<StepHandle>>,
    ahead: Vec<String>,
    down: bool,
}

#[pymethods]
impl Plan {
    /// The steps, in the order the run takes them.
    #[getter]
    fn steps(&self, py: Python<'_>) -> Vec<Py<StepHandle>> {
        self.steps.iter().map(|s| s.clone_ref(py)).collect()
    }

    /// Applied migrations the directory lacks, let through by `allow_ahead`.
    #[getter]
    fn ahead(&self) -> Vec<String> {
        self.ahead.clone()
    }

    /// `"up"` or `"down"`.
    #[getter]
    fn direction(&self) -> &'static str {
        if self.down { "down" } else { "up" }
    }
}

/// `plan`'s handles for `owner` (`0`: a preview), each with its standing
/// record from `records`.
fn plan_handles(
    plan: ferro_migrate::RunPlan,
    records: &[ferro_migrate::StepRecord],
    direction: ferro_migrate::Direction,
    owner: u64,
) -> PyResult<Plan> {
    let down = matches!(direction, ferro_migrate::Direction::Down { .. });
    Python::attach(|py| {
        let steps = plan
            .steps
            .into_iter()
            .map(|step| {
                let standing = if down {
                    Some(step.record.clone())
                } else {
                    records
                        .iter()
                        .find(|r| (r.migration, r.step) == (step.migration, step.step))
                        .cloned()
                };
                Py::new(
                    py,
                    StepHandle {
                        step: Arc::new(step),
                        standing,
                        down,
                        owner,
                    },
                )
            })
            .collect::<PyResult<_>>()?;
        Ok(Plan {
            steps,
            ahead: plan.ahead,
            down,
        })
    })
}

/// The reads both run objects carry, from `tracked`.
mod reads {
    use super::*;

    pub fn records(py: Python<'_>, tracked: &crate::run::Tracked) -> PyResult<Py<PyAny>> {
        to_py(py, &tracked.records())
    }

    pub fn migrations(py: Python<'_>, tracked: &crate::run::Tracked) -> PyResult<Py<PyAny>> {
        to_py(py, &tracked.held()?.dir)
    }

    pub fn status(
        py: Python<'_>,
        tracked: &crate::run::Tracked,
        order_keys: Option<&Bound<'_, PyAny>>,
        lock_held: bool,
    ) -> PyResult<Py<PyAny>> {
        let order_keys = order_keys_arg(order_keys)?;
        to_py(py, &tracked.status(lock_held, order_keys.as_ref()))
    }
}

/// A database's tracking tables and its migrations directory, each read
/// once (ADR-0048). Read-only: `status`, a preview `plan`, whether the run
/// lock is held; `locked(...)` is the one door to a run's writes.
#[pyclass(frozen, module = "ferro._core")]
pub struct TrackedDatabase {
    inner: Arc<crate::run::Tracked>,
    ddl: Arc<crate::ddl_exec::DdlExecutor>,
}

#[pymethods]
impl TrackedDatabase {
    /// `"postgres"` or `"sqlite"`.
    #[getter]
    fn dialect(&self) -> &'static str {
        dialect_name(self.inner.dialect())
    }

    /// `<schema>._ferro_migrations`.
    #[getter]
    fn tracking_table(&self) -> &str {
        self.inner.tracking_table()
    }

    /// The step records as read (a list of dicts).
    #[getter]
    fn records(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        reads::records(py, &self.inner)
    }

    /// The newer-format refusal every verb stops on, or `None`.
    #[getter]
    fn refusal(&self) -> Option<&str> {
        self.inner.format_refusal()
    }

    /// The held directory read, as `_read_migrations_dir` shapes it.
    ///
    /// # Errors
    /// `RunRefused` when the directory could not be read.
    #[getter]
    fn migrations(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        reads::migrations(py, &self.inner)
    }

    /// Whether any run holds the run lock, asked without taking it.
    fn lock_held<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let tracked = Arc::clone(&self.inner);
        pyo3_async_runtimes::tokio::future_into_py(py, async move { tracked.lock_held().await })
    }

    /// `ferro migrate status`'s document: every migration's steps, the
    /// migrations ahead of the directory, and the refusal `up` would meet.
    #[pyo3(signature = (order_keys=None, *, lock_held=false))]
    fn status(
        &self,
        py: Python<'_>,
        order_keys: Option<&Bound<'_, PyAny>>,
        lock_held: bool,
    ) -> PyResult<Py<PyAny>> {
        reads::status(py, &self.inner, order_keys, lock_held)
    }

    /// A preview of a run's plan: its steps cannot be executed.
    ///
    /// # Errors
    /// `RunRefused` for every refusal the run would meet.
    #[pyo3(signature = (direction, *, allow_ahead=false, order_keys=None))]
    fn plan<'py>(
        &self,
        py: Python<'py>,
        direction: &Bound<'py, PyAny>,
        allow_ahead: bool,
        order_keys: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let direction = parse_direction(direction)?;
        let order_keys = order_keys_arg(order_keys)?;
        let tracked = Arc::clone(&self.inner);
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let plan = tracked
                .plan(direction, allow_ahead, order_keys.as_ref())
                .await?;
            plan_handles(plan, tracked.records(), direction, 0)
        })
    }

    /// The run lock, as an async context manager whose block holds it: a
    /// second run waits up to `timeout_s` seconds (`0` tries once), and
    /// `on_wait(text)` hears at once that it waits. The block's
    /// [`LockedDatabase`] re-reads the records under the lock.
    ///
    /// # Errors
    /// `ValueError` for a negative, non-finite or unrepresentable timeout.
    #[pyo3(signature = (timeout_s, on_wait=None))]
    fn locked(&self, timeout_s: f64, on_wait: Option<Py<PyAny>>) -> PyResult<LockScope> {
        Ok(LockScope {
            tracked: Arc::clone(&self.inner),
            ddl: Arc::clone(&self.ddl),
            timeout: lock_timeout(timeout_s)?,
            on_wait,
            unacquired: false,
            entered: Arc::new(std::sync::Mutex::new(None)),
        })
    }

    /// A `locked()` block whose Postgres lock never took the advisory lock,
    /// as a transaction-mode pooler hands back: its first write fails the
    /// first check. Test-only.
    fn _locked_unacquired_for_test(&self) -> LockScope {
        LockScope {
            tracked: Arc::clone(&self.inner),
            ddl: Arc::clone(&self.ddl),
            timeout: std::time::Duration::ZERO,
            on_wait: None,
            unacquired: true,
            entered: Arc::new(std::sync::Mutex::new(None)),
        }
    }

    fn __repr__(&self) -> String {
        format!("<TrackedDatabase {}>", self.inner.tracking_table())
    }
}

fn dialect_name(dialect: Dialect) -> &'static str {
    match dialect {
        Dialect::Postgres => "postgres",
        Dialect::Sqlite => "sqlite",
    }
}

/// `async with tracked.locked(...) as run:` — takes the run lock on entry
/// and releases it on exit, error and cancellation included.
#[pyclass(frozen, module = "ferro._core")]
pub struct LockScope {
    tracked: Arc<crate::run::Tracked>,
    ddl: Arc<crate::ddl_exec::DdlExecutor>,
    timeout: std::time::Duration,
    on_wait: Option<Py<PyAny>>,
    unacquired: bool,
    entered: Arc<std::sync::Mutex<Option<Arc<crate::run::Locked>>>>,
}

fn scope_poisoned() -> PyErr {
    PyRuntimeError::new_err("ferro migrate: the run lock's block is unusable after a panic")
}

#[pymethods]
impl LockScope {
    fn __aenter__<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let tracked = Arc::clone(&self.tracked);
        let ddl = Arc::clone(&self.ddl);
        let on_wait = self.on_wait.as_ref().map(|c| c.clone_ref(py));
        let (timeout, unacquired) = (self.timeout, self.unacquired);
        let entered = Arc::clone(&self.entered);
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let locked = if unacquired {
                tracked.lock_unacquired_for_test().await?
            } else {
                tracked
                    .lock(timeout, |text| {
                        if let Some(callback) = &on_wait {
                            Python::attach(|py| {
                                if let Err(err) = callback.call1(py, (text,)) {
                                    err.print(py);
                                }
                            });
                        }
                    })
                    .await?
            };
            let locked = Arc::new(locked);
            *entered.lock().map_err(|_| scope_poisoned())? = Some(Arc::clone(&locked));
            Ok(LockedDatabase { inner: locked, ddl })
        })
    }

    #[pyo3(signature = (*_exc))]
    fn __aexit__<'py>(
        &self,
        py: Python<'py>,
        _exc: &Bound<'py, pyo3::types::PyTuple>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let locked = self.entered.lock().map_err(|_| scope_poisoned())?.take();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            if let Some(locked) = locked {
                locked.release().await?;
            }
            Ok(false)
        })
    }
}

/// What `start`/`finish`/... need of a step: the planned step of this run's
/// own plan.
fn owned<'a>(
    step: &'a StepHandle,
    locked: &crate::run::Locked,
) -> PyResult<&'a Arc<ferro_migrate::PlannedStep>> {
    if step.owner != locked.id() {
        return Err(PyValueError::new_err(format!(
            "{}/{} was not planned by this locked run: a preview plan's steps cannot execute, \
             and another run's are not this one's",
            step.step.migration_name, step.step.file
        )));
    }
    Ok(&step.step)
}

/// `rows_done`, refused below zero.
fn rows(rows_done: i64) -> PyResult<i64> {
    if rows_done < 0 {
        return Err(PyValueError::new_err(format!(
            "rows_done must be zero or more, not {rows_done}"
        )));
    }
    Ok(rows_done)
}

/// A chunked position from Python: given exactly when `rows_done` is.
fn position(
    cursor: Option<String>,
    rows_done: Option<i64>,
) -> PyResult<Option<crate::run::Position>> {
    match (cursor, rows_done) {
        (cursor, Some(rows_done)) => Ok(Some((cursor, rows(rows_done)?))),
        (None, None) => Ok(None),
        (Some(_), None) => Err(PyValueError::new_err(
            "a cursor is recorded with the rows_done it is a position after",
        )),
    }
}

/// A batch's position from Python, its cursor checked.
fn batch_position(cursor: Option<String>, rows_done: i64) -> PyResult<crate::run::Position> {
    let rows_done = rows(rows_done)?;
    if let Some(cursor) = &cursor {
        check_cursor_json(cursor, rows_done)?;
    }
    Ok((cursor, rows_done))
}

fn record_kind(kind: &str) -> PyResult<ferro_migrate::RecordKind> {
    ferro_migrate::RecordKind::parse(kind).ok_or_else(|| {
        PyValueError::new_err(format!(
            "{kind:?} is not a step record kind (ddl, ddl-no-transaction, atomic, chunked)"
        ))
    })
}

/// A run holding the run lock (ADR-0048), alive inside its `locked()`
/// block: the reads of [`TrackedDatabase`] with the records re-read under
/// the lock, its plan, and every write a run makes. Each write verifies the
/// lock first; the first creates the tracking tables where they are missing.
#[pyclass(frozen, module = "ferro._core")]
pub struct LockedDatabase {
    inner: Arc<crate::run::Locked>,
    ddl: Arc<crate::ddl_exec::DdlExecutor>,
}

#[pymethods]
impl LockedDatabase {
    /// `"postgres"` or `"sqlite"`.
    #[getter]
    fn dialect(&self) -> &'static str {
        dialect_name(self.inner.tracked().dialect())
    }

    /// `<schema>._ferro_migrations`.
    #[getter]
    fn tracking_table(&self) -> &str {
        self.inner.tracked().tracking_table()
    }

    /// The step records, re-read under the lock.
    #[getter]
    fn records(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        reads::records(py, self.inner.tracked())
    }

    /// The newer-format refusal every verb stops on, or `None`.
    #[getter]
    fn refusal(&self) -> Option<&str> {
        self.inner.tracked().format_refusal()
    }

    /// The held directory read (the tracked database's one read).
    ///
    /// # Errors
    /// `RunRefused` when the directory could not be read.
    #[getter]
    fn migrations(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        reads::migrations(py, self.inner.tracked())
    }

    /// Whether any run holds the run lock (this one does).
    fn lock_held<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let locked = Arc::clone(&self.inner);
        pyo3_async_runtimes::tokio::future_into_py(
            py,
            async move { locked.tracked().lock_held().await },
        )
    }

    /// `ferro migrate status`'s document, as of the records read under the
    /// lock.
    #[pyo3(signature = (order_keys=None, *, lock_held=false))]
    fn status(
        &self,
        py: Python<'_>,
        order_keys: Option<&Bound<'_, PyAny>>,
        lock_held: bool,
    ) -> PyResult<Py<PyAny>> {
        reads::status(py, self.inner.tracked(), order_keys, lock_held)
    }

    /// The run's plan: step handles this run executes and records.
    ///
    /// # Errors
    /// `RunRefused` for every refusal the run meets.
    #[pyo3(signature = (direction, *, allow_ahead=false, order_keys=None))]
    fn plan<'py>(
        &self,
        py: Python<'py>,
        direction: &Bound<'py, PyAny>,
        allow_ahead: bool,
        order_keys: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let direction = parse_direction(direction)?;
        let order_keys = order_keys_arg(order_keys)?;
        let locked = Arc::clone(&self.inner);
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let plan = locked
                .tracked()
                .plan(direction, allow_ahead, order_keys.as_ref())
                .await?;
            plan_handles(plan, locked.tracked().records(), direction, locked.id())
        })
    }

    /// Execute one planned SQL step, up or down, from the held bytes, and
    /// settle its record. `on_attempt(text)` hears each attempt that timed
    /// out waiting for a lock (ADR-0044). Returns `{"ok", "ms", "error",
    /// "message"}`.
    ///
    /// # Errors
    /// `RunRefused` when the run lock was lost or a rebuild is refused;
    /// `ValueError` for a step of another plan or a data step; a database
    /// error writing a record; the first exception `on_attempt` raised, once
    /// the step has settled its record.
    #[pyo3(signature = (step, on_attempt=None))]
    fn execute<'py>(
        &self,
        py: Python<'py>,
        step: PyRef<'py, StepHandle>,
        on_attempt: Option<Py<PyAny>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let planned = Arc::clone(owned(&step, &self.inner)?);
        let down = step.down;
        let (locked, ddl) = (Arc::clone(&self.inner), Arc::clone(&self.ddl));
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            // A callback that raises is the caller's bug: its first error is
            // raised once the step has settled its record (stopping mid-step
            // would leave the record started with nothing running).
            let mut callback_error: Option<PyErr> = None;
            let outcome = locked
                .execute(&planned, down, &ddl, |attempt| {
                    if let Some(callback) = &on_attempt
                        && callback_error.is_none()
                    {
                        let text = attempt.describe();
                        callback_error = Python::attach(|py| callback.call1(py, (text,)).err());
                    }
                })
                .await?;
            if let Some(err) = callback_error {
                return Err(err);
            }
            to_py_attached(&outcome)
        })
    }

    /// A data step's started record, written on its own before it runs:
    /// `kind` is the shape its `up` declares (`"atomic"` / `"chunked"`); a
    /// resumed chunked step keeps its cursor.
    fn start<'py>(
        &self,
        py: Python<'py>,
        step: PyRef<'py, StepHandle>,
        kind: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let planned = Arc::clone(owned(&step, &self.inner)?);
        let standing = step.standing.clone();
        let kind = record_kind(kind)?;
        let locked = Arc::clone(&self.inner);
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            locked.start(&planned, kind, standing.as_ref()).await
        })
    }

    /// One committed batch of a chunked `up` on its record, inside the
    /// batch's transaction (`tx`, its route).
    fn advance<'py>(
        &self,
        py: Python<'py>,
        step: PyRef<'py, StepHandle>,
        cursor: Option<String>,
        rows_done: i64,
        tx: Py<crate::state::RouteHandle>,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.batch(py, step, cursor, rows_done, tx, false)
    }

    /// One committed batch of a chunked `down` on its record (reverting),
    /// inside the batch's transaction.
    fn advance_revert<'py>(
        &self,
        py: Python<'py>,
        step: PyRef<'py, StepHandle>,
        cursor: Option<String>,
        rows_done: i64,
        tx: Py<crate::state::RouteHandle>,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.batch(py, step, cursor, rows_done, tx, true)
    }

    /// A data step's finished record: on `tx` (the step's or its last
    /// batch's transaction) so it commits with the work, or on its own; a
    /// chunked step's last `cursor` and `rows_done` with it.
    #[pyo3(signature = (step, ms, tx=None, *, cursor=None, rows_done=None))]
    fn finish<'py>(
        &self,
        py: Python<'py>,
        step: PyRef<'py, StepHandle>,
        ms: i64,
        tx: Option<Py<crate::state::RouteHandle>>,
        cursor: Option<String>,
        rows_done: Option<i64>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let planned = Arc::clone(owned(&step, &self.inner)?);
        let position = position(cursor, rows_done)?;
        let slot = tx.map(|route| transaction_slot(route.get())).transpose()?;
        let locked = Arc::clone(&self.inner);
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            locked.finish(&planned, ms, slot.as_ref(), position).await
        })
    }

    /// A data step's failure on its started record, after the rollback; a
    /// chunked step's committed `cursor` and `rows_done` with it.
    #[pyo3(signature = (step, ms, error, cursor=None, rows_done=None))]
    fn fail<'py>(
        &self,
        py: Python<'py>,
        step: PyRef<'py, StepHandle>,
        ms: i64,
        error: String,
        cursor: Option<String>,
        rows_done: Option<i64>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let planned = Arc::clone(owned(&step, &self.inner)?);
        let position = position(cursor, rows_done)?;
        let locked = Arc::clone(&self.inner);
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            locked.fail(&planned, ms, error, position).await
        })
    }

    /// A chunked down that failed after a committed batch: its record stays
    /// reverting at `cursor`, carrying the error.
    fn fail_revert<'py>(
        &self,
        py: Python<'py>,
        step: PyRef<'py, StepHandle>,
        error: String,
        cursor: Option<String>,
        rows_done: i64,
    ) -> PyResult<Bound<'py, PyAny>> {
        let planned = Arc::clone(owned(&step, &self.inner)?);
        let position = (cursor, rows(rows_done)?);
        let locked = Arc::clone(&self.inner);
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            locked.fail_revert(&planned, error, position).await
        })
    }

    /// Remove a reverted step's record inside its down's transaction (`tx`).
    fn remove<'py>(
        &self,
        py: Python<'py>,
        step: PyRef<'py, StepHandle>,
        tx: Py<crate::state::RouteHandle>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let planned = Arc::clone(owned(&step, &self.inner)?);
        let slot = transaction_slot(tx.get())?;
        let locked = Arc::clone(&self.inner);
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            locked.remove(&planned, &slot).await
        })
    }

    /// Plan `ferro migrate baseline` through `target` (`None`: the head;
    /// `"0006"` or `"0006_add_teams"`).
    ///
    /// # Errors
    /// `RunRefused`: records already exist, the target is not in the
    /// directory, the directory is unreadable, a step has no rendering.
    #[pyo3(signature = (target=None))]
    fn plan_baseline(&self, target: Option<&str>) -> PyResult<BaselinePlan> {
        Ok(BaselinePlan {
            plan: Arc::new(self.inner.plan_baseline(target)?),
        })
    }

    /// Write a baseline's records in one transaction; `data_kinds` maps
    /// each data step's `(migration, step)` to the shape its `up` declares.
    #[pyo3(signature = (plan, data_kinds=None))]
    fn write_baseline<'py>(
        &self,
        py: Python<'py>,
        plan: PyRef<'py, BaselinePlan>,
        data_kinds: Option<std::collections::HashMap<(u16, u8), String>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let kinds = data_kinds
            .unwrap_or_default()
            .into_iter()
            .map(|(key, kind)| Ok((key, record_kind(&kind)?)))
            .collect::<PyResult<std::collections::HashMap<_, _>>>()?;
        let plan = Arc::clone(&plan.plan);
        let locked = Arc::clone(&self.inner);
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            locked.write_baseline(&plan, &kinds).await
        })
    }

    /// Delete every baseline-origin record. Returns the `(migration, step)`
    /// of each record removed (empty when there was no baseline).
    fn remove_baseline<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let locked = Arc::clone(&self.inner);
        pyo3_async_runtimes::tokio::future_into_py(
            py,
            async move { locked.remove_baseline().await },
        )
    }

    /// Plan `ferro migrate rerecord <target>`: `mode` is `"record"`,
    /// `"continue"` or `"restart"`; `order_keys` the edited chunked files'.
    ///
    /// # Errors
    /// `RunRefused` (structured) for every refusal the planner makes.
    #[pyo3(signature = (target, mode, order_keys=None))]
    fn plan_rerecord(
        &self,
        target: &str,
        mode: &str,
        order_keys: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<RerecordPlan> {
        let mode: ferro_migrate::run_plan::RerecordMode =
            parse_json(&format!("\"{mode}\""), "mode")?;
        let order_keys = order_keys_arg(order_keys)?.unwrap_or_default();
        Ok(RerecordPlan {
            action: Arc::new(self.inner.plan_rerecord(target, mode, &order_keys)?),
        })
    }

    /// Write one planned re-record; a data step's record takes `kind`, the
    /// shape its edited `up` declares.
    #[pyo3(signature = (action, kind=None))]
    fn rerecord<'py>(
        &self,
        py: Python<'py>,
        action: PyRef<'py, RerecordPlan>,
        kind: Option<&str>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let kind = kind.map(record_kind).transpose()?;
        let action = Arc::clone(&action.action);
        let locked = Arc::clone(&self.inner);
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            locked.rerecord(&action, kind).await
        })
    }

    /// Close the Postgres lock connection without releasing the lock, as a
    /// dropped network connection would. Test-only.
    fn _close_lock_connection_for_test<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let locked = Arc::clone(&self.inner);
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            locked.close_lock_connection_for_test().await;
            Ok(())
        })
    }

    fn __repr__(&self) -> String {
        format!("<LockedDatabase {}>", self.inner.tracked().tracking_table())
    }
}

impl LockedDatabase {
    fn batch<'py>(
        &self,
        py: Python<'py>,
        step: PyRef<'py, StepHandle>,
        cursor: Option<String>,
        rows_done: i64,
        tx: Py<crate::state::RouteHandle>,
        reverting: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let planned = Arc::clone(owned(&step, &self.inner)?);
        let position = batch_position(cursor, rows_done)?;
        let slot = transaction_slot(tx.get())?;
        let locked = Arc::clone(&self.inner);
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            locked.advance(&planned, position, &slot, reverting).await
        })
    }
}

/// What `ferro migrate baseline` would record (`plan_baseline`).
#[pyclass(frozen, module = "ferro._core")]
pub struct BaselinePlan {
    plan: Arc<crate::run::BaselinePlan>,
}

#[pymethods]
impl BaselinePlan {
    /// `NNNN_<name>` of the migration whose snapshot the database is checked
    /// against.
    #[getter]
    fn target(&self) -> &str {
        &self.plan.target
    }

    /// The target's schema snapshot (a dict).
    #[getter]
    fn snapshot(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        to_py(py, &self.plan.snapshot)
    }

    /// `NNNN_<name>` of every migration it records, in order.
    #[getter]
    fn recorded(&self) -> Vec<String> {
        self.plan.recorded.clone()
    }

    /// `NNNN_<name>/<file>` of every data step recorded without running.
    #[getter]
    fn data_steps(&self) -> Vec<String> {
        self.plan.data_steps.clone()
    }

    /// `(migration, step, path)` of each data step: the file whose `up`
    /// declaration gives its record's kind.
    #[getter]
    fn data_step_files(&self) -> Vec<(u16, u8, String)> {
        self.plan
            .data_files
            .iter()
            .map(|(m, s, path)| (*m, *s, path.display().to_string()))
            .collect()
    }

    /// How many step records it writes.
    #[getter]
    fn steps(&self) -> usize {
        self.plan.records.len()
    }
}

/// What `ferro migrate rerecord` would change (`plan_rerecord`).
#[pyclass(frozen, module = "ferro._core")]
pub struct RerecordPlan {
    action: Arc<ferro_migrate::run_plan::RerecordAction>,
}

#[pymethods]
impl RerecordPlan {
    /// `NNNN`.
    #[getter]
    fn migration(&self) -> u16 {
        self.action.migration
    }

    /// `NN`.
    #[getter]
    fn step(&self) -> u8 {
        self.action.step
    }

    /// `NNNN_<name>`.
    #[getter]
    fn migration_name(&self) -> &str {
        &self.action.migration_name
    }

    /// The file the record will name.
    #[getter]
    fn file(&self) -> &str {
        &self.action.file
    }

    /// That file's path (a data step's declared kind is read from it).
    #[getter]
    fn path(&self) -> String {
        self.action.path.display().to_string()
    }

    /// The checksum the record holds now.
    #[getter]
    fn old_checksum(&self) -> &str {
        &self.action.old_checksum
    }

    /// The checksum it will hold.
    #[getter]
    fn new_checksum(&self) -> &str {
        &self.action.new_checksum
    }

    /// A Python data step: `rerecord` needs the kind its `up` declares.
    #[getter]
    fn data(&self) -> bool {
        self.action.data
    }
}
