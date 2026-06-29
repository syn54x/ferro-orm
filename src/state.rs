//! Centralized state management for the Ferro ORM.
//!
//! This module holds the global connection pool, the model registry,
//! and the Identity Map used for object tracking.

use crate::backend::{BackendKind, EngineConnection, EngineHandle};
use dashmap::DashMap;
use ferro_schema_ir::{IrEnvelope, SchemaIrPayload};
use once_cell::sync::Lazy;
use pyo3::IntoPyObjectExt;
use pyo3::prelude::*;
use std::collections::HashMap;
use std::sync::{Arc, RwLock};
use tokio::sync::Mutex;

/// Backward-compatible name for query/DDL builder selection.
pub type SqlDialect = BackendKind;

/// Global registry mapping model names to their Pydantic-generated JSON schemas.
pub static MODEL_REGISTRY: Lazy<RwLock<HashMap<String, serde_json::Value>>> =
    Lazy::new(|| RwLock::new(HashMap::new()));

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
                    pyo3::exceptions::PyRuntimeError::new_err(format!(
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
            return Err(pyo3::exceptions::PyRuntimeError::new_err(
                "No default connection selected",
            ));
        }

        return Err(pyo3::exceptions::PyRuntimeError::new_err(
            "Engine not initialized",
        ));
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
            pyo3::exceptions::PyValueError::new_err(format!(
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

/// Identity Map used for object tracking and deduplication.
///
/// Maps `(ConnectionName, ModelName, PrimaryKeyValue)` to a live Python object.
pub static IDENTITY_MAP: Lazy<DashMap<(String, String, String), Py<PyAny>>> =
    Lazy::new(DashMap::new);

/// Session-scoped runtime state for Phase 6 sessionized execution.
///
/// A session pins connection routing and keeps transactional/identity state local
/// to that session so concurrent sessions do not bleed mutable runtime state.
pub struct SessionState {
    /// Connection pinned for the lifetime of this session.
    pub connection_name: String,
    /// Transactions opened within this session (isolated from global registry).
    pub transaction_registry: DashMap<String, TransactionHandle>,
    /// Session-local identity map (isolated from global [`IDENTITY_MAP`]).
    pub identity_map: DashMap<(String, String, String), Py<PyAny>>,
}

impl SessionState {
    /// Allocate empty session state for `connection_name`.
    pub fn new(connection_name: String) -> Self {
        Self {
            connection_name,
            transaction_registry: DashMap::new(),
            identity_map: DashMap::new(),
        }
    }
}

/// Global registry of active sessions.
pub static SESSION_REGISTRY: Lazy<DashMap<String, Arc<SessionState>>> = Lazy::new(DashMap::new);

/// Register a new session bound to `connection_name`.
///
/// # Arguments
/// * `connection_name` — Registered connection to pin for the session.
///
/// # Returns
/// Opaque session UUID string for subsequent operations.
pub fn register_session(connection_name: String) -> String {
    let session_id = uuid::Uuid::new_v4().to_string();
    SESSION_REGISTRY.insert(session_id.clone(), Arc::new(SessionState::new(connection_name)));
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
                let dt_module = py.import("datetime")?;
                let dt_class = dt_module.getattr("datetime")?;
                dt_class.call_method1("fromisoformat", (s.replace('Z', "+00:00"),))
            }
            RustValue::Date(s) => {
                let dt_module = py.import("datetime")?;
                let date_class = dt_module.getattr("date")?;
                date_class.call_method1("fromisoformat", (s,))
            }
            RustValue::Json(v) => {
                let json_str = v.to_string();
                let json_module = py.import("json")?;
                json_module.call_method1("loads", (json_str,))
            }
            RustValue::Blob(b) => {
                let bytes = pyo3::types::PyBytes::new(py, &b);
                Ok(bytes.into_any())
            }
            RustValue::Uuid(s) => {
                let uuid_module = py.import("uuid")?;
                let uuid_class = uuid_module.getattr("UUID")?;
                uuid_class.call1((s,))
            }
            RustValue::Decimal(s) => {
                let decimal_module = py.import("decimal")?;
                let decimal_class = decimal_module.getattr("Decimal")?;
                decimal_class.call1((s,))
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
        let session_id = register_session("default".to_string());

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
