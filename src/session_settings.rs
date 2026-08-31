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
//! opening a transaction. Every settings delivery path — the explicit
//! `transaction()` block, the implicit transaction [`OperationScope`] opens
//! around a non-transactional operation, and the mid-session ones that follow
//! — goes through them rather than growing its own copy of the statement.
//!
//! [`OperationScope`] is where "which connection does this operation's
//! statements run on" is decided, once per operation: the ambient
//! transaction's, an implicit one it opens for a settings-bearing session, or
//! the pool. It is the reason a plain `Model.where().all()` outside any
//! `transaction()` is tenant-scoped.

use crate::backend::{EngineBindValue, EngineConnection, EngineHandle};
use crate::state::{TransactionConnection, session_state};
use ferro_ddl_lowering::Dialect;
use pyo3::PyResult;
use pyo3::exceptions::PyRuntimeError;
use tokio::sync::Mutex;

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

/// One ORM operation's execution scope: the connection every statement of that
/// operation runs on, plus the implicit transaction the operation opened for
/// them when it had to open one.
///
/// The wrap is per *operation*, never per statement — a `save()` that probes
/// the table catalog before its `INSERT` sends both inside one wrap, and a
/// chunked `bulk_create` sends every chunk inside one wrap. That is the
/// operation-atomicity invariant (CONTEXT.md): an operation issues one
/// statement, or several inside one transaction. Settings delivery rides it
/// rather than inventing a second boundary.
///
/// Every operation opens a scope, and the scope takes one of three shapes:
///
/// | Route | Connection | What the operation sends |
/// |---|---|---|
/// | Inside `transaction()` | that transaction's | its own statements — the block owns `BEGIN`/`COMMIT` |
/// | Settings-bearing session on Postgres, no transaction | one pool connection, held for the whole operation | `BEGIN`, the `set_config` batch, its statements, `COMMIT` |
/// | Everything else | the pool, one statement at a time | its own statements, byte for byte as before |
///
/// A session that carries no settings — and every sessionless call, and every
/// session that lands on a non-Postgres connection — therefore keeps today's
/// unwrapped path exactly: [`settings_would_apply`] is the only switch, and it
/// is false for all of them.
///
/// Everything an operation sends on its route is inside the wrap, including
/// the once-per-table catalog probe (which already shares the operation's
/// connection inside `transaction()`), so an implicitly wrapped operation
/// behaves exactly like the same operation written inside `transaction()`.
/// What is outside it is schema work — `connect`, `create_tables`, the
/// migration and introspection passes: they run against the engine rather than
/// through an operation route, carry no session, and have no row security to
/// scope.
pub struct OperationScope {
    /// The transaction this operation opened for itself, if it opened one.
    /// Owned outright — nothing else can reach it — so [`Self::finish`] can
    /// take the connection back and close it when it has to.
    owned: Option<Mutex<EngineConnection>>,
    /// The ambient `transaction()`'s connection, when the operation is inside
    /// one. Borrowed from the transaction registry; this scope never ends it.
    ambient: Option<TransactionConnection>,
}

impl OperationScope {
    /// Decide one operation's connection, opening an implicit transaction when
    /// the operation needs one.
    ///
    /// # Arguments
    /// * `engine` — Engine for the resolved connection.
    /// * `session_id` — Session the operation belongs to, or `None`.
    /// * `ambient` — The open `transaction()`'s connection, when there is one.
    /// * `atomic_required` — `true` when the operation will issue several
    ///   statements and must stay all-or-nothing even without settings (the
    ///   `bulk_create` chunking contract, #298).
    /// * `context` — Error label for a failed `BEGIN`, so each operation keeps
    ///   its own diagnostic (e.g. "Bulk save failed for 'User'").
    ///
    /// # Errors
    /// `PyRuntimeError` when the session id is unknown, or when `BEGIN` or the
    /// settings batch fails (see [`begin_transaction_with_settings`]).
    pub async fn open(
        engine: &EngineHandle,
        session_id: Option<&str>,
        ambient: Option<TransactionConnection>,
        atomic_required: bool,
        context: &str,
    ) -> PyResult<Self> {
        if ambient.is_some() {
            // An open transaction already is the boundary, and its `BEGIN`
            // already carried the settings.
            return Ok(Self {
                owned: None,
                ambient,
            });
        }

        if !atomic_required && !settings_would_apply(engine.backend(), session_id)? {
            return Ok(Self {
                owned: None,
                ambient: None,
            });
        }

        let conn = begin_transaction_with_settings(engine, session_id, context).await?;
        Ok(Self {
            owned: Some(Mutex::new(conn)),
            ambient: None,
        })
    }

    /// The connection this operation's statements must run on, or `None` to
    /// run them on the pool one statement at a time (the unwrapped path).
    #[must_use]
    pub fn connection(&self) -> Option<&Mutex<EngineConnection>> {
        match (&self.owned, &self.ambient) {
            (Some(owned), _) => Some(owned),
            (None, Some(ambient)) => Some(ambient),
            (None, None) => None,
        }
    }

    /// Close the operation's own transaction around `outcome` and hand the
    /// outcome back: `COMMIT` when the operation succeeded, `ROLLBACK` when it
    /// did not. A scope that opened nothing returns `outcome` untouched.
    ///
    /// Every operation runs its whole body as one `PyResult` precisely so that
    /// there is exactly one path out of it, and this is on it.
    ///
    /// # Arguments
    /// * `outcome` — What the operation produced, error included.
    ///
    /// # Errors
    /// The operation's own error, unchanged, when it failed; otherwise a
    /// `PyRuntimeError` when `COMMIT` fails.
    pub async fn finish<T>(self, outcome: PyResult<T>) -> PyResult<T> {
        let Some(owned) = self.owned else {
            return outcome;
        };
        let mut conn = owned.into_inner();

        match outcome {
            Ok(value) => {
                conn.commit()
                    .await
                    .map_err(|e| crate::errors::map_db_error("Failed to COMMIT", e))?;
                Ok(value)
            }
            Err(err) => {
                // The operation's own failure is the honest error, so it
                // survives the rollback either way. But a connection whose
                // `ROLLBACK` failed may be stuck idle-in-transaction — sqlx
                // only pings on release — so it is discarded rather than
                // handed to the next checkout.
                if conn.rollback().await.is_err() {
                    let _ = conn.detach_and_close().await;
                }
                Err(err)
            }
        }
    }
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

    // ----------------------------------------------------------------
    // OperationScope: which operations get a wrap, and what closes it.
    //
    // SQLite stands in for "a backend that delivers no settings": the gate
    // that keeps a settings-less session on the unwrapped path is the same
    // one, so these pin the decision without needing a live Postgres.
    // ----------------------------------------------------------------

    /// `max_connections(1)`: the wrap holds the pool's only connection, so a
    /// scope that failed to release it would hang the next read rather than
    /// pass quietly.
    async fn sqlite_engine() -> EngineHandle {
        let pool = sqlx::sqlite::SqlitePoolOptions::new()
            .max_connections(1)
            .connect("sqlite::memory:")
            .await
            .expect("sqlite memory pool");
        EngineHandle::new_sqlite(pool)
    }

    async fn create_marker_table(engine: &EngineHandle) {
        engine
            .execute_sql("CREATE TABLE marker (id INTEGER PRIMARY KEY)")
            .await
            .expect("create table");
    }

    async fn marker_ids(engine: &EngineHandle) -> Vec<i64> {
        engine
            .fetch_all_sql_with_binds("SELECT id FROM marker ORDER BY id", &[])
            .await
            .expect("read markers")
            .iter()
            .filter_map(|row| row.values.first().and_then(|(_, v)| v.as_i64()))
            .collect()
    }

    #[tokio::test]
    async fn a_sessionless_operation_opens_nothing() {
        let engine = sqlite_engine().await;
        let scope = OperationScope::open(&engine, None, None, false, "test")
            .await
            .expect("scope opens");
        assert!(
            scope.connection().is_none(),
            "no session, no settings, no wrap — the operation runs on the pool"
        );
    }

    #[tokio::test]
    async fn inherited_settings_stay_inert_off_postgres() {
        let engine = sqlite_engine().await;
        let session_id = crate::state::register_session(vec![setting("pinch.ledger_id", "acme")]);

        let scope = OperationScope::open(&engine, Some(&session_id), None, false, "test")
            .await
            .expect("scope opens");
        assert!(
            scope.connection().is_none(),
            "settings a session only inherited have nothing to apply to on SQLite, \
             so the operation stays unwrapped rather than failing"
        );

        crate::state::unregister_session(&session_id);
    }

    #[tokio::test]
    async fn a_multi_statement_operation_wraps_and_commits() {
        let engine = sqlite_engine().await;
        create_marker_table(&engine).await;

        let scope = OperationScope::open(&engine, None, None, true, "test")
            .await
            .expect("scope opens");
        let conn = scope.connection().expect("an atomic operation gets a wrap");
        conn.lock()
            .await
            .execute_sql("INSERT INTO marker (id) VALUES (1)")
            .await
            .expect("insert");

        scope.finish(Ok(())).await.expect("commit");
        assert_eq!(marker_ids(&engine).await, vec![1]);
    }

    #[tokio::test]
    async fn a_failed_operation_leaves_nothing_behind() {
        let engine = sqlite_engine().await;
        create_marker_table(&engine).await;

        let scope = OperationScope::open(&engine, None, None, true, "test")
            .await
            .expect("scope opens");
        scope
            .connection()
            .expect("an atomic operation gets a wrap")
            .lock()
            .await
            .execute_sql("INSERT INTO marker (id) VALUES (1)")
            .await
            .expect("insert");

        let failure: PyResult<()> = Err(PyRuntimeError::new_err("operation failed"));
        let err = scope.finish(failure).await.expect_err("the failure stands");
        assert!(err.to_string().contains("operation failed"));
        assert!(
            marker_ids(&engine).await.is_empty(),
            "the wrap rolled back, so a statement that already succeeded is gone too"
        );
    }

    #[tokio::test]
    async fn an_ambient_transaction_is_borrowed_never_committed() {
        use std::sync::Arc;

        let engine = sqlite_engine().await;
        create_marker_table(&engine).await;

        let tx: TransactionConnection = Arc::new(Mutex::new(
            engine
                .begin_transaction_connection()
                .await
                .expect("begin transaction"),
        ));

        let scope = OperationScope::open(&engine, None, Some(tx.clone()), true, "test")
            .await
            .expect("scope opens");
        scope
            .connection()
            .expect("the operation runs on the open transaction")
            .lock()
            .await
            .execute_sql("INSERT INTO marker (id) VALUES (1)")
            .await
            .expect("insert");

        // `atomic_required` is set, and it still must not end a transaction it
        // does not own: `transaction()` decides when this commits.
        scope.finish(Ok(())).await.expect("finish");
        tx.lock().await.rollback().await.expect("rollback");
        drop(tx);

        assert!(
            marker_ids(&engine).await.is_empty(),
            "finish() committed a transaction it had only borrowed"
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
