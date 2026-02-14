//! Connection lifecycle management for the Ferro ORM.
//!
//! This module handles database connections, pool initialization,
//! and engine resets.

use crate::schema::internal_create_tables;
use crate::state::{ENGINE, IDENTITY_MAP};
use pyo3::prelude::*;
use sqlx::any::AnyPoolOptions;
use std::sync::Arc;

/// Initializes the global database connection pool.
///
/// This is an asynchronous function that returns a Python coroutine.
///
/// Args:
///     url (str): The database connection URL (e.g., "sqlite:test.db").
///     auto_migrate (bool): If True, automatically creates tables for all
///         registered models on connection. Defaults to False.
///
/// # Errors
/// Returns a `PyErr` if the connection fails or if auto-migration fails.
#[pyfunction]
#[pyo3(signature = (url, auto_migrate=false))]
pub fn connect(py: Python<'_>, url: String, auto_migrate: bool) -> PyResult<Bound<'_, PyAny>> {
    sqlx::any::install_default_drivers();

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let pool = AnyPoolOptions::new()
            .max_connections(5)
            .connect(&url)
            .await
            .map_err(|e| {
                pyo3::exceptions::PyConnectionError::new_err(format!("DB Connection failed: {}", e))
            })?;

        let arc_pool = Arc::new(pool);

        if auto_migrate {
            internal_create_tables(arc_pool.clone()).await?;
        }

        let mut engine = ENGINE
            .write()
            .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Engine"))?;
        *engine = Some(arc_pool);

        crate::log_debug(format!("⚡️ Ferro Engine: Connected to {}", url));
        Ok(())
    })
}

/// Shuts down the global engine and clears the Identity Map.
///
/// This is useful for testing environments to ensure isolation
/// between test runs.
///
/// # Errors
/// Returns a `PyErr` if the engine lock cannot be acquired.
#[pyfunction]
pub fn reset_engine() -> PyResult<()> {
    let mut engine = ENGINE
        .write()
        .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Engine"))?;
    *engine = None;
    IDENTITY_MAP.clear();
    Ok(())
}
