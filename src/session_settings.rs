//! Session settings — the `SET LOCAL` batch a settings-bearing session applies
//! to every Postgres transaction it opens.
//!
//! A *session setting* is one custom Postgres setting (GUC) with a string
//! value, e.g. `"pinch.ledger_id" -> "acme"`. A session that carries settings
//! makes them visible to every statement it runs; row-level-security policies
//! read them back with `current_setting('pinch.ledger_id', true)`.
//!
//! `transaction` settings delivery renders the whole set as exactly ONE
//! parameter-bound statement, issued immediately after `BEGIN` and before any
//! user statement. A session opened with
//! `settings={"pinch.ledger_id": "acme", "app.role": "admin"}` therefore sends:
//!
//! ```sql
//! BEGIN
//! SELECT set_config($1, $2, true), set_config($3, $4, true)
//! -- binds: ['pinch.ledger_id', 'acme', 'app.role', 'admin']
//! ```
//!
//! The `true` is `set_config`'s `is_local` flag: the value lives until the
//! transaction ends, so a recycled pool connection can never hand one request's
//! tenant value to the next. Keys *and* values are always binds — nothing from
//! a session's settings is ever interpolated into SQL text.
//!
//! [`render_set_config_batch`] is the single renderer and
//! [`apply_session_settings`] the single execution seam, with
//! [`begin_transaction_with_settings`] wrapping the pair for callers that are
//! opening a transaction. Every settings delivery path — today's transactional
//! one, and the non-transactional and mid-session ones that follow — goes
//! through them rather than growing its own copy of the statement.

use crate::backend::{EngineBindValue, EngineConnection, EngineHandle};
use crate::state::session_state;
use ferro_ddl_lowering::Dialect;
use pyo3::PyResult;
use pyo3::exceptions::PyRuntimeError;

/// One session setting: `(key, value)`, both bound as parameters.
///
/// Ordered rather than mapped: the order Python declared the settings in is the
/// order they are rendered in, so the emitted statement is deterministic and
/// pinnable by test.
pub type SessionSetting = (String, String);

/// Render the `set_config` batch for a session's effective settings.
///
/// # Arguments
/// * `settings` — Effective settings in declaration order.
///
/// # Returns
/// `Some((sql, binds))` — one `SELECT` with N `set_config` calls and 2N binds
/// (key, value, key, value, …) — or `None` when there is nothing to apply.
/// A session without settings renders `None` and therefore emits no statement
/// at all: zero added round-trips for everyone who is not using the feature.
pub fn render_set_config_batch(
    settings: &[SessionSetting],
) -> Option<(String, Vec<EngineBindValue>)> {
    if settings.is_empty() {
        return None;
    }

    let mut sql = String::from("SELECT ");
    let mut binds = Vec::with_capacity(settings.len() * 2);
    for (index, (key, value)) in settings.iter().enumerate() {
        if index > 0 {
            sql.push_str(", ");
        }
        let key_param = index * 2 + 1;
        sql.push_str(&format!(
            "set_config(${}, ${}, true)",
            key_param,
            key_param + 1
        ));
        binds.push(EngineBindValue::String(key.clone()));
        binds.push(EngineBindValue::String(value.clone()));
    }
    Some((sql, binds))
}

/// Apply a session's effective settings to a connection that has just begun a
/// transaction.
///
/// Called once per root `BEGIN`; nested savepoints inherit the values because
/// `SET LOCAL` is transaction-scoped, not savepoint-scoped.
///
/// # Arguments
/// * `conn` — Connection with an open transaction.
/// * `settings` — Effective settings in declaration order.
///
/// # Errors
/// The underlying `sqlx::Error` when the batch fails to execute. Callers must
/// abort the transaction they just opened rather than continuing unscoped.
pub async fn apply_session_settings(
    conn: &mut EngineConnection,
    settings: &[SessionSetting],
) -> Result<(), sqlx::Error> {
    let Some((sql, binds)) = render_set_config_batch(settings) else {
        return Ok(());
    };
    conn.execute_sql_with_binds(&sql, &binds).await?;
    Ok(())
}

/// Reject settings a session *declared* on a non-Postgres connection.
///
/// Declaring `settings=` is a promise the backend has to keep, and only
/// Postgres can: silently skipping them would turn a tenancy boundary into a
/// no-op. So this fires at session open, on the session's own dict.
///
/// It deliberately does **not** apply to *inherited* settings. A session nested
/// inside a settings-bearing one carries its parent's settings so that a
/// Postgres descendant still inherits them, but on a non-Postgres connection
/// those settings simply lie inert — an auxiliary SQLite database has no row
/// security to scope, and refusing to open a session on it would make ordinary
/// multi-database nesting impossible.
///
/// # Arguments
/// * `backend` — Dialect of the resolved connection.
/// * `connection_name` — Registered connection name, for the message.
/// * `declared` — Settings this session declared itself; empty is always fine.
///
/// # Errors
/// `PyRuntimeError` when `declared` is non-empty and `backend` is not Postgres.
pub fn ensure_postgres_for_settings(
    backend: Dialect,
    connection_name: &str,
    declared: &[SessionSetting],
) -> PyResult<()> {
    if declared.is_empty() || backend == Dialect::Postgres {
        return Ok(());
    }
    Err(PyRuntimeError::new_err(format!(
        "Session settings require a Postgres connection, but connection '{}' is \
         {:?}. Session settings are Postgres settings (GUCs) applied with \
         set_config(); no other backend can honour them. Open the session on a \
         Postgres connection, or drop `settings=` — a nested session that only \
         inherits settings from an outer session opens fine on any backend (they \
         stay inert there).",
        connection_name, backend
    )))
}

/// A session's effective settings, or an empty set for a sessionless route.
///
/// The single read site for [`crate::state::SessionState::settings`], so
/// "which settings apply" has one answer for every delivery path.
///
/// # Arguments
/// * `session_id` — Session the route belongs to, or `None`.
///
/// # Errors
/// `PyRuntimeError` when the session id is unknown.
pub fn session_settings_for(session_id: Option<&str>) -> PyResult<Vec<SessionSetting>> {
    match session_id {
        Some(id) => Ok(session_state(id)?.settings.clone()),
        None => Ok(Vec::new()),
    }
}

/// Whether a `BEGIN` on this route would actually deliver settings.
///
/// True only when the session carries settings *and* the connection is
/// Postgres — the same gate [`begin_transaction_with_settings`] applies. An
/// operation that would self-wrap only to give the settings somewhere to land
/// asks this first, so it never opens a transaction that would deliver nothing.
///
/// # Arguments
/// * `backend` — Dialect of the resolved connection.
/// * `session_id` — Session the route belongs to, or `None`.
///
/// # Errors
/// `PyRuntimeError` when the session id is unknown.
pub fn settings_would_apply(backend: Dialect, session_id: Option<&str>) -> PyResult<bool> {
    if backend != Dialect::Postgres {
        return Ok(false);
    }
    match session_id {
        Some(id) => Ok(!session_state(id)?.settings.is_empty()),
        None => Ok(false),
    }
}

/// `BEGIN` on `engine` with the route's session settings already applied.
///
/// The single "open a transaction inside a session" seam. Every `BEGIN` Ferro
/// issues goes through here — the explicit `transaction()` block and the
/// self-wrap a multi-statement operation opens when there is no ambient
/// transaction (the operation-atomicity invariant) — so the two can never
/// drift into one applying settings and the other not.
///
/// Settings are delivered only on Postgres connections. Anything a session
/// merely inherited is inert elsewhere (see [`ensure_postgres_for_settings`]);
/// settings a session *declared* can never reach here on a non-Postgres
/// connection, because opening that session already raised.
///
/// # Arguments
/// * `engine` — Engine for the resolved connection.
/// * `session_id` — Session the transaction belongs to, or `None`.
/// * `begin_context` — Error label for a failed `BEGIN`, supplied by the caller
///   so each site keeps its own diagnostic (e.g. "Bulk save failed for 'User'").
///
/// # Errors
/// `PyRuntimeError` when `BEGIN` fails, or when the settings batch fails — in
/// which case the just-opened transaction is rolled back rather than continuing
/// unscoped, and the connection is discarded if even that rollback fails.
pub async fn begin_transaction_with_settings(
    engine: &EngineHandle,
    session_id: Option<&str>,
    begin_context: &str,
) -> PyResult<EngineConnection> {
    let settings = if engine.backend() == Dialect::Postgres {
        session_settings_for(session_id)?
    } else {
        Vec::new()
    };

    let mut conn = engine
        .begin_transaction_connection()
        .await
        .map_err(|e| crate::errors::map_db_error(begin_context, e))?;

    if let Err(err) = apply_session_settings(&mut conn, &settings).await {
        // The transaction is open and unscoped, so it must not survive. If even
        // the rollback fails the connection may be stuck idle-in-transaction —
        // sqlx only pings on release — so it is discarded rather than handed to
        // the next checkout.
        if conn.rollback().await.is_err() {
            let _ = conn.detach_and_close().await;
        }
        return Err(crate::errors::map_db_error(
            "Failed to apply session settings",
            err,
        ));
    }

    Ok(conn)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn setting(key: &str, value: &str) -> SessionSetting {
        (key.to_string(), value.to_string())
    }

    fn bind_strings(binds: &[EngineBindValue]) -> Vec<String> {
        binds
            .iter()
            .map(|bind| match bind {
                EngineBindValue::String(text) => text.clone(),
                other => panic!("expected a text bind, got {:?}", other),
            })
            .collect()
    }

    #[test]
    fn empty_settings_render_nothing() {
        assert!(render_set_config_batch(&[]).is_none());
    }

    #[test]
    fn one_setting_renders_one_set_config() {
        let (sql, binds) = render_set_config_batch(&[setting("pinch.ledger_id", "acme")])
            .expect("one setting renders a statement");
        assert_eq!(sql, "SELECT set_config($1, $2, true)");
        assert_eq!(bind_strings(&binds), vec!["pinch.ledger_id", "acme"]);
    }

    #[test]
    fn many_settings_render_one_statement() {
        let (sql, binds) = render_set_config_batch(&[
            setting("pinch.ledger_id", "acme"),
            setting("app.role", "admin"),
            setting("app.request_id", "r-1"),
        ])
        .expect("three settings render a statement");

        assert_eq!(
            sql,
            "SELECT set_config($1, $2, true), \
             set_config($3, $4, true), \
             set_config($5, $6, true)"
        );
        assert_eq!(sql.matches("set_config").count(), 3);
        assert_eq!(sql.matches(';').count(), 0);
        assert_eq!(
            bind_strings(&binds),
            vec![
                "pinch.ledger_id",
                "acme",
                "app.role",
                "admin",
                "app.request_id",
                "r-1",
            ]
        );
    }

    #[test]
    fn values_are_never_interpolated() {
        let hostile = "'); DROP TABLE users; --";
        let (sql, binds) = render_set_config_batch(&[setting("pinch.ledger_id", hostile)])
            .expect("one setting renders a statement");
        assert!(!sql.contains("DROP TABLE"));
        assert_eq!(bind_strings(&binds)[1], hostile);
    }

    #[test]
    fn declaration_order_is_preserved() {
        let (_, binds) = render_set_config_batch(&[setting("b.two", "2"), setting("a.one", "1")])
            .expect("two settings render a statement");
        assert_eq!(bind_strings(&binds), vec!["b.two", "2", "a.one", "1"]);
    }

    #[test]
    fn settings_never_apply_off_postgres_or_off_session() {
        // No session, no settings — and SQLite short-circuits before it would
        // need a session at all, so an unknown id can never be consulted there.
        assert!(!settings_would_apply(Dialect::Postgres, None).expect("no session"));
        assert!(!settings_would_apply(Dialect::Sqlite, None).expect("no session"));
        assert!(
            !settings_would_apply(Dialect::Sqlite, Some("no-such-session"))
                .expect("sqlite short-circuits")
        );
    }

    #[test]
    fn sqlite_rejects_declared_settings_but_allows_none() {
        assert!(ensure_postgres_for_settings(Dialect::Sqlite, "default", &[]).is_ok());
        assert!(ensure_postgres_for_settings(Dialect::Postgres, "default", &[]).is_ok());
        assert!(
            ensure_postgres_for_settings(
                Dialect::Postgres,
                "default",
                &[setting("pinch.ledger_id", "acme")]
            )
            .is_ok()
        );
        assert!(
            ensure_postgres_for_settings(
                Dialect::Sqlite,
                "local",
                &[setting("pinch.ledger_id", "acme")]
            )
            .is_err()
        );
    }
}
