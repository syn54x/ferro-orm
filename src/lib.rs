//! Ferro: High-Performance Rust-Backed Python ORM.
//!
//! This crate provides the core Rust engine for Ferro, handling
//! database connectivity, schema management, and high-speed object
//! hydration using PyO3 and SQLx.

mod connection;
mod operations;
mod query;
mod schema;
mod state;

use crate::state::MODEL_REGISTRY;
use pyo3::prelude::*;

/// Logs a debug message through Python's logging system.
///
/// This function imports the `ferro` logger and calls its `debug()` method.
/// All Ferro engine messages are logged at DEBUG level.
///
/// This can be called from async contexts by acquiring the GIL.
pub fn log_debug(message: String) {
    // Use with_gil to safely access Python from async contexts
    Python::attach(|py| {
        if let Err(_e) = (|| -> PyResult<()> {
            let logging = py.import("logging")?;
            let get_logger = logging.getattr("getLogger")?;
            let logger = get_logger.call1(("ferro",))?;
            let debug_method = logger.getattr("debug")?;
            debug_method.call1((message,))?;
            Ok(())
        })() {
            // If logging fails, silently continue (don't break the application)
            // In production, we might want to log this to stderr, but for now we'll be silent
        }
    });
}

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
    m.add_function(wrap_pyfunction!(operations::fetch_filtered, m)?)?;
    m.add_function(wrap_pyfunction!(operations::count_filtered, m)?)?;
    m.add_function(wrap_pyfunction!(operations::fetch_one, m)?)?;
    m.add_function(wrap_pyfunction!(operations::register_instance, m)?)?;
    m.add_function(wrap_pyfunction!(operations::evict_instance, m)?)?;
    m.add_function(wrap_pyfunction!(operations::save_record, m)?)?;
    m.add_function(wrap_pyfunction!(operations::save_bulk_records, m)?)?;
    m.add_function(wrap_pyfunction!(operations::delete_record, m)?)?;
    m.add_function(wrap_pyfunction!(operations::delete_filtered, m)?)?;
    m.add_function(wrap_pyfunction!(operations::update_filtered, m)?)?;
    m.add_function(wrap_pyfunction!(operations::add_m2m_links, m)?)?;
    m.add_function(wrap_pyfunction!(operations::remove_m2m_links, m)?)?;
    m.add_function(wrap_pyfunction!(operations::clear_m2m_links, m)?)?;
    m.add_function(wrap_pyfunction!(operations::begin_transaction, m)?)?;
    m.add_function(wrap_pyfunction!(operations::commit_transaction, m)?)?;
    m.add_function(wrap_pyfunction!(operations::rollback_transaction, m)?)?;
    m.add_function(wrap_pyfunction!(connection::reset_engine, m)?)?;
    m.add_function(wrap_pyfunction!(clear_registry, m)?)?;
    m.add_function(wrap_pyfunction!(version, m)?)?;
    m.add_function(wrap_pyfunction!(schema::create_tables, m)?)?;

    Ok(())
}
