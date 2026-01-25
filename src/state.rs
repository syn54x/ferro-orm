//! Centralized state management for the Ferro ORM.
//!
//! This module holds the global connection pool, the model registry,
//! and the Identity Map used for object tracking.

use dashmap::DashMap;
use once_cell::sync::Lazy;
use pyo3::IntoPyObjectExt;
use pyo3::prelude::*;
use sqlx::{Any, AnyConnection, Pool};
use std::collections::HashMap;
use std::sync::{Arc, RwLock};
use tokio::sync::Mutex;

/// Global registry mapping model names to their Pydantic-generated JSON schemas.
pub static MODEL_REGISTRY: Lazy<RwLock<HashMap<String, serde_json::Value>>> =
    Lazy::new(|| RwLock::new(HashMap::new()));

/// The global SQLx connection pool, initialized via `connect()`.
pub static ENGINE: Lazy<RwLock<Option<Arc<Pool<Any>>>>> = Lazy::new(|| RwLock::new(None));

/// Global registry for active transactions.
/// Maps Transaction ID -> Mutex-protected Connection.
pub static TRANSACTION_REGISTRY: Lazy<DashMap<String, Arc<Mutex<AnyConnection>>>> =
    Lazy::new(DashMap::new);

/// Identity Map used for object tracking and deduplication.
///
/// Maps `(ModelName, PrimaryKeyValue)` to a live Python object.
pub static IDENTITY_MAP: Lazy<DashMap<(String, String), Py<PyAny>>> = Lazy::new(DashMap::new);

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
