//! Connection lifecycle management for the Ferro ORM.
//!
//! This module handles database connections, pool initialization,
//! and engine resets.

use crate::backend::{BackendKind, EngineHandle};
use crate::schema::internal_create_tables;
use crate::state::{CONNECTION_REGISTRY, DEFAULT_CONNECTION_NAME, ENGINE, IDENTITY_MAP};
use pyo3::prelude::*;
use sqlx::postgres::PgPoolOptions;
use sqlx::sqlite::SqlitePoolOptions;
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

fn is_secret_query_key(key: &str) -> bool {
    let key = key.to_ascii_lowercase();
    key.contains("password")
        || key.contains("passwd")
        || key.contains("pwd")
        || key.contains("secret")
        || key.contains("token")
        || key.contains("apikey")
        || key.contains("api_key")
        || key.contains("service_role")
}

fn redact_connection_url(url: &str) -> String {
    let (without_fragment, fragment) = match url.split_once('#') {
        Some((base, fragment)) => (base, Some(fragment)),
        None => (url, None),
    };
    let (base, query) = match without_fragment.split_once('?') {
        Some((base, query)) => (base, Some(query)),
        None => (without_fragment, None),
    };

    let redacted_base = match base.find("://") {
        Some(scheme_idx) => {
            let authority_start = scheme_idx + 3;
            let after_scheme = &base[authority_start..];
            if let Some(at_idx) = after_scheme.find('@') {
                let userinfo = &after_scheme[..at_idx];
                let authority_rest = &after_scheme[at_idx..];
                if let Some((user, _password)) = userinfo.rsplit_once(':') {
                    format!(
                        "{}{}:<redacted>{}",
                        &base[..authority_start],
                        user,
                        authority_rest
                    )
                } else {
                    base.to_string()
                }
            } else {
                base.to_string()
            }
        }
        None => base.to_string(),
    };

    let redacted_query = query.map(|query| {
        query
            .split('&')
            .map(|pair| {
                let (key, value) = pair.split_once('=').unwrap_or((pair, ""));
                if is_secret_query_key(key) {
                    format!("{key}=<redacted>")
                } else if value.is_empty() {
                    key.to_string()
                } else {
                    format!("{key}={value}")
                }
            })
            .collect::<Vec<_>>()
            .join("&")
    });

    let mut redacted = match redacted_query {
        Some(query) => format!("{redacted_base}?{query}"),
        None => redacted_base,
    };
    if let Some(fragment) = fragment {
        redacted.push('#');
        redacted.push_str(fragment);
    }
    redacted
}

fn normalized_connection_name(name: Option<String>) -> PyResult<(String, bool)> {
    match name {
        Some(name) => {
            if name.is_empty()
                || !name
                    .chars()
                    .all(|ch| ch.is_ascii_alphanumeric() || ch == '_')
            {
                return Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "Invalid connection name '{}'",
                    name
                )));
            }
            Ok((name, false))
        }
        None => Ok(("default".to_string(), true)),
    }
}

async fn connect_engine_handle(
    connection_url: &str,
    backend: BackendKind,
    search_path: Option<String>,
    max_connections: u32,
    min_connections: u32,
) -> Result<EngineHandle, sqlx::Error> {
    match backend {
        BackendKind::Sqlite => {
            let pool = SqlitePoolOptions::new()
                .max_connections(max_connections)
                .min_connections(min_connections)
                .connect(connection_url)
                .await?;
            Ok(EngineHandle::new_sqlite(pool))
        }
        BackendKind::Postgres => {
            let mut pool_options = PgPoolOptions::new()
                .max_connections(max_connections)
                .min_connections(min_connections);
            if let Some(search_path) = search_path {
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

            let pool = pool_options.connect(connection_url).await?;
            Ok(EngineHandle::new_postgres(pool))
        }
    }
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
#[pyo3(signature = (url, auto_migrate=false, name=None, default=false, max_connections=5, min_connections=0))]
pub fn connect(
    py: Python<'_>,
    url: String,
    auto_migrate: bool,
    name: Option<String>,
    default: bool,
    max_connections: u32,
    min_connections: u32,
) -> PyResult<Bound<'_, PyAny>> {
    let (connection_url, search_path) = split_search_path(&url);
    let redacted_url = redact_connection_url(&connection_url);
    let backend = BackendKind::from_url(&connection_url).map_err(|e| {
        pyo3::exceptions::PyConnectionError::new_err(format!(
            "DB Connection failed for {}: {}",
            redacted_url, e
        ))
    })?;
    let (connection_name, is_implicit_default) = normalized_connection_name(name)?;
    let should_select_default = default || is_implicit_default;

    sqlx::any::install_default_drivers();

    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        if CONNECTION_REGISTRY
            .read()
            .map_err(|_| {
                pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Connection Registry")
            })?
            .contains_key(&connection_name)
            && !is_implicit_default
        {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "Connection '{}' is already registered",
                connection_name
            )));
        }

        if let Some(ref search_path) = search_path
            && !is_safe_search_path(search_path)
        {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "Invalid ferro_search_path '{}'",
                search_path
            )));
        }

        let engine_handle = connect_engine_handle(
            &connection_url,
            backend,
            search_path.clone(),
            max_connections,
            min_connections,
        )
        .await
        .map_err(|e| {
            pyo3::exceptions::PyConnectionError::new_err(format!(
                "DB Connection failed for {}: {}",
                redacted_url, e
            ))
        })?;

        let engine_handle = Arc::new(engine_handle);

        if auto_migrate {
            internal_create_tables(engine_handle.clone()).await?;
        }

        let mut registry = CONNECTION_REGISTRY.write().map_err(|_| {
            pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Connection Registry")
        })?;
        if registry.contains_key(&connection_name) && !is_implicit_default {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "Connection '{}' is already registered",
                connection_name
            )));
        }
        registry.insert(connection_name.clone(), engine_handle.clone());
        drop(registry);

        if should_select_default {
            let mut default_name = DEFAULT_CONNECTION_NAME.write().map_err(|_| {
                pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Default Connection")
            })?;
            *default_name = Some(connection_name.clone());
        }

        let mut engine = ENGINE
            .write()
            .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Engine"))?;
        if should_select_default {
            *engine = Some(engine_handle);
        }

        crate::log_debug(format!(
            "⚡️ Ferro Engine: Connected '{}' to {}",
            connection_name, redacted_url
        ));
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
    CONNECTION_REGISTRY
        .write()
        .map_err(|_| {
            pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Connection Registry")
        })?
        .clear();
    *DEFAULT_CONNECTION_NAME.write().map_err(|_| {
        pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Default Connection")
    })? = None;
    IDENTITY_MAP.clear();
    Ok(())
}

/// Selects the default connection used by legacy unqualified operations.
///
/// # Errors
/// Returns a `PyErr` if the name is invalid, unknown, or state locks fail.
#[pyfunction]
pub fn set_default_connection(name: String) -> PyResult<()> {
    let (connection_name, _) = normalized_connection_name(Some(name))?;
    let engine_handle = CONNECTION_REGISTRY
        .read()
        .map_err(|_| {
            pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Connection Registry")
        })?
        .get(&connection_name)
        .cloned()
        .ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err(format!(
                "Connection '{}' is not registered",
                connection_name
            ))
        })?;

    *DEFAULT_CONNECTION_NAME.write().map_err(|_| {
        pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Default Connection")
    })? = Some(connection_name);
    *ENGINE
        .write()
        .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Engine"))? =
        Some(engine_handle);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::connect_engine_handle;
    use crate::backend::BackendKind;

    #[tokio::test]
    async fn connect_engine_handle_uses_typed_sqlite_backend() {
        let engine = connect_engine_handle("sqlite::memory:", BackendKind::Sqlite, None, 5, 0)
            .await
            .unwrap();

        assert_eq!(engine.backend(), BackendKind::Sqlite);
        assert!(engine.sqlite_pool().is_some());
        assert!(engine.postgres_pool().is_none());
    }

    #[tokio::test]
    async fn connect_engine_handle_supports_sqlite_runtime_execution() {
        let engine = connect_engine_handle("sqlite::memory:", BackendKind::Sqlite, None, 5, 0)
            .await
            .unwrap();

        assert_eq!(engine.backend(), BackendKind::Sqlite);
        assert!(engine.sqlite_pool().is_some());
        assert_eq!(engine.execute_sql("SELECT 1").await.unwrap(), 0);
    }
}
