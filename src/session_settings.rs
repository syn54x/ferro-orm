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
use crate::state::{SessionState, TransactionConnection, TransactionHandle, session_state};
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
/// The single read site for [`crate::state::SessionState::settings_snapshot`], so
/// "which settings apply" has one answer for every delivery path.
///
/// # Arguments
/// * `session_id` — Session the route belongs to, or `None`.
///
/// # Errors
/// `PyRuntimeError` when the session id is unknown.
pub fn session_settings_for(session_id: Option<&str>) -> PyResult<Vec<SessionSetting>> {
    match session_id {
        Some(id) => Ok(session_state(id)?.settings_snapshot()),
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
        Some(id) => Ok(!session_state(id)?.settings_snapshot().is_empty()),
        None => Ok(false),
    }
}

/// Whether a `set_config` mutation actually changes anything.
///
/// The gate for the whole downstream mutation: reapplying to open
/// transactions and evicting the identity map are both skipped when this is
/// `false`. Effective settings are already an ordered list (declaration
/// order, `Session._merge_ambient_settings`), so a plain, order-sensitive
/// equality check is the exact answer — anything that did not change the
/// rendered `set_config` batch must not re-emit it.
///
/// # Arguments
/// * `current` — The session's settings before the call.
/// * `new` — The full proposed effective settings after the call (the
///   session's prior settings merged with the one key/value that changed).
#[must_use]
pub fn settings_changed(current: &[SessionSetting], new: &[SessionSetting]) -> bool {
    current != new
}

/// Run one statement against every distinct connection a session currently
/// has an open transaction on.
///
/// A session's nested `transaction()` savepoints all share their root's
/// connection (`SET LOCAL` is transaction-scoped, not savepoint-scoped), so
/// the same connection can appear under several transaction ids in
/// [`SessionState::transaction_registry`] — deduplicated here
/// by pointer identity so a session with several open savepoints still runs
/// the statement exactly once per connection.
///
/// Generic over the statement so it is testable without a live Postgres
/// connection (see the unit tests below); [`reapply_settings_to_open_transactions`]
/// is the one production caller, and it always passes a rendered
/// `set_config` batch.
///
/// # Errors
/// `PyRuntimeError` when the statement fails on any of the session's open
/// connections.
pub async fn execute_on_open_transactions(
    session: &SessionState,
    sql: &str,
    binds: &[EngineBindValue],
) -> PyResult<()> {
    // Address as `usize` rather than a raw pointer: a pointer held across an
    // `.await` makes the whole future non-`Send`, which `future_into_py`
    // requires (the pointer is never dereferenced — only compared).
    let mut seen: Vec<usize> = Vec::new();
    for entry in session.transaction_registry.iter() {
        let conn = entry.value().conn.clone();
        let addr = std::sync::Arc::as_ptr(&conn) as usize;
        if seen.contains(&addr) {
            continue;
        }
        seen.push(addr);
        conn.lock()
            .await
            .execute_sql_with_binds(sql, binds)
            .await
            .map_err(|e| crate::errors::map_db_error("Failed to apply session settings", e))?;
    }
    Ok(())
}

/// Deliver a session's just-changed effective settings to every transaction
/// it currently has open — the eager half of the mid-transaction `set_config`
/// contract (#411): the very next statement in an already-open `transaction()`
/// on this session sees the new value, without waiting for that transaction's
/// next `BEGIN` (there won't be one until it ends).
///
/// A session with no open transactions — the common case, `set_config`
/// between transactions or before the first one — reapplies nothing here;
/// the next `BEGIN` picks up the new settings through
/// [`begin_transaction_with_settings`] like any other, so no second copy of
/// the delivery logic exists for this path.
///
/// # Arguments
/// * `session` — The session whose settings changed.
/// * `settings` — The full new effective settings (never empty: `set_config`
///   always adds or overwrites at least one key).
///
/// # Errors
/// `PyRuntimeError` when the batch fails on any of the session's open
/// transaction connections.
pub async fn reapply_settings_to_open_transactions(
    session: &SessionState,
    settings: &[SessionSetting],
) -> PyResult<()> {
    let Some((sql, binds)) = render_set_config_batch(settings) else {
        return Ok(());
    };
    execute_on_open_transactions(session, &sql, &binds).await
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
    /// take the connection back and close it when it has to. Taking it is also
    /// how "this scope was closed deliberately" is recorded: still `Some` at
    /// drop time means the operation was cancelled, and `Drop` discards it.
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

    /// A scope that never opens a transaction of its own, whatever the session
    /// carries — for a statement that Postgres refuses to run inside one
    /// (`CREATE INDEX CONCURRENTLY`, `VACUUM`, pre-12 `ALTER TYPE ... ADD
    /// VALUE`), reached through `ferro.raw.execute(..., autocommit=True)`.
    ///
    /// An ambient `transaction()` is still honoured: the caller asked for a
    /// transaction explicitly, and Postgres refusing the statement inside it is
    /// the right answer. What this skips is the *implicit* wrap, which the
    /// caller never asked for. Such a statement is therefore not tenant-scoped
    /// — it is maintenance DDL, not tenant data.
    ///
    /// # Arguments
    /// * `ambient` — The open `transaction()`'s connection, when there is one.
    #[must_use]
    pub fn unwrapped(ambient: Option<TransactionConnection>) -> Self {
        Self {
            owned: None,
            ambient,
        }
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
    /// there is exactly one path out of it, and this is on it. The path that is
    /// *not* a value — the operation's future being dropped mid-flight because
    /// the caller was cancelled — is [`Drop`]'s job.
    ///
    /// # Arguments
    /// * `outcome` — What the operation produced, error included.
    ///
    /// # Errors
    /// The operation's own error, unchanged, when it failed; otherwise a
    /// `PyRuntimeError` when `COMMIT` fails.
    pub async fn finish<T>(mut self, outcome: PyResult<T>) -> PyResult<T> {
        // Taking it is what tells `Drop` this scope was closed deliberately.
        let Some(owned) = self.owned.take() else {
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

/// A scope dropped without [`OperationScope::finish`] discards its connection.
///
/// This is the cancellation path, and it is not a nicety. When the caller's
/// asyncio task is cancelled — a client disconnects, `asyncio.wait_for` times
/// out, a `TaskGroup` sibling fails — the operation's Rust future is dropped
/// wherever it was suspended, so `finish` never runs. The connection it was
/// holding is a pool connection, not an `sqlx::Transaction`, so nothing rolls
/// it back on drop and the pool only pings before reuse: it would go back
/// idle-in-transaction with the cancelled request's `SET LOCAL` scope still
/// live, and the next checkout — another tenant's request — would run inside
/// it. That is a cross-tenant read.
///
/// So the connection is *discarded*, never salvaged: [`EngineConnection::detach`]
/// is synchronous, so severing the pool's claim always happens, right here,
/// before anything else can get the connection. The close handshake is spawned
/// when there is a runtime to spawn it on; without one the value simply drops
/// and the socket shuts. Burning one connection is the correct price for a
/// cancelled operation — and unlike "roll it back and reuse it", there is no
/// window in which the poisoned connection is reachable.
impl Drop for OperationScope {
    fn drop(&mut self) {
        let Some(owned) = self.owned.take() else {
            return;
        };
        let detached = owned.into_inner().detach();
        if let Ok(runtime) = tokio::runtime::Handle::try_current() {
            runtime.spawn(async move {
                let _ = detached.close().await;
            });
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
    async fn a_cancelled_operation_never_returns_its_connection_to_the_pool() {
        // A file database, not `:memory:`: an in-memory SQLite database lives
        // inside its connection, so discarding the connection would discard
        // the data and the assertion would pass for the wrong reason.
        let path = std::env::temp_dir().join(format!(
            "ferro_scope_cancel_{}.db",
            uuid::Uuid::new_v4().simple()
        ));
        let pool = sqlx::sqlite::SqlitePoolOptions::new()
            .max_connections(1)
            .connect(&format!("sqlite://{}?mode=rwc", path.display()))
            .await
            .expect("sqlite file pool");
        let engine = EngineHandle::new_sqlite(pool);
        create_marker_table(&engine).await;

        {
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
            // Dropped without `finish`: exactly what happens when the caller's
            // asyncio task is cancelled mid-operation.
        }
        // Let the spawned close run before the pool is asked for a connection.
        tokio::task::yield_now().await;

        // The pool's only connection was discarded, so this is a fresh one —
        // if the poisoned one had been handed back, this read would be running
        // inside the abandoned transaction and would see the uncommitted row.
        assert!(
            marker_ids(&engine).await.is_empty(),
            "a cancelled operation's transaction was handed to the next checkout"
        );

        // And the fresh connection is not stuck mid-transaction: a write on it
        // commits normally.
        engine
            .execute_sql("INSERT INTO marker (id) VALUES (2)")
            .await
            .expect("the replacement connection is usable");
        assert_eq!(marker_ids(&engine).await, vec![2]);

        let _ = std::fs::remove_file(&path);
    }

    #[tokio::test]
    async fn an_autocommit_statement_opens_no_transaction() {
        let engine = sqlite_engine().await;
        let session_id = crate::state::register_session(vec![setting("pinch.ledger_id", "acme")]);

        // Same session, same backend — `open` would wrap this on Postgres.
        let scope = OperationScope::unwrapped(None);
        assert!(
            scope.connection().is_none(),
            "an autocommit statement must reach the server outside any transaction"
        );

        crate::state::unregister_session(&session_id);
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

    // ----------------------------------------------------------------
    // Mid-transaction `set_config` (#411): the no-redundant-reapplication
    // gate, and eager delivery to a session's open transactions.
    // ----------------------------------------------------------------

    #[test]
    fn unchanged_settings_are_not_a_change() {
        let current = vec![
            setting("pinch.ledger_id", "acme"),
            setting("app.role", "owner"),
        ];
        let same = current.clone();
        assert!(
            !settings_changed(&current, &same),
            "identical settings must not be treated as a change — no reapplication, \
             no identity-map eviction"
        );
    }

    #[test]
    fn a_new_value_for_an_existing_key_is_a_change() {
        let current = vec![setting("pinch.ledger_id", "acme")];
        let updated = vec![setting("pinch.ledger_id", "globex")];
        assert!(settings_changed(&current, &updated));
    }

    #[test]
    fn an_added_key_is_a_change() {
        let current = vec![setting("pinch.ledger_id", "acme")];
        let updated = vec![
            setting("pinch.ledger_id", "acme"),
            setting("app.role", "owner"),
        ];
        assert!(settings_changed(&current, &updated));
    }

    #[test]
    fn empty_to_empty_is_not_a_change() {
        assert!(!settings_changed(&[], &[]));
    }

    /// A file database of its own: two of these stand in for two genuinely
    /// distinct pool connections without either seeing the other's writes —
    /// an in-memory `:memory:` database lives inside one connection, and two
    /// connections to the *same* file would fight over SQLite's single
    /// writer lock the moment both are mid-transaction, which is a real
    /// `sqlx::Error` this crate maps through `ferro.exceptions` (unavailable
    /// to a bare `cargo test`, which has no Python interpreter with `ferro`
    /// importable) — precisely the failure mode this test must not exercise.
    async fn marker_engine() -> (EngineHandle, std::path::PathBuf) {
        let path = std::env::temp_dir().join(format!(
            "ferro_session_settings_reapply_{}.db",
            uuid::Uuid::new_v4().simple()
        ));
        let pool = sqlx::sqlite::SqlitePoolOptions::new()
            .max_connections(1)
            .connect(&format!("sqlite://{}?mode=rwc", path.display()))
            .await
            .expect("sqlite file pool");
        let engine = EngineHandle::new_sqlite(pool);
        engine
            .execute_sql("CREATE TABLE marker (id INTEGER)")
            .await
            .expect("create table");
        (engine, path)
    }

    #[tokio::test]
    async fn execute_on_open_transactions_runs_once_per_distinct_connection() {
        let (engine_a, path_a) = marker_engine().await;
        let (engine_b, path_b) = marker_engine().await;
        let session_id = crate::state::register_session(Vec::new());
        let session = crate::state::session_state(&session_id).expect("session registered");

        let conn_a = engine_a
            .begin_transaction_connection()
            .await
            .expect("begin transaction A");
        let conn_b = engine_b
            .begin_transaction_connection()
            .await
            .expect("begin transaction B");
        let handle_a = TransactionHandle::root(conn_a, "default".to_string());
        let handle_b = TransactionHandle::root(conn_b, "default".to_string());
        // A savepoint on A shares A's connection — this is what dedup must
        // collapse back down to one execution.
        let nested_a = TransactionHandle::nested(
            handle_a.conn.clone(),
            "sp_1".to_string(),
            "default".to_string(),
        );
        session
            .transaction_registry
            .insert("tx-a".to_string(), handle_a.clone());
        session
            .transaction_registry
            .insert("tx-a-nested".to_string(), nested_a);
        session
            .transaction_registry
            .insert("tx-b".to_string(), handle_b.clone());

        execute_on_open_transactions(&session, "INSERT INTO marker (id) VALUES (1)", &[])
            .await
            .expect("statement runs on every distinct connection");

        for handle in [&handle_a, &handle_b] {
            let mut guard = handle.conn.lock().await;
            let rows = guard
                .fetch_all_sql_with_binds("SELECT id FROM marker", &[])
                .await
                .expect("read marker");
            assert_eq!(
                rows.len(),
                1,
                "each connection must see the statement exactly once, not once per \
                 registry entry sharing it"
            );
        }

        crate::state::unregister_session(&session_id);
        drop(handle_a);
        drop(handle_b);
        let _ = std::fs::remove_file(&path_a);
        let _ = std::fs::remove_file(&path_b);
    }

    #[tokio::test]
    async fn reapply_settings_to_open_transactions_dispatches_the_rendered_batch() {
        // `reapply_settings_to_open_transactions` is `render_set_config_batch`
        // (already pinned above: the real Postgres `set_config(...)` SQL,
        // parameter-bound) piped straight into `execute_on_open_transactions`
        // (dedup pinned above). What is left to pin here is that the two are
        // actually wired together — settings that render nothing dispatch
        // nothing, so an open transaction that received no statement at all
        // proves the "no batch, no dispatch" wiring without needing a
        // Postgres-only statement to succeed against SQLite.
        let (engine, path) = marker_engine().await;
        let session_id = crate::state::register_session(Vec::new());
        let session = crate::state::session_state(&session_id).expect("session registered");

        let conn = engine
            .begin_transaction_connection()
            .await
            .expect("begin transaction");
        let handle = TransactionHandle::root(conn, "default".to_string());
        session
            .transaction_registry
            .insert("tx".to_string(), handle.clone());

        reapply_settings_to_open_transactions(&session, &[])
            .await
            .expect("no settings renders no batch, so there is nothing to dispatch");

        let mut guard = handle.conn.lock().await;
        let rows = guard
            .fetch_all_sql_with_binds("SELECT id FROM marker", &[])
            .await
            .expect("read marker");
        assert!(
            rows.is_empty(),
            "empty settings must not reach the connection at all"
        );
        drop(guard);

        crate::state::unregister_session(&session_id);
        drop(handle);
        let _ = std::fs::remove_file(&path);
    }

    #[tokio::test]
    async fn reapply_with_no_open_transactions_is_a_silent_no_op() {
        let session_id = crate::state::register_session(Vec::new());
        let session = crate::state::session_state(&session_id).expect("session registered");

        reapply_settings_to_open_transactions(&session, &[setting("pinch.ledger_id", "acme")])
            .await
            .expect("nothing to reapply to is not an error");

        crate::state::unregister_session(&session_id);
    }

    #[tokio::test]
    async fn settings_snapshot_reflects_replace_settings() {
        let session_id = crate::state::register_session(vec![setting("a.one", "1")]);
        let session = crate::state::session_state(&session_id).expect("session registered");

        assert_eq!(session.settings_snapshot(), vec![setting("a.one", "1")]);

        session.replace_settings(vec![setting("a.one", "2"), setting("b.two", "3")]);
        assert_eq!(
            session.settings_snapshot(),
            vec![setting("a.one", "2"), setting("b.two", "3")]
        );

        crate::state::unregister_session(&session_id);
    }
}
