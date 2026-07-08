//! Centralized state management for the Ferro ORM.
//!
//! This module holds the global connection pool, the model registry,
//! and the Identity Map used for object tracking.

use crate::backend::{EngineConnection, EngineHandle};
use dashmap::DashMap;
use ferro_schema_ir::{IrEnvelope, SchemaColumn, SchemaIrPayload};
use once_cell::sync::Lazy;
use pyo3::IntoPyObjectExt;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use std::collections::HashMap;
use std::sync::atomic::AtomicUsize;
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
}

impl RegisteredModel {
    /// Compile the codec plan and wrap the registration for the registry.
    pub fn new(columns: Vec<SchemaColumn>, table_name: String) -> Result<Arc<Self>, String> {
        let codec_plan = crate::codec_plan::ModelCodecPlan::compile_from_columns(&columns)?;
        let meta = ModelMeta::from_columns(&columns);
        Ok(Arc::new(RegisteredModel {
            table_name,
            codec_plan,
            meta,
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

/// Python-compiled SchemaIR modelset pushed by the `connect`/`migrate` wrappers.
///
/// Populated before `internal_migrate` runs so the runtime diff can consume the
/// canonical Python domain IR instead of re-deriving it on the Rust side.
pub static SCHEMA_IR_MODELSET: Lazy<RwLock<Option<IrEnvelope<SchemaIrPayload>>>> =
    Lazy::new(|| RwLock::new(None));

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
    /// Allocate empty session state.
    pub fn new() -> Self {
        Self {
            transaction_registry: DashMap::new(),
            identity_map: DashMap::new(),
            identity_ops: AtomicUsize::new(0),
        }
    }
}

/// Global registry of active sessions.
pub static SESSION_REGISTRY: Lazy<DashMap<String, Arc<SessionState>>> = Lazy::new(DashMap::new);

/// Register a new session.
///
/// # Returns
/// Opaque session UUID string for subsequent operations.
pub fn register_session() -> String {
    let session_id = uuid::Uuid::new_v4().to_string();
    SESSION_REGISTRY.insert(session_id.clone(), Arc::new(SessionState::new()));
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
        let session_id = register_session();

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
