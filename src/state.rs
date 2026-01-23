//! Centralized state management for the Ferro ORM.
//!
//! This module holds the global connection pool, the model registry,
//! and the Identity Map used for object tracking.

use dashmap::DashMap;
use once_cell::sync::Lazy;
use pyo3::IntoPyObjectExt;
use pyo3::prelude::*;
use sqlx::{Any, Pool};
use std::collections::HashMap;
use std::sync::{Arc, RwLock};

/// Global registry mapping model names to their Pydantic-generated JSON schemas.
pub static MODEL_REGISTRY: Lazy<RwLock<HashMap<String, serde_json::Value>>> =
    Lazy::new(|| RwLock::new(HashMap::new()));

/// The global SQLx connection pool, initialized via `connect()`.
pub static ENGINE: Lazy<RwLock<Option<Arc<Pool<Any>>>>> = Lazy::new(|| RwLock::new(None));

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
            RustValue::None => Ok(py.None().into_bound(py)),
        }
    }
}
