//! Connection lifecycle management for the Ferro ORM.
//!
//! This module handles database connections, pool initialization,
//! and engine resets.

use crate::schema::internal_create_tables;
use crate::state::{set_sql_dialect, ENGINE, IDENTITY_MAP, SqlDialect};
use pyo3::prelude::*;
use sqlx::any::AnyPoolOptions;
use std::sync::Arc;

fn split_search_path(url: &str) -> (String, Option<String>) {
    let Some((base, query)) = url.split_once('?') else {
        return (url.to_string(), None);
    };

    let mut retained = Vec::new();
    let mut search_path = None;

    for pair in query.split('&') {
        if let Some(value) = pair.strip_prefix("ferro_search_path=") {
            search_path = Some(value.to_string());
        } else if !pair.is_empty() {
            retained.push(pair);
        }
    }

    let clean_url = if retained.is_empty() {
        base.to_string()
    } else {
        format!("{}?{}", base, retained.join("&"))
    };

    (clean_url, search_path)
}

fn is_safe_search_path(search_path: &str) -> bool {
    !search_path.is_empty()
        && search_path
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || ch == '_')
}

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
    let (connection_url, search_path) = split_search_path(&url);

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let dialect = if connection_url.starts_with("postgres://")
            || connection_url.starts_with("postgresql://")
        {
            SqlDialect::Postgres
        } else {
            SqlDialect::Sqlite
        };

        if let Some(ref search_path) = search_path
            && !is_safe_search_path(search_path)
        {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "Invalid ferro_search_path '{}'",
                search_path
            )));
        }

        let mut pool_options = AnyPoolOptions::new().max_connections(5);
        if dialect == SqlDialect::Postgres
            && let Some(search_path) = search_path
        {
            let set_search_path_sql = Arc::new(format!("SET search_path TO {}", search_path));
            pool_options = pool_options.after_connect(move |conn, _meta| {
                let set_search_path_sql = set_search_path_sql.clone();
                Box::pin(async move {
                    sqlx::query(set_search_path_sql.as_str())
                        .execute(conn)
                        .await?;
                    Ok(())
                })
            });
        }

        let pool = pool_options
            .connect(&connection_url)
            .await
            .map_err(|e| {
                pyo3::exceptions::PyConnectionError::new_err(format!("DB Connection failed: {}", e))
            })?;

        set_sql_dialect(dialect);

        let arc_pool = Arc::new(pool);

        if auto_migrate {
            internal_create_tables(arc_pool.clone()).await?;
        }

        let mut engine = ENGINE
            .write()
            .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Engine"))?;
        *engine = Some(arc_pool);

        crate::log_debug(format!("⚡️ Ferro Engine: Connected to {}", connection_url));
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
    set_sql_dialect(SqlDialect::Sqlite);
    IDENTITY_MAP.clear();
    Ok(())
}
