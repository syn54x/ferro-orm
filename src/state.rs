//! Centralized state management for the Ferro ORM.
//!
//! This module holds the global connection pool, the model registry,
//! and the Identity Map used for object tracking.

use crate::backend::{BackendKind, EngineConnection, EngineHandle};
use dashmap::DashMap;
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

/// The global runtime engine, initialized via `connect()`.
pub static ENGINE: Lazy<RwLock<Option<Arc<EngineHandle>>>> = Lazy::new(|| RwLock::new(None));

/// Registered runtime engines keyed by user-facing connection name.
pub static CONNECTION_REGISTRY: Lazy<RwLock<HashMap<String, Arc<EngineHandle>>>> =
    Lazy::new(|| RwLock::new(HashMap::new()));

/// Name of the default connection used by legacy unqualified operations.
pub static DEFAULT_CONNECTION_NAME: Lazy<RwLock<Option<String>>> = Lazy::new(|| RwLock::new(None));

/// Resolve a connection name and engine by explicit connection name, or by the selected default.
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
pub fn engine_for_connection(using: Option<String>) -> PyResult<Arc<EngineHandle>> {
    connection_for_route(using).map(|(_, engine)| engine)
}

/// Active transaction handle.
#[derive(Clone)]
pub struct TransactionHandle {
    pub conn: TransactionConnection,
    pub savepoint_name: Option<String>,
    pub connection_name: String,
}

pub type TransactionConnection = Arc<Mutex<EngineConnection>>;

impl TransactionHandle {
    pub fn root(conn: EngineConnection, connection_name: String) -> Self {
        Self {
            conn: Arc::new(Mutex::new(conn)),
            savepoint_name: None,
            connection_name,
        }
    }

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

/// A Rust-native representation of database values.
///
/// Used during the GIL-free parsing phase to move data from the database
/// into Rust memory before acquiring the Python GIL for object injection.
#[derive(Clone, Debug)]
pub enum RustValue {
    BigInt(i64),
    Double(f64),
    String(String),
    Bool(bool),
    DateTime(String),
    Date(String),
    Json(serde_json::Value),
    Blob(Vec<u8>),
    Uuid(String),
    Decimal(String),
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
