//! Centralized state management for the Ferro ORM.
//!
//! This module holds the global connection pool, the model registry,
//! and the Identity Map used for object tracking.

use crate::backend::{EngineConnection, EngineHandle};
use crate::session_settings::SessionSetting;
use dashmap::DashMap;
use ferro_schema_ir::{IrEnvelope, SchemaColumn, SchemaIrPayload};
use once_cell::sync::Lazy;
use pyo3::IntoPyObjectExt;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, RwLock};
use tokio::sync::Mutex;

pub(crate) use ferro_ddl_lowering::Dialect;

/// Primary-key facts scanned once from SchemaIR columns at registration
/// (FF-G G2): the per-operation `properties` walks collapsed to one place.
/// Registration is immutable after insert (a re-registration swaps the
/// whole `Arc<RegisteredModel>`), so this can never go stale.
#[derive(Debug)]
pub struct ModelMeta {
    /// First column flagged `primary_key: true`, in IR column order.
    pub pk_col: Option<String>,
    /// The PK's `autoincrement` flag; `true` when there is no PK at all,
    /// matching the historical per-site default that `build_save_sql` receives.
    pub pk_autoincrement: bool,
}

impl ModelMeta {
    /// Scan SchemaIR columns for primary-key metadata (FF-G G2 / #236).
    pub fn from_columns(columns: &[SchemaColumn]) -> Self {
        let mut pk_col = None;
        let mut pk_autoincrement = true;
        for col in columns {
            if col.primary_key {
                pk_col = Some(col.name.clone());
                pk_autoincrement = col.autoincrement;
                break;
            }
        }
        ModelMeta { pk_col, pk_autoincrement }
    }
}

/// One registered model: codec plan and PK metadata compiled from SchemaIR
/// columns at registration (FF-C C1 / #238).
#[derive(Debug)]
pub struct RegisteredModel {
    /// Resolved physical table name (configured `__ferro_table__` or the
    /// lowercased class name), pushed from Python at registration (FF-E E2).
    /// Rust never derives a table name from a model name.
    pub table_name: String,
    /// Per-column codec decisions, compiled once at registration.
    pub codec_plan: crate::codec_plan::ModelCodecPlan,
    /// Primary-key metadata, scanned once at registration (FF-G G2).
    pub meta: ModelMeta,
    /// Column names in SchemaIR declaration order (#286): the instances
    /// fetch walker enumerates each included hop's complete column list to
    /// widen the SELECT deterministically (every populated hop is a complete
    /// row — the complete-instance invariant, ADR-0008).
    pub column_names: Vec<String>,
}

impl RegisteredModel {
    /// Compile the codec plan and wrap the registration for the registry.
    pub fn new(columns: Vec<SchemaColumn>, table_name: String) -> Result<Arc<Self>, String> {
        let codec_plan = crate::codec_plan::ModelCodecPlan::compile_from_columns(&columns)?;
        let meta = ModelMeta::from_columns(&columns);
        let column_names = columns.iter().map(|col| col.name.clone()).collect();
        Ok(Arc::new(RegisteredModel {
            table_name,
            codec_plan,
            meta,
            column_names,
        }))
    }
}

#[cfg(test)]
impl RegisteredModel {
    /// Test-only registration when only a JSON schema fixture is available.
    pub fn new_for_test(schema: serde_json::Value, table_name: String) -> Arc<Self> {
        Self::new(
            crate::schema::infer_test_schema_columns(&schema),
            table_name,
        )
        .expect("test model registration")
    }
}

/// Global registry mapping model names to their registration (schema + codec plan).
pub static MODEL_REGISTRY: Lazy<RwLock<HashMap<String, Arc<RegisteredModel>>>> =
    Lazy::new(|| RwLock::new(HashMap::new()));

/// Extract the qualified Ferro identity stamped on a model class (FF-E E1).
///
/// # Errors
/// `PyTypeError` when the object is not a Ferro model class.
pub fn model_identity(cls: &Bound<'_, PyAny>) -> PyResult<String> {
    let attr = cls.getattr("__ferro_identity__").map_err(|_| {
        pyo3::exceptions::PyTypeError::new_err(
            "Object is not a Ferro model class (missing __ferro_identity__)",
        )
    })?;
    attr.extract::<String>().map_err(|_| {
        pyo3::exceptions::PyTypeError::new_err("__ferro_identity__ must be a str")
    })
}

/// Look up one registration by registry key.
///
/// # Errors
/// `PyRuntimeError` when the registry lock fails or the model is unknown.
pub fn registered_model(name: &str) -> PyResult<Arc<RegisteredModel>> {
    MODEL_REGISTRY
        .read()
        .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Model Registry"))?
        .get(name)
        .cloned()
        .ok_or_else(|| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("Model '{}' not found", name))
        })
}

/// Look up the registration whose physical table is `table_name` (#270 relation
/// traversal): the owning model of a joined hop table, for typed binds on
/// traversed columns.
///
/// Models cannot share a table — the metaclass enforces one table per model — so
/// a table→registration resolution is unambiguous. Used once per distinct joined
/// table per query (not per value), mirroring [`registered_model`]'s single
/// resolution for the root.
///
/// # Errors
/// `PyRuntimeError` when the registry lock fails or no registered model owns the
/// table (a loud error — a traversed hop must never fall back to root/generic
/// binds).
pub fn registration_for_table(table_name: &str) -> PyResult<Arc<RegisteredModel>> {
    MODEL_REGISTRY
        .read()
        .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Model Registry"))?
        .values()
        .find(|model| model.table_name == table_name)
        .cloned()
        .ok_or_else(|| {
            pyo3::exceptions::PyRuntimeError::new_err(format!(
                "No registered model owns table '{table_name}'; cannot resolve typed \
                 binds for a traversed relation"
            ))
        })
}

/// Python-compiled SchemaIR modelset pushed by the `connect`/`migrate` wrappers.
///
/// Populated before `internal_migrate` runs so the runtime diff can consume the
/// canonical Python domain IR instead of re-deriving it on the Rust side.
pub static SCHEMA_IR_MODELSET: Lazy<RwLock<Option<IrEnvelope<SchemaIrPayload>>>> =
    Lazy::new(|| RwLock::new(None));

/// Fingerprint of the modelset currently installed in the runtime.
///
/// Part of the atomic bulk-install unit (#244): set only on a successful
/// [`install_registration`] and cleared by [`clear_registration`], both inside
/// the same lock scope as [`MODEL_REGISTRY`] and [`SCHEMA_IR_MODELSET`]. The
/// fingerprint gate reads it to skip re-installing a modelset the runtime
/// already holds; keeping it inside the swap window means the gate can never
/// match against schema the runtime does not actually hold.
pub static INSTALLED_FINGERPRINT: Lazy<RwLock<Option<String>>> = Lazy::new(|| RwLock::new(None));

/// Process-wide count of actual bulk installs performed (a fingerprint-skip
/// does not bump it). Test-only instrument mirroring the
/// `_catalog_query_count_for_test` precedent: incremented at the single install
/// choke point in [`install_registration`] and read from Python via
/// `_bulk_install_count_for_test`.
pub static BULK_INSTALL_COUNT: AtomicU64 = AtomicU64::new(0);

/// Whether the runtime already holds the modelset named by `fingerprint`.
///
/// Lets the FFI wrapper short-circuit a warm reconnect *before* parsing the
/// payload — [`install_registration`] re-checks the gate under the same read,
/// so this is a pure fast-path optimization, never the correctness gate.
///
/// # Errors
/// `PyRuntimeError` when the fingerprint lock is poisoned.
pub fn installed_fingerprint_matches(fingerprint: &str) -> PyResult<bool> {
    let recorded = INSTALLED_FINGERPRINT.read().map_err(|_| {
        pyo3::exceptions::PyRuntimeError::new_err("Failed to lock installed fingerprint")
    })?;
    Ok(recorded.as_deref() == Some(fingerprint))
}

/// Atomically install the column registry, schema modelset, and recorded
/// fingerprint from one assembled modelset payload (#244).
///
/// Build-then-swap: the incoming column registry is fully constructed and
/// validated *before* any store lock is taken, then all three stores are
/// swapped under simultaneously-held write guards — all or nothing. A build or
/// validation failure returns an error and leaves the previously installed
/// registration and its fingerprint untouched (retained-last-good; never an
/// emptied runtime).
///
/// Fingerprint gate: when `fingerprint` equals the recorded one this is a no-op
/// — no swap, and the bulk-install counter is not bumped. Returns `true` when an
/// install was performed, `false` when the gate skipped it.
///
/// # Errors
/// `PyValueError` when a model's columns cannot compile a codec plan;
/// `PyRuntimeError` when a store lock is poisoned.
pub fn install_registration(
    envelope: IrEnvelope<SchemaIrPayload>,
    fingerprint: String,
) -> PyResult<bool> {
    // Fingerprint gate — a cheap read, released before any building begins so
    // build-then-swap never holds a lock while constructing the new registry.
    {
        let recorded = INSTALLED_FINGERPRINT.read().map_err(|_| {
            pyo3::exceptions::PyRuntimeError::new_err("Failed to lock installed fingerprint")
        })?;
        if recorded.as_deref() == Some(fingerprint.as_str()) {
            return Ok(false);
        }
    }

    // Build the entire new column registry up front. Any failure here aborts
    // before a single store is touched.
    let mut new_registry: HashMap<String, Arc<RegisteredModel>> =
        HashMap::with_capacity(envelope.payload.models.len());
    for model in &envelope.payload.models {
        let registered =
            RegisteredModel::new(model.columns.clone(), model.table_name.clone())
                .map_err(pyo3::exceptions::PyValueError::new_err)?;
        new_registry.insert(model.model_name.clone(), registered);
    }

    // Swap all three stores under simultaneously-held write guards so no reader
    // can observe a half-installed runtime. Lock order (registry → modelset →
    // fingerprint) is fixed and never nested-held elsewhere, so it cannot
    // deadlock.
    let mut registry_guard = MODEL_REGISTRY.write().map_err(|_| {
        pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Model Registry")
    })?;
    let mut modelset_guard = SCHEMA_IR_MODELSET.write().map_err(|_| {
        pyo3::exceptions::PyRuntimeError::new_err("Failed to lock SchemaIR modelset")
    })?;
    let mut fingerprint_guard = INSTALLED_FINGERPRINT.write().map_err(|_| {
        pyo3::exceptions::PyRuntimeError::new_err("Failed to lock installed fingerprint")
    })?;

    *registry_guard = new_registry;
    *modelset_guard = Some(envelope);
    *fingerprint_guard = Some(fingerprint);
    BULK_INSTALL_COUNT.fetch_add(1, Ordering::Relaxed);

    Ok(true)
}

/// Clear the entire installed registration — column registry, schema modelset,
/// and recorded fingerprint — under simultaneously-held write guards so the
/// fingerprint is never left pointing at schema the runtime no longer holds
/// (#244). Used by `clear_registry()` and runtime teardown.
///
/// # Errors
/// `PyRuntimeError` when a store lock is poisoned.
pub fn clear_registration() -> PyResult<()> {
    let mut registry_guard = MODEL_REGISTRY.write().map_err(|_| {
        pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Model Registry")
    })?;
    let mut modelset_guard = SCHEMA_IR_MODELSET.write().map_err(|_| {
        pyo3::exceptions::PyRuntimeError::new_err("Failed to lock SchemaIR modelset")
    })?;
    let mut fingerprint_guard = INSTALLED_FINGERPRINT.write().map_err(|_| {
        pyo3::exceptions::PyRuntimeError::new_err("Failed to lock installed fingerprint")
    })?;

    registry_guard.clear();
    *modelset_guard = None;
    *fingerprint_guard = None;
    Ok(())
}

/// The global runtime engine, initialized via `connect()`.
pub static ENGINE: Lazy<RwLock<Option<Arc<EngineHandle>>>> = Lazy::new(|| RwLock::new(None));

/// Registered runtime engines keyed by user-facing connection name.
pub static CONNECTION_REGISTRY: Lazy<RwLock<HashMap<String, Arc<EngineHandle>>>> =
    Lazy::new(|| RwLock::new(HashMap::new()));

/// Name of the default connection used by legacy unqualified operations.
pub static DEFAULT_CONNECTION_NAME: Lazy<RwLock<Option<String>>> = Lazy::new(|| RwLock::new(None));

/// Resolve a connection name and engine by explicit connection name, or by the selected default.
///
/// # Arguments
/// * `using` — Registered connection name, or `None` to use [`DEFAULT_CONNECTION_NAME`].
///
/// # Returns
/// `(connection_name, engine)` for the resolved route.
///
/// # Errors
/// * `PyValueError` — Unknown or invalid connection name.
/// * `PyRuntimeError` — No default selected when required, registry lock failure, or engine missing.
pub fn connection_for_route(using: Option<String>) -> PyResult<(String, Arc<EngineHandle>)> {
    let Some(connection_name) = using else {
        let default_name = DEFAULT_CONNECTION_NAME
            .read()
            .map_err(|_| {
                pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Default Connection")
            })?
            .clone();

        if let Some(connection_name) = default_name {
            let engine = CONNECTION_REGISTRY
                .read()
                .map_err(|_| {
                    pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Connection Registry")
                })?
                .get(&connection_name)
                .cloned()
                .ok_or_else(|| {
                    crate::errors::interface_error(format!(
                        "Default connection '{}' is not registered",
                        connection_name
                    ))
                })?;
            return Ok((connection_name, engine));
        }

        let has_connections = !CONNECTION_REGISTRY
            .read()
            .map_err(|_| {
                pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Connection Registry")
            })?
            .is_empty();
        let has_default = DEFAULT_CONNECTION_NAME
            .read()
            .map_err(|_| {
                pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Default Connection")
            })?
            .is_some();

        if has_connections && !has_default {
            return Err(crate::errors::interface_error(
                "No default connection selected",
            ));
        }

        return Err(crate::errors::interface_error("Engine not initialized"));
    };

    if connection_name.is_empty()
        || !connection_name
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || ch == '_')
    {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "Invalid connection name '{}'",
            connection_name
        )));
    }

    CONNECTION_REGISTRY
        .read()
        .map_err(|_| {
            pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Connection Registry")
        })?
        .get(&connection_name)
        .cloned()
        .map(|engine| (connection_name.clone(), engine))
        .ok_or_else(|| {
            crate::errors::interface_error(format!(
                "Connection '{}' is not registered",
                connection_name
            ))
        })
}

/// Resolve an engine by explicit connection name, or by the selected default.
///
/// # Arguments
/// * `using` — Registered connection name, or `None` for the default connection.
///
/// # Returns
/// Shared [`EngineHandle`] for the route.
///
/// # Errors
/// Same as [`connection_for_route`].
pub fn engine_for_connection(using: Option<String>) -> PyResult<Arc<EngineHandle>> {
    connection_for_route(using).map(|(_, engine)| engine)
}

/// Opaque, immutable route for one operation (FF-D D3).
///
/// Resolved exactly once in Python's `resolve_operation_scope` /
/// `resolve_transaction_scope` and passed by value through every FFI
/// operation. `connection_name` is non-optional: the removed ambient-default
/// path is unrepresentable, not an error branch.
#[pyclass(frozen, module = "ferro._core")]
pub struct RouteHandle {
    pub tx_id: Option<String>,
    pub connection_name: String,
    pub session_id: Option<String>,
}

#[pymethods]
impl RouteHandle {
    #[new]
    #[pyo3(signature = (connection_name, tx_id=None, session_id=None))]
    fn new(connection_name: String, tx_id: Option<String>, session_id: Option<String>) -> Self {
        Self { tx_id, connection_name, session_id }
    }

    #[getter]
    fn connection_name(&self) -> &str {
        &self.connection_name
    }

    #[getter]
    fn tx_id(&self) -> Option<&str> {
        self.tx_id.as_deref()
    }

    #[getter]
    fn session_id(&self) -> Option<&str> {
        self.session_id.as_deref()
    }
}

/// Active transaction handle (root `BEGIN` or nested `SAVEPOINT`).
#[derive(Clone)]
pub struct TransactionHandle {
    /// Shared mutex around the live [`EngineConnection`].
    pub conn: TransactionConnection,
    /// Savepoint name when this is a nested transaction; `None` for the root.
    pub savepoint_name: Option<String>,
    /// Connection name the transaction was opened on (for routing and identity map keys).
    pub connection_name: String,
}

/// Async mutex wrapper around a checked-out [`EngineConnection`].
pub type TransactionConnection = Arc<Mutex<EngineConnection>>;

impl TransactionHandle {
    /// Create a root transaction handle after `BEGIN`.
    ///
    /// # Arguments
    /// * `conn` — Checked-out connection that has started a transaction.
    /// * `connection_name` — Registered Ferro connection name.
    pub fn root(conn: EngineConnection, connection_name: String) -> Self {
        Self {
            conn: Arc::new(Mutex::new(conn)),
            savepoint_name: None,
            connection_name,
        }
    }

    /// Create a nested transaction handle after `SAVEPOINT`.
    ///
    /// # Arguments
    /// * `conn` — Parent transaction connection (shared with the root).
    /// * `savepoint_name` — Generated savepoint identifier.
    /// * `connection_name` — Registered Ferro connection name.
    pub fn nested(
        conn: TransactionConnection,
        savepoint_name: String,
        connection_name: String,
    ) -> Self {
        Self {
            conn,
            savepoint_name: Some(savepoint_name),
            connection_name,
        }
    }
}

/// Global registry for active transactions.
/// Maps Transaction ID -> backend connection plus optional savepoint.
pub static TRANSACTION_REGISTRY: Lazy<DashMap<String, TransactionHandle>> = Lazy::new(DashMap::new);

/// Session-scoped runtime state for Phase 6 sessionized execution.
///
/// A session pins connection routing and keeps transactional/identity state local
/// to that session so concurrent sessions do not bleed mutable runtime state.
///
/// The pinned connection name itself is *not* stored here: before FF-D D3 it
/// backed an ambient ("ask the session what its connection is") fallback in
/// `active_engine_for_connection`. That fallback is gone — Python's
/// `resolve_operation_scope`/`resolve_transaction_scope` resolve the session's
/// connection once and bake it into the `RouteHandle`, so Rust only ever sees
/// an already-resolved `connection_name` and never re-derives it from the
/// session.
pub struct SessionState {
    /// Transactions opened within this session (isolated from global registry).
    pub transaction_registry: DashMap<String, TransactionHandle>,
    /// Effective session settings, snapshotted at open in declaration order
    /// (see [`crate::session_settings`]). Empty for the vast majority of
    /// sessions, which then pay nothing for the feature.
    pub settings: Vec<SessionSetting>,
    /// Session-local identity map — the *only* identity map (FF-D D2:
    /// sessionless operations cache nothing); values are `weakref.ref`
    /// objects (FF-D D1).
    /// INVARIANT (FF-G G4c): access only while holding the GIL — sweeps call
    /// into Python under a shard guard, so GIL-before-shard is the required
    /// lock order (asserted by `debug_assert_gil_held` in operations.rs).
    pub identity_map: DashMap<(String, String, String), Py<PyAny>>,
    /// Amortized-sweep op counter for `identity_map`.
    pub identity_ops: AtomicUsize,
}

impl SessionState {
    /// Allocate session state carrying `settings` (empty for a plain session).
    pub fn new(settings: Vec<SessionSetting>) -> Self {
        Self {
            transaction_registry: DashMap::new(),
            settings,
            identity_map: DashMap::new(),
            identity_ops: AtomicUsize::new(0),
        }
    }
}

/// Global registry of active sessions.
pub static SESSION_REGISTRY: Lazy<DashMap<String, Arc<SessionState>>> = Lazy::new(DashMap::new);

/// Register a new session.
///
/// # Arguments
/// * `settings` — Effective session settings in declaration order; empty for a
///   session that declares none.
///
/// # Returns
/// Opaque session UUID string for subsequent operations.
pub fn register_session(settings: Vec<SessionSetting>) -> String {
    let session_id = uuid::Uuid::new_v4().to_string();
    SESSION_REGISTRY.insert(session_id.clone(), Arc::new(SessionState::new(settings)));
    session_id
}

/// Remove a session from [`SESSION_REGISTRY`].
///
/// # Arguments
/// * `session_id` — Id returned by [`register_session`].
///
/// # Returns
/// `true` when the session existed and was removed.
pub fn unregister_session(session_id: &str) -> bool {
    SESSION_REGISTRY.remove(session_id).is_some()
}

/// Look up active session state.
///
/// # Arguments
/// * `session_id` — Id returned by [`register_session`].
///
/// # Returns
/// Shared [`SessionState`] for the session.
///
/// # Errors
/// `PyRuntimeError` when the session id is unknown.
pub fn session_state(session_id: &str) -> PyResult<Arc<SessionState>> {
    SESSION_REGISTRY
        .get(session_id)
        .map(|entry| entry.value().clone())
        .ok_or_else(|| {
            pyo3::exceptions::PyRuntimeError::new_err(format!(
                "Session '{}' is not active",
                session_id
            ))
        })
}

pub(crate) const SESSION_CLOSE_ACTIVE_TRANSACTIONS_MSG: &str =
    "Cannot close session while transactions are active. \
     Exit all transaction() blocks before closing the session.";

/// Guard that a session has no open transactions before close.
///
/// # Arguments
/// * `session_id` — Session being closed.
///
/// # Errors
/// `PyRuntimeError` with [`SESSION_CLOSE_ACTIVE_TRANSACTIONS_MSG`] when transactions remain.
pub fn ensure_session_idle_for_close(session_id: &str) -> PyResult<()> {
    let session = session_state(session_id)?;
    if !session.transaction_registry.is_empty() {
        return Err(pyo3::exceptions::PyRuntimeError::new_err(
            SESSION_CLOSE_ACTIVE_TRANSACTIONS_MSG,
        ));
    }
    Ok(())
}

/// A Rust-native representation of database values.
///
/// Used during the GIL-free parsing phase to move data from the database
/// into Rust memory before acquiring the Python GIL for object injection.
#[derive(Clone, Debug)]
pub enum RustValue {
    /// 64-bit integer (SQLite booleans may arrive as 0/1).
    BigInt(i64),
    /// IEEE double.
    Double(f64),
    /// Plain text / enum label before Python coercion.
    String(String),
    /// Boolean after schema-aware decode.
    Bool(bool),
    /// ISO datetime string (`format: date-time`).
    DateTime(String),
    /// ISO date string (`format: date`).
    Date(String),
    /// ISO time string (`format: time`).
    Time(String),
    /// Parsed JSON object or array column.
    Json(serde_json::Value),
    /// Binary column bytes.
    Blob(Vec<u8>),
    /// UUID string (`format: uuid`).
    Uuid(String),
    /// Decimal/numeric as string (preserves precision).
    Decimal(String),
    /// SQL `NULL`.
    None,
}

static DATETIME_FROMISOFORMAT: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static DATE_FROMISOFORMAT: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static TIME_FROMISOFORMAT: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static JSON_LOADS: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static UUID_CLASS: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static DECIMAL_CLASS: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

/// Resolve a module attribute path once per process and cache the handle
/// (FF-G G5): the hot decode path must not pay `py.import` + `getattr`
/// per value. Ferro is abi3/single-interpreter, so a process-lifetime
/// cache is safe.
///
/// # Errors
/// Returns a `PyErr` if the module import or attribute lookup fails.
fn cached_callable<'py>(
    py: Python<'py>,
    cell: &'static PyOnceLock<Py<PyAny>>,
    module: &str,
    attrs: &[&str],
) -> PyResult<&'py Bound<'py, PyAny>> {
    Ok(cell
        .get_or_try_init(py, || -> PyResult<Py<PyAny>> {
            let mut obj = py.import(module)?.into_any();
            for attr in attrs {
                obj = obj.getattr(*attr)?;
            }
            Ok(obj.unbind())
        })?
        .bind(py))
}

impl RustValue {
    /// Converts the Rust-native value into a Python object.
    ///
    /// # Errors
    /// Returns a `PyErr` if the conversion fails.
    pub fn into_py_any<'py>(self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        match self {
            RustValue::BigInt(i) => Ok(i.into_py_any(py)?.into_bound(py)),
            RustValue::Double(f) => Ok(f.into_py_any(py)?.into_bound(py)),
            RustValue::String(s) => Ok(s.into_py_any(py)?.into_bound(py)),
            RustValue::Bool(b) => Ok(b.into_py_any(py)?.into_bound(py)),
            RustValue::DateTime(s) => {
                cached_callable(py, &DATETIME_FROMISOFORMAT, "datetime", &["datetime", "fromisoformat"])?
                    .call1((s.replace('Z', "+00:00"),))
            }
            RustValue::Date(s) => {
                cached_callable(py, &DATE_FROMISOFORMAT, "datetime", &["date", "fromisoformat"])?
                    .call1((s,))
            }
            RustValue::Time(s) => {
                cached_callable(py, &TIME_FROMISOFORMAT, "datetime", &["time", "fromisoformat"])?
                    .call1((s,))
            }
            RustValue::Json(v) => {
                cached_callable(py, &JSON_LOADS, "json", &["loads"])?.call1((v.to_string(),))
            }
            RustValue::Blob(b) => {
                let bytes = pyo3::types::PyBytes::new(py, &b);
                Ok(bytes.into_any())
            }
            RustValue::Uuid(s) => cached_callable(py, &UUID_CLASS, "uuid", &["UUID"])?.call1((s,)),
            RustValue::Decimal(s) => {
                cached_callable(py, &DECIMAL_CLASS, "decimal", &["Decimal"])?.call1((s,))
            }
            RustValue::None => Ok(py.None().into_bound(py)),
        }
    }
}

#[cfg(test)]
mod session_close_tests {
    use super::{TransactionHandle, register_session, unregister_session, SESSION_REGISTRY};
    use crate::backend::EngineHandle;
    use sqlx::sqlite::SqlitePoolOptions;

    #[tokio::test]
    async fn close_guard_uses_transaction_registry_emptiness() {
        let session_id = register_session(Vec::new());

        let pool = SqlitePoolOptions::new()
            .max_connections(1)
            .connect("sqlite::memory:")
            .await
            .expect("sqlite memory pool");
        let engine = EngineHandle::new_sqlite(pool);
        let conn = engine
            .begin_transaction_connection()
            .await
            .expect("begin transaction connection");

        {
            let session = SESSION_REGISTRY
                .get(&session_id)
                .expect("session registered")
                .value()
                .clone();
            session.transaction_registry.insert(
                "tx-test".to_string(),
                TransactionHandle::root(conn, "default".to_string()),
            );
            assert!(!session.transaction_registry.is_empty());
        }

        SESSION_REGISTRY
            .get(&session_id)
            .expect("session registered")
            .transaction_registry
            .clear();
        assert!(
            SESSION_REGISTRY
                .get(&session_id)
                .expect("session registered")
                .transaction_registry
                .is_empty()
        );
        assert!(unregister_session(&session_id));
    }
}

#[cfg(test)]
mod install_registration_tests {
    use super::{
        clear_registration, install_registration, BULK_INSTALL_COUNT, INSTALLED_FINGERPRINT,
        MODEL_REGISTRY, SCHEMA_IR_MODELSET,
    };
    use ferro_schema_ir::{IrEnvelope, SchemaColumn, SchemaIrPayload, SchemaModel};
    use std::sync::atomic::Ordering;
    use std::sync::{Mutex, MutexGuard, OnceLock};

    /// These tests mutate the process-global registration stores, so they must
    /// not run concurrently with each other. Serialize them behind one mutex.
    fn serial_guard() -> MutexGuard<'static, ()> {
        static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
        LOCK.get_or_init(|| Mutex::new(()))
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn id_col() -> SchemaColumn {
        SchemaColumn {
            name: "id".to_string(),
            logical_type: "integer".to_string(),
            db_type: None,
            db_type_explicit: None,
            nullable: false,
            primary_key: true,
            autoincrement: true,
            unique: false,
            index: false,
            default: None,
            format: None,
            enum_values: None,
            enum_type_name: None,
            postgres_native_enum: false,
        }
    }

    fn model(name: &str, logical_type: &str) -> SchemaModel {
        SchemaModel {
            model_name: name.to_string(),
            table_name: name.to_lowercase(),
            columns: vec![
                id_col(),
                SchemaColumn {
                    name: "value".to_string(),
                    logical_type: logical_type.to_string(),
                    db_type: None,
                    db_type_explicit: None,
                    nullable: true,
                    primary_key: false,
                    autoincrement: false,
                    unique: false,
                    index: false,
                    default: None,
                    format: None,
                    enum_values: None,
                    enum_type_name: None,
                    postgres_native_enum: false,
                },
            ],
            foreign_keys: vec![],
            indexes: vec![],
            uniques: vec![],
            checks: vec![],
            table_checks: vec![],
            row_security: None,
        }
    }

    fn envelope(models: Vec<SchemaModel>) -> IrEnvelope<SchemaIrPayload> {
        IrEnvelope {
            ir_kind: "schema".to_string(),
            ir_version: 1,
            payload: SchemaIrPayload {
                dialect_agnostic: true,
                models,
            },
        }
    }

    fn registry_keys() -> Vec<String> {
        let mut keys: Vec<String> = MODEL_REGISTRY
            .read()
            .expect("registry read")
            .keys()
            .cloned()
            .collect();
        keys.sort();
        keys
    }

    fn recorded_fingerprint() -> Option<String> {
        INSTALLED_FINGERPRINT.read().expect("fingerprint read").clone()
    }

    #[test]
    fn install_gate_and_build_then_swap() {
        let _guard = serial_guard();
        clear_registration().expect("reset");

        let base_count = BULK_INSTALL_COUNT.load(Ordering::Relaxed);

        // First install: two valid models — all three stores populated, counter
        // bumped once.
        let installed = install_registration(
            envelope(vec![model("Alpha", "string"), model("Beta", "integer")]),
            "fp-1".to_string(),
        )
        .expect("first install");
        assert!(installed, "first install should perform a swap");
        assert_eq!(registry_keys(), vec!["Alpha".to_string(), "Beta".to_string()]);
        assert_eq!(recorded_fingerprint().as_deref(), Some("fp-1"));
        assert!(SCHEMA_IR_MODELSET.read().unwrap().is_some());
        assert_eq!(BULK_INSTALL_COUNT.load(Ordering::Relaxed) - base_count, 1);

        // Fingerprint gate: same fingerprint is a no-op — no swap, no bump.
        let skipped = install_registration(
            envelope(vec![model("Alpha", "string"), model("Beta", "integer")]),
            "fp-1".to_string(),
        )
        .expect("gated install");
        assert!(!skipped, "matching fingerprint should skip the swap");
        assert_eq!(BULK_INSTALL_COUNT.load(Ordering::Relaxed) - base_count, 1);

        // Build-then-swap: a payload whose second model fails to compile (an
        // unknown logical_type has no codec) must abort the whole install and
        // leave the previous registration + fingerprint untouched.
        let result = install_registration(
            envelope(vec![model("Gamma", "string"), model("Broken", "not_a_type")]),
            "fp-2".to_string(),
        );
        assert!(result.is_err(), "invalid model should fail the install");
        assert_eq!(
            registry_keys(),
            vec!["Alpha".to_string(), "Beta".to_string()],
            "retained-last-good: registry unchanged after a failed install"
        );
        assert_eq!(recorded_fingerprint().as_deref(), Some("fp-1"));
        assert_eq!(
            BULK_INSTALL_COUNT.load(Ordering::Relaxed) - base_count,
            1,
            "a failed install must not bump the push counter"
        );

        // clear_registration wipes all three stores together.
        clear_registration().expect("clear");
        assert!(registry_keys().is_empty());
        assert!(recorded_fingerprint().is_none());
        assert!(SCHEMA_IR_MODELSET.read().unwrap().is_none());
    }
}

#[cfg(test)]
mod model_meta_tests {
    use super::ModelMeta;
    use ferro_schema_ir::SchemaColumn;

    #[test]
    fn from_columns_no_primary_key_yields_none_and_default_autoincrement() {
        let meta = ModelMeta::from_columns(&[SchemaColumn {
            name: "name".to_string(),
            logical_type: "string".to_string(),
            db_type: None,
            db_type_explicit: None,
            nullable: true,
            primary_key: false,
            autoincrement: false,
            unique: false,
            index: false,
            default: None,
            format: None,
            enum_values: None,
            enum_type_name: None,
            postgres_native_enum: false,
        }]);
        assert_eq!(meta.pk_col, None);
        assert!(meta.pk_autoincrement);
    }

    #[test]
    fn from_columns_pk_without_autoincrement_key_defaults_true() {
        let meta = ModelMeta::from_columns(&[SchemaColumn {
            name: "id".to_string(),
            logical_type: "integer".to_string(),
            db_type: None,
            db_type_explicit: None,
            nullable: false,
            primary_key: true,
            autoincrement: true,
            unique: false,
            index: false,
            default: None,
            format: None,
            enum_values: None,
            enum_type_name: None,
            postgres_native_enum: false,
        }]);
        assert_eq!(meta.pk_col.as_deref(), Some("id"));
        assert!(meta.pk_autoincrement);
    }

    #[test]
    fn from_columns_pk_with_autoincrement_false_is_preserved() {
        let meta = ModelMeta::from_columns(&[SchemaColumn {
            name: "id".to_string(),
            logical_type: "string".to_string(),
            db_type: None,
            db_type_explicit: None,
            nullable: false,
            primary_key: true,
            autoincrement: false,
            unique: false,
            index: false,
            default: None,
            format: None,
            enum_values: None,
            enum_type_name: None,
            postgres_native_enum: false,
        }]);
        assert_eq!(meta.pk_col.as_deref(), Some("id"));
        assert!(!meta.pk_autoincrement);
    }

    #[test]
    fn from_columns_first_flagged_column_in_declaration_order_wins() {
        let meta = ModelMeta::from_columns(&[
            SchemaColumn {
                name: "b_id".to_string(),
                logical_type: "integer".to_string(),
                db_type: None,
                db_type_explicit: None,
                nullable: false,
                primary_key: true,
                autoincrement: false,
                unique: false,
                index: false,
                default: None,
                format: None,
                enum_values: None,
                enum_type_name: None,
                postgres_native_enum: false,
            },
            SchemaColumn {
                name: "a_id".to_string(),
                logical_type: "integer".to_string(),
                db_type: None,
                db_type_explicit: None,
                nullable: false,
                primary_key: true,
                autoincrement: true,
                unique: false,
                index: false,
                default: None,
                format: None,
                enum_values: None,
                enum_type_name: None,
                postgres_native_enum: false,
            },
        ]);
        assert_eq!(meta.pk_col.as_deref(), Some("b_id"));
        assert!(!meta.pk_autoincrement);
    }
}
