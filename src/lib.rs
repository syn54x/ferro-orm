//! Ferro: High-Performance Rust-Backed Python ORM.
//!
//! This crate provides the core Rust engine for Ferro, handling
//! database connectivity, schema management, and high-speed object
//! hydration using PyO3 and SQLx.

mod connection;
mod operations;
mod schema;
mod state;

use crate::state::MODEL_REGISTRY;
use pyo3::prelude::*;

/// Returns the current version of the Ferro core.
#[pyfunction]
fn version() -> String {
    env!("CARGO_PKG_VERSION").to_string()
}

/// Clears the global model registry.
///
/// Primarily used for cleaning up state between tests.
///
/// # Errors
/// Returns a `PyErr` if the registry lock cannot be acquired.
#[pyfunction]
fn clear_registry() -> PyResult<()> {
    let mut registry = MODEL_REGISTRY
        .write()
        .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Model Registry"))?;
    registry.clear();
    Ok(())
}

/// The main Python module bridge for Ferro.
///
/// This module exposes the Rust-backed core functions to Python.
#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(schema::register_model_schema, m)?)?;
    m.add_function(wrap_pyfunction!(connection::connect, m)?)?;
    m.add_function(wrap_pyfunction!(operations::fetch_all, m)?)?;
    m.add_function(wrap_pyfunction!(operations::fetch_one, m)?)?;
    m.add_function(wrap_pyfunction!(operations::register_instance, m)?)?;
    m.add_function(wrap_pyfunction!(operations::save_record, m)?)?;
    m.add_function(wrap_pyfunction!(connection::reset_engine, m)?)?;
    m.add_function(wrap_pyfunction!(clear_registry, m)?)?;
    m.add_function(wrap_pyfunction!(version, m)?)?;
    m.add_function(wrap_pyfunction!(schema::create_tables, m)?)?;

    Ok(())
}
