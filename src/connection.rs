//! Connection lifecycle management for the Ferro ORM.
//!
//! This module handles database connections, pool initialization,
//! and engine resets.

use crate::backend::{EngineHandle, PoolSpec, dialect_from_url};
use crate::migrate::{
    AutoMigrateDoor, MigrateOptions, PassReport, internal_migrate, with_pass_report,
};
use crate::session_settings::SettingsDelivery;
use crate::state::{
    CONNECTION_REGISTRY, DEFAULT_CONNECTION_NAME, Dialect, ENGINE, SESSION_REGISTRY,
    TRANSACTION_REGISTRY, connection_for_route,
};
use pyo3::prelude::*;
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

/// What a connection URL's secrets are masked with.
const MASK: &str = "***";

/// `url` with its secrets masked by `***`: the userinfo password, and the
/// value of every query key [`is_secret_query_key`] names. The scheme, user,
/// host, port, path, every other query pair and the fragment are kept, and a
/// URL with nothing to mask (a SQLite URL) comes back as given, byte for
/// byte; only a URL with a secret is rebuilt from what the parser read.
///
/// The URL is read with `url::Url::parse`, the parser sqlx reads connection
/// URLs with, so the parts masked are the parts the driver connects with.
/// What it cannot read is masked whole (`<scheme>://***`, or `***` when no
/// scheme is readable): the driver refuses it too. So is a URL with an `@`
/// past a non-empty authority (in its path, query or fragment): that is
/// where the delimiter of a password holding a raw `/`, `?` or `#` lands
/// (`u:9/s3cret@h` reads as host `u`, port `9`), and the string cannot tell
/// the two apart; written as `%40`, the `@` shows. A Postgres URL with no
/// host and an `@` anywhere is masked whole for the same reason, and so is
/// every URL whose scheme Ferro does not connect to
/// (`postgresql:/u:s3cret@h/db`, `mysql://…`): the error refusing it quotes
/// it, and nothing about its parse says where a secret sits.
pub(crate) fn redact_connection_url(url: &str) -> String {
    // A scheme Ferro does not connect to is masked whole before anything
    // else reads it: its parse says nothing about where a secret sits
    // (`postgresql:/u:s3cret@h/db` parses with no host and no password).
    let Ok(dialect) = dialect_from_url(url) else {
        return masked_whole(readable_scheme(url));
    };
    let Ok(mut parsed) = url::Url::parse(url) else {
        return masked_whole(readable_scheme(url));
    };
    let has_authority = parsed.host_str().is_some_and(|host| !host.is_empty());
    let at_past_authority = [Some(parsed.path()), parsed.query(), parsed.fragment()]
        .into_iter()
        .flatten()
        .any(|part| part.contains('@'));
    // Past a non-empty authority, an `@` may be a password's delimiter; with
    // no authority at all, a Postgres URL's `@` anywhere may be one too (a
    // SQLite URL has no userinfo: its `@` is a path character).
    let hostless_at = !has_authority && dialect == Dialect::Postgres && url.contains('@');
    if (has_authority && at_past_authority) || hostless_at {
        return masked_whole(Some(parsed.scheme()));
    }
    let is_secret_pair = |pair: &str| {
        url::form_urlencoded::parse(pair.as_bytes())
            .next()
            .is_some_and(|(key, _)| is_secret_query_key(&key))
    };
    let has_secret_pair = parsed
        .query()
        .is_some_and(|query| query.split('&').any(is_secret_pair));
    if parsed.password().is_none() && !has_secret_pair {
        // Nothing to mask: shown as given, never re-escaped by the parser.
        return url.to_string();
    }
    if parsed.password().is_some() && parsed.set_password(Some(MASK)).is_err() {
        return masked_whole(Some(parsed.scheme()));
    }
    if let Some(query) = parsed.query() {
        let query = query
            .split('&')
            .map(|pair| match pair.split_once('=') {
                _ if !is_secret_pair(pair) => pair.to_string(),
                Some((key, _)) => format!("{key}={MASK}"),
                None => format!("{pair}={MASK}"),
            })
            .collect::<Vec<_>>()
            .join("&");
        parsed.set_query(Some(&query));
    }
    parsed.into()
}

/// A URL masked whole: `<scheme>://***`, or `***` without a scheme.
fn masked_whole(scheme: Option<&str>) -> String {
    match scheme {
        Some(scheme) => format!("{scheme}://{MASK}"),
        None => MASK.to_string(),
    }
}

/// The scheme `url` starts with, when it has a well-formed one (a letter,
/// then letters, digits, `+`, `-` or `.`, then `:`).
fn readable_scheme(url: &str) -> Option<&str> {
    let (scheme, _) = url.split_once(':')?;
    let mut chars = scheme.chars();
    let first = chars.next()?;
    (first.is_ascii_alphabetic()
        && chars.all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '+' | '-' | '.')))
    .then_some(scheme)
}

/// `DB Connection failed for <url>`, the URL's secrets masked: the context
/// every connection error carries.
fn connection_failed_context(url: &str) -> String {
    format!("DB Connection failed for {}", redact_connection_url(url))
}

/// The dialect `connection_url` reaches, or the `InterfaceError` every
/// connection to an unsupported scheme fails with.
fn accepted_dialect(connection_url: &str) -> PyResult<Dialect> {
    dialect_from_url(connection_url).map_err(|e| {
        crate::errors::interface_error(format!(
            "{}: {e}",
            connection_failed_context(connection_url)
        ))
    })
}

/// `url` as Ferro shows it, its secrets masked by `***` (the userinfo
/// password and every secret query value): what `ferro migrate` prints on
/// its database line and `status --json` carries as `url`. A URL with no
/// secret (a SQLite URL) comes back as given.
///
/// # Errors
/// `InterfaceError` when `url`'s scheme is not one Ferro connects to, the
/// same error `connect(url)` raises, so a URL is shown only once accepted.
#[pyfunction]
pub fn _redact_url(url: String) -> PyResult<String> {
    let (connection_url, _) = split_search_path(&url);
    accepted_dialect(&connection_url)?;
    Ok(redact_connection_url(&url))
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

/// Builds the duplicate-connection error shared by the early fail-fast check
/// and the authoritative insert-time check (FF-G G4b). Both sites must raise
/// the same tailored message for a given `(connection_name, is_implicit_default)`
/// pair so a race between the two checks can't surface inconsistent errors.
fn duplicate_connection_error(connection_name: &str, is_implicit_default: bool) -> PyErr {
    pyo3::exceptions::PyValueError::new_err(if is_implicit_default {
        "A default connection is already registered. Pass name=\"...\" to \
         register an additional named connection, or call reset_engine() \
         first to tear down existing connections."
            .to_string()
    } else {
        format!("Connection '{}' is already registered", connection_name)
    })
}

async fn connect_engine_handle(
    connection_url: &str,
    backend: crate::state::Dialect,
    search_path: Option<String>,
    max_connections: u32,
    min_connections: u32,
    settings_delivery: SettingsDelivery,
) -> Result<EngineHandle, sqlx::Error> {
    EngineHandle::connect(PoolSpec {
        backend,
        url: connection_url.to_string(),
        search_path,
        max_connections,
        min_connections,
        settings_delivery,
        pins: std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0)),
    })
    .await
}

/// Initializes the global database connection pool.
///
/// This is an asynchronous function that returns a Python coroutine.
///
/// Args:
///     url (str): The database connection URL (e.g., "sqlite:test.db").
///     auto_migrate (bool): If True, automatically creates tables for all
///         registered models on connection. Defaults to False.
///     migrate_updates (bool): If True, also adds missing model columns to
///         existing tables (and reconciles type/nullability drift on
///         Postgres). Implies `auto_migrate`. On Postgres each table's plan
///         runs in one transaction (a mid-plan failure rolls that table
///         back); SQLite applies statements one at a time.
///     migrate_destructive (bool): If True, also drops live columns that no
///         longer exist on the model. Implies `migrate_updates`.
///     settings_delivery (str): How sessions on this connection deliver their
///         session settings — `"transaction"` (the default) or `"connection"`.
///         See `ferro.PoolConfig`; the mode is never inferred from the URL.
///     tracking_schemas (list[str]): The project's configured migration
///         `tracking_schema`s, read by `ferro.connect` from `FerroSettings`
///         only when an auto-migrate flag is set. The auto-migrate guard
///         (ADR-0038) looks for tracking tables there as well as in the
///         current schema and the catalog's format tables.
///     ddl_lock_timeout_s (float): The project's `ddl_lock_timeout` in
///         seconds, read by `ferro.connect` from `FerroSettings` with the
///         tracking schemas: how long each reconciliation statement waits for
///         a table lock on Postgres before its table's plan is retried
///         (ADR-0044). `0` disables; the default is the setting's, 5 seconds.
///     lock_timeout_s (float): The project's `lock_timeout` in seconds, read
///         with them: how long the auto-migrate pass waits for the run lock
///         another run or pass holds before it refuses (ADR-0038). The
///         default is the setting's, 30 seconds.
///
/// # Errors
/// Returns a `PyErr` if the connection fails or if auto-migration fails.
/// Also returns a `PyErr` (`ValueError`) if a connection with this name (or a
/// default connection, when `name` is omitted) is already registered — call
/// `reset_engine()` first or pass a distinct `name` to register an additional
/// connection.
#[pyfunction]
#[pyo3(signature = (url, auto_migrate=false, name=None, default=false, max_connections=5, min_connections=0, identity_map=true, migrate_updates=false, migrate_destructive=false, settings_delivery="transaction".to_string(), tracking_schemas=Vec::new(), ddl_lock_timeout_s=5.0, lock_timeout_s=30.0))]
#[allow(clippy::too_many_arguments)]
pub fn connect(
    py: Python<'_>,
    url: String,
    auto_migrate: bool,
    name: Option<String>,
    default: bool,
    max_connections: u32,
    min_connections: u32,
    identity_map: bool,
    migrate_updates: bool,
    migrate_destructive: bool,
    settings_delivery: String,
    tracking_schemas: Vec<String>,
    ddl_lock_timeout_s: f64,
    lock_timeout_s: f64,
) -> PyResult<Bound<'_, PyAny>> {
    let settings_delivery = SettingsDelivery::parse(&settings_delivery)?;
    let opts = MigrateOptions::laddered(migrate_updates, migrate_destructive)
        .with_ddl_lock_timeout_seconds(ddl_lock_timeout_s)?
        .with_lock_timeout_seconds(lock_timeout_s)?;
    let (connection_url, search_path) = split_search_path(&url);
    let failed_context = connection_failed_context(&connection_url);
    let redacted_url = redact_connection_url(&connection_url);
    let backend = accepted_dialect(&connection_url)?;
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
        {
            // FF-G G4b: a second unnamed connect() used to silently replace
            // the default engine — a loud error, not a swallow (I-6). This is
            // only a fail-fast shortcut to avoid wasted pool creation; the
            // insert-time check below (under the write lock) is authoritative.
            return Err(duplicate_connection_error(
                &connection_name,
                is_implicit_default,
            ));
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
            settings_delivery,
        )
        .await
        .map_err(|e| crate::errors::map_db_error(&failed_context, e))?
        .with_identity_map_enabled(identity_map)
        .with_shown_url(&url);

        let engine_handle = Arc::new(engine_handle);

        // Flag ladder: migrate_destructive ⇒ migrate_updates ⇒ auto_migrate.
        // There is no coherent "alter existing tables but don't create missing
        // ones" mode — the diff baseline relies on CREATE TABLE IF NOT EXISTS
        // having run first. The passes run under the run lock, behind the
        // guard that refuses a database governed by ferro migrations
        // (ADR-0038), before the connection is registered.
        // `connect()` returns nothing: it builds the same report
        // `ferro.migrate()` returns and logs from it (ADR-0049), and a failed
        // pass's error carries it.
        if auto_migrate || opts.updates {
            let mut report = PassReport::default();
            internal_migrate(
                engine_handle.clone(),
                opts,
                &tracking_schemas,
                AutoMigrateDoor::Connect,
                &mut report,
            )
            .await
            .map_err(|err| with_pass_report(err, &report))?;
            report.log_summary();
        }

        let mut registry = CONNECTION_REGISTRY.write().map_err(|_| {
            pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Connection Registry")
        })?;
        // Authoritative check: registration happens after pool creation (and
        // optional auto-migrate), so two concurrent connect() calls for the
        // same name can both pass the early fail-fast check above before
        // either registers here. This check runs under the write lock and
        // must not carry the implicit-default exemption, or the second
        // caller would silently replace the default engine (FF-G G4b).
        if registry.contains_key(&connection_name) {
            return Err(duplicate_connection_error(
                &connection_name,
                is_implicit_default,
            ));
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

/// Shuts down the global engine and tears down all sessions (and their
/// session-scoped identity maps).
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
    TRANSACTION_REGISTRY.clear();
    // Tears down every session's identity map with it (FF-D D2: identity
    // maps are session-scoped only).
    SESSION_REGISTRY.clear();
    Ok(())
}

/// Close and forget one named, non-default connection — the counterpart of
/// `connect(url, name=...)` for a caller that opened a connection for one
/// job (the migration runner connecting to `--url`). The default connection
/// is refused: tearing it down is `reset_engine()`'s job.
///
/// # Errors
/// `ValueError` when the name is not registered or is the default
/// connection; `RuntimeError` when a state lock is poisoned.
#[pyfunction]
#[pyo3(name = "_disconnect")]
pub fn disconnect(py: Python<'_>, name: String) -> PyResult<Bound<'_, PyAny>> {
    let default_name = DEFAULT_CONNECTION_NAME
        .read()
        .map_err(|_| {
            pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Default Connection")
        })?
        .clone();
    if default_name.as_deref() == Some(name.as_str()) {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "Connection '{name}' is the default connection; reset_engine() tears it down"
        )));
    }
    let engine = CONNECTION_REGISTRY
        .write()
        .map_err(|_| {
            pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Connection Registry")
        })?
        .remove(&name)
        .ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err(format!(
                "Connection '{name}' is not registered"
            ))
        })?;
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        if let Some(pool) = engine.sqlite_pool() {
            pool.close().await;
        }
        if let Some(pool) = engine.postgres_pool() {
            pool.close().await;
        }
        Ok(())
    })
}

/// The connected dialect for ``using``, or the default connection.
///
/// ``None`` when no engine is initialized — recipe compile stays
/// dialect-agnostic for goldens. SQLite vs Postgres is available after
/// ``connect()`` so Postgres-only expressions can fail at build time.
#[pyfunction]
#[pyo3(signature = (using=None))]
pub fn connection_backend(using: Option<String>) -> Option<String> {
    connection_for_route(using)
        .ok()
        .map(|(_, engine)| match engine.backend() {
            Dialect::Sqlite => "sqlite".to_string(),
            Dialect::Postgres => "postgres".to_string(),
        })
}

/// The name of the default connection, or `None` when there is none: what
/// `ferro.migrations` runs on when no `using=` is given.
///
/// # Errors
/// `RuntimeError` when the state lock is poisoned.
#[pyfunction]
#[pyo3(name = "_default_connection_name")]
pub fn default_connection_name() -> PyResult<Option<String>> {
    Ok(DEFAULT_CONNECTION_NAME
        .read()
        .map_err(|_| {
            pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Default Connection")
        })?
        .clone())
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
    use super::{connect_engine_handle, connection_failed_context, redact_connection_url};
    use ferro_ddl_lowering::Dialect;

    #[test]
    fn redact_masks_a_password_containing_an_at_sign() {
        assert_eq!(
            redact_connection_url("postgresql://u:p@ss@h:5432/db"),
            "postgresql://u:***@h:5432/db"
        );
    }

    #[test]
    fn redact_masks_a_password_containing_a_colon() {
        assert_eq!(
            redact_connection_url("postgresql://u:p:ss@h/db"),
            "postgresql://u:***@h/db"
        );
    }

    /// `redact_connection_url(url)`, asserting that no piece of `secret`
    /// (split on the characters a password may hold raw) appears in it.
    fn redacted_without(url: &str, secret: &str) -> String {
        let shown = redact_connection_url(url);
        for piece in secret
            .split(['@', ':', '/', '?', '#'])
            .filter(|piece| !piece.is_empty())
        {
            assert!(!shown.contains(piece), "{piece:?} of {secret:?} in {shown}");
        }
        shown
    }

    #[test]
    fn redact_masks_a_password_holding_any_raw_delimiter() {
        // The parser reads a raw `@` or `:` inside the userinfo. A raw `/`,
        // `?` or `#` ends the authority early, so `u:s3` is read as a host
        // with an invalid port, the string does not parse, and it is masked
        // whole.
        for (raw, shown) in [
            ("@", "postgresql://u:***@h:5432/db?sslmode=require"),
            (":", "postgresql://u:***@h:5432/db?sslmode=require"),
            ("/", "postgresql://***"),
            ("?", "postgresql://***"),
            ("#", "postgresql://***"),
        ] {
            let secret = format!("s3{raw}cret");
            assert_eq!(
                redacted_without(
                    &format!("postgresql://u:{secret}@h:5432/db?sslmode=require"),
                    &secret
                ),
                shown,
                "{raw}"
            );
        }
    }

    #[test]
    fn redact_masks_a_password_whose_raw_delimiter_leaves_a_valid_port() {
        // `u:9/s3cret@h` parses as host `u`, port `9`, path `/s3cret@h/db`.
        // The parser sees no password, but an `@` past a non-empty authority
        // is where an unencoded password's delimiter lands: masked whole.
        for (url, secret) in [
            ("postgresql://u:9/s3cret@h/db", "9/s3cret"),
            ("postgresql://u:9?s3cret@h/db", "9?s3cret"),
            ("postgresql://u:9#s3cret@h/db", "9#s3cret"),
            ("postgresql://u:pw@sx/s3cret@h/db", "pw@sx/s3cret"),
            ("postgresql://h:5432/db@x", "5432/db"),
        ] {
            assert_eq!(redacted_without(url, secret), "postgresql://***", "{url}");
        }
    }

    #[test]
    fn redact_masks_an_at_sign_in_a_query_value_whole() {
        // The second review's case: an `@` in a query value must never print
        // the `password=` value after it. The string cannot be told from a
        // password holding a raw `?`, so it is masked whole.
        assert_eq!(
            redacted_without(
                "postgresql://u:pw@h/db?application_name=a@b&password=s3cret",
                "s3cret"
            ),
            "postgresql://***"
        );
        assert_eq!(
            redacted_without("postgresql://h/db?app=me@x&password=s3cret", "s3cret"),
            "postgresql://***"
        );
    }

    #[test]
    fn redact_masks_a_scheme_ferro_does_not_connect_to_whole() {
        // `postgresql:/u:...` and `postgresql:u:...` parse with no host, so
        // no password is read; their scheme is not one Ferro accepts, and
        // the error text quoting them must never carry the secret.
        for (url, shown) in [
            ("postgresql:/u:s3cret@h/db", "postgresql://***"),
            ("postgresql:u:s3cret@h/db", "postgresql://***"),
            ("mysql://u:s3cret@h/db", "mysql://***"),
            ("mysql://h/db", "mysql://***"),
        ] {
            assert_eq!(redacted_without(url, "s3cret"), shown, "{url}");
            assert!(
                !connection_failed_context(url).contains("s3cret"),
                "{url}"
            );
        }
    }

    #[test]
    fn redact_masks_a_hostless_postgres_url_holding_an_at_sign_whole() {
        for (url, shown) in [
            ("postgresql:///u:s3cret@h/db", "postgresql://***"),
            ("postgres:///db?app=x@s3cret", "postgres://***"),
        ] {
            assert_eq!(redacted_without(url, "s3cret"), shown, "{url}");
        }
        // A host-less postgres URL with no `@` (a Unix socket) is shown as given.
        assert_eq!(
            redact_connection_url("postgresql:///db?host=/var/run/postgresql"),
            "postgresql:///db?host=/var/run/postgresql"
        );
    }

    #[test]
    fn redact_shows_an_at_sign_written_as_percent_40() {
        for url in [
            "postgresql://h:5432/db%40x",
            "postgresql://h/db?application_name=a%40b",
        ] {
            assert_eq!(redact_connection_url(url), url);
        }
        assert_eq!(
            redact_connection_url("postgresql://u:pw@h/db?application_name=a%40b&password=s"),
            "postgresql://u:***@h/db?application_name=a%40b&password=***"
        );
    }

    #[test]
    fn redact_returns_a_url_with_nothing_to_mask_byte_for_byte() {
        // The parser would escape these (`Jane%20Doe`, `%C3%A9`); a URL with
        // nothing to mask is shown as the user configured it.
        for url in [
            "sqlite:/Users/Jane Doe/app.db",
            "sqlite:///Users/Jane Doe/app.db?mode=rwc",
            "sqlite:/données/café.db",
            "sqlite::memory:",
            "postgresql://pinch@db.internal:5432/pinch prod?application_name=Jane Doe",
        ] {
            assert_eq!(redact_connection_url(url), url);
        }
    }

    #[test]
    fn redact_masks_a_string_the_parser_rejects() {
        assert_eq!(
            redact_connection_url("postgresql://u:pw@h:99999/db"),
            "postgresql://***"
        );
        assert_eq!(redact_connection_url("not a url: pw"), "***");
        assert_eq!(redact_connection_url(""), "***");
    }

    #[test]
    fn redact_masks_a_percent_escaped_password() {
        assert_eq!(
            redacted_without("postgresql://u:p%40ss%3A@h/db", "p%40ss%3A"),
            "postgresql://u:***@h/db"
        );
    }

    #[test]
    fn redact_keeps_a_url_without_a_password() {
        for url in [
            "postgresql://pinch@db.internal:5432/pinch_prod?sslmode=require",
            "postgresql://db.internal/pinch_prod",
        ] {
            assert_eq!(redact_connection_url(url), url);
        }
    }

    #[test]
    fn redact_keeps_a_sqlite_url() {
        for url in [
            "sqlite:///var/data/app.db?mode=rwc",
            "sqlite:app.db",
            "sqlite::memory:",
            "sqlite:///tmp/odd@name:1.db",
            "sqlite:/tmp/app.db?mode=rwc",
        ] {
            assert_eq!(redact_connection_url(url), url);
        }
    }

    #[test]
    fn redact_masks_every_secret_query_key() {
        for key in [
            "password",
            "db_PASSWORD",
            "passwd",
            "pwd",
            "client_secret",
            "access_token",
            "apikey",
            "api_key",
            "service_role",
            "pass%77ord",
        ] {
            assert_eq!(
                redact_connection_url(&format!("postgresql://u@h/db?{key}=hunter2")),
                format!("postgresql://u@h/db?{key}=***"),
                "{key}"
            );
        }
    }

    #[test]
    fn redact_keeps_a_non_secret_query_key() {
        assert_eq!(
            redact_connection_url(
                "postgresql://u:pw@h/db?sslmode=require&password=x&application_name=pinch"
            ),
            "postgresql://u:***@h/db?sslmode=require&password=***&application_name=pinch"
        );
    }

    #[test]
    fn redact_keeps_the_fragment() {
        assert_eq!(
            redact_connection_url("postgresql://u:pw@h/db?token=t#replica"),
            "postgresql://u:***@h/db?token=***#replica"
        );
    }

    #[tokio::test]
    async fn a_connection_failure_names_the_url_with_its_password_masked() {
        let url = "postgresql://u:secret@127.0.0.1:1/db";
        let err = match connect_engine_handle(
            url,
            Dialect::Postgres,
            None,
            1,
            0,
            crate::session_settings::SettingsDelivery::Transaction,
        )
        .await
        {
            Ok(_) => panic!("nothing listens on port 1"),
            Err(err) => err,
        };
        let message = format!("{}: {}", connection_failed_context(url), err);
        assert!(message.contains("u:***@"), "{message}");
        assert!(!message.contains("secret"), "{message}");
        assert!(!message.contains("<redacted>"), "{message}");
    }

    #[tokio::test]
    async fn connect_engine_handle_uses_typed_sqlite_backend() {
        let engine = connect_engine_handle(
            "sqlite::memory:",
            Dialect::Sqlite,
            None,
            5,
            0,
            crate::session_settings::SettingsDelivery::Transaction,
        )
        .await
        .unwrap();

        assert_eq!(engine.backend(), Dialect::Sqlite);
        assert!(engine.sqlite_pool().is_some());
        assert!(engine.postgres_pool().is_none());
    }

    #[tokio::test]
    async fn connect_engine_handle_supports_sqlite_runtime_execution() {
        let engine = connect_engine_handle(
            "sqlite::memory:",
            Dialect::Sqlite,
            None,
            5,
            0,
            crate::session_settings::SettingsDelivery::Transaction,
        )
        .await
        .unwrap();

        assert_eq!(engine.backend(), Dialect::Sqlite);
        assert!(engine.sqlite_pool().is_some());
        assert_eq!(engine.execute_sql("SELECT 1").await.unwrap(), 0);
    }
}
