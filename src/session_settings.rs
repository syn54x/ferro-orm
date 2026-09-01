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
use crate::state::{
    ConnectionSlot, SessionState, TransactionConnection, TransactionHandle, discard_connection,
    session_state,
};
use ferro_ddl_lowering::Dialect;
use pyo3::PyResult;
use pyo3::exceptions::PyRuntimeError;
use std::sync::Arc;
use std::sync::RwLock;
use std::sync::atomic::{AtomicUsize, Ordering};
use tokio::sync::Mutex;

/// How a session's settings reach the database (CONTEXT.md, *Settings
/// delivery*). Chosen per connection pool, never auto-detected: a transaction
/// pooler is invisible to its clients, and guessing wrong is a cross-tenant
/// leak.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum SettingsDelivery {
    /// The default. `set_config(k, v, true)` — `SET LOCAL` — inside every
    /// transaction, with non-transactional operations self-wrapping. Correct
    /// behind any pooler, including PgBouncer transaction mode.
    #[default]
    Transaction,
    /// Opt-in, direct-Postgres only. A settings-bearing session pins one pool
    /// connection at first use, applies `set_config(k, v, false)` once on it,
    /// and resets exactly those keys when it closes.
    Connection,
}

impl SettingsDelivery {
    /// The token `PoolConfig(settings_delivery=...)` accepts.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            SettingsDelivery::Transaction => "transaction",
            SettingsDelivery::Connection => "connection",
        }
    }

    /// Parse the Python-side token.
    ///
    /// # Errors
    /// `PyValueError` naming both accepted tokens. `PoolConfig` already
    /// rejects anything else at the call site; this is the FFI's own gate, so
    /// a direct `_core.connect(...)` caller cannot smuggle a third mode in.
    pub fn parse(token: &str) -> PyResult<Self> {
        match token {
            "transaction" => Ok(SettingsDelivery::Transaction),
            "connection" => Ok(SettingsDelivery::Connection),
            other => Err(pyo3::exceptions::PyValueError::new_err(format!(
                "Unknown settings_delivery '{other}'. Use \"{}\" (the default — \
                 SET LOCAL inside every transaction, safe behind any pooler) or \
                 \"{}\" (one pinned connection per settings-bearing session; only \
                 for a pool that talks to Postgres directly).",
                SettingsDelivery::Transaction.as_str(),
                SettingsDelivery::Connection.as_str(),
            ))),
        }
    }
}

/// One session setting: `(key, value)`, both bound as parameters.
///
/// Ordered rather than mapped: the order Python declared the settings in is the
/// order they are rendered in, so the emitted statement is deterministic and
/// pinnable by test.
pub type SessionSetting = (String, String);

/// The custom setting a pinned connection carries while it is pinned: the
/// comma-joined list of keys the session has applied on it.
///
/// It is Ferro's own bookkeeping, not a user setting — a connection that
/// reaches the pool's release hook still carrying it is one that escaped the
/// close path, and the hook refuses to put it back. Reset alongside the keys
/// it names (see [`render_reset_config_batch`]).
pub const PIN_MARKER_KEY: &str = "ferro.pinned_keys";

/// Render the `set_config` batch for a session's effective settings.
///
/// # Arguments
/// * `settings` — Effective settings in declaration order.
/// * `is_local` — `set_config`'s third argument, and the whole difference
///   between the two settings deliveries. `true` (`transaction` delivery)
///   scopes each value to the transaction the batch is issued in, so a
///   recycled pool connection can never carry it into the next request.
///   `false` (`connection` delivery) sets it for the whole database session,
///   which is only safe because the connection is pinned to this Ferro
///   session and reset before it goes back to the pool.
///
/// # Returns
/// `Some((sql, binds))` — one `SELECT` with N `set_config` calls and 2N binds
/// (key, value, key, value, …) — or `None` when there is nothing to apply.
/// A session without settings renders `None` and therefore emits no statement
/// at all: zero added round-trips for everyone who is not using the feature.
pub fn render_set_config_batch(
    settings: &[SessionSetting],
    is_local: bool,
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
            "set_config(${}, ${}, {})",
            key_param,
            key_param + 1,
            is_local
        ));
        binds.push(EngineBindValue::String(key.clone()));
        binds.push(EngineBindValue::String(value.clone()));
    }
    Some((sql, binds))
}

/// Render the batch that puts a pinned connection back the way it was found:
/// one parameter-bound reset per key, and nothing else.
///
/// `RESET` itself is a utility statement and takes no parameters, so a
/// key would have to be pasted into SQL text to use it — which is exactly what
/// the settings feature promises never to do. `set_config(key, NULL, false)`
/// is the same operation as a function call: a `NULL` value means "back to
/// this setting's default", and the key stays a bind.
///
/// It is deliberately *not* `RESET ALL`. A pool connection also carries state
/// Ferro put there on behalf of the whole application — the `search_path` from
/// `ferro_search_path`, applied once by the pool's `after_connect` hook —
/// and `RESET ALL` would take that with it, leaving every later query on that
/// connection resolving tables in the wrong schema.
///
/// **What "default" means, exactly.** A reset restores the value the
/// connection *started* with, not the empty string. For a custom setting no
/// one has configured, those are the same thing and the key reads back as
/// `''` — which is the fail-closed behaviour the `NULLIF` policy contract
/// relies on. But `ALTER ROLE ... SET myapp.tenant_id = ...` or
/// `ALTER DATABASE ... SET ...` makes that configured value the startup value,
/// so resetting brings it *back* rather than clearing it. Never set a tenancy
/// key as a role or database default: a session that reset it would then be
/// scoped to whatever the operator configured instead of to nothing.
///
/// # Arguments
/// * `keys` — Every key this session applied on the connection, plus
///   [`PIN_MARKER_KEY`].
///
/// # Returns
/// `Some((sql, binds))` — one `SELECT` with N `set_config` calls and N binds —
/// or `None` when there is nothing to reset.
pub fn render_reset_config_batch(keys: &[String]) -> Option<(String, Vec<EngineBindValue>)> {
    if keys.is_empty() {
        return None;
    }

    let mut sql = String::from("SELECT ");
    let mut binds = Vec::with_capacity(keys.len());
    for (index, key) in keys.iter().enumerate() {
        if index > 0 {
            sql.push_str(", ");
        }
        sql.push_str(&format!("set_config(${}, NULL, false)", index + 1));
        binds.push(EngineBindValue::String(key.clone()));
    }
    Some((sql, binds))
}

/// The value [`PIN_MARKER_KEY`] carries while a connection is pinned.
#[must_use]
pub fn render_pin_marker(keys: &[String]) -> String {
    keys.join(",")
}

/// Whether a connection reaching the pool's release hook is still carrying a
/// session's settings.
///
/// The pure half of the `after_release` safety net (see
/// [`crate::backend::PoolSpec`]): a pinned connection is only ever released by
/// the close path, which resets the marker along with the keys it names, so a
/// non-empty marker here means the connection escaped that path and must not
/// be handed to another session.
#[must_use]
pub fn pinned_marker_is_dirty(marker: &str) -> bool {
    !marker.trim().is_empty()
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
    apply_settings_batch(conn, settings, true).await
}

/// Apply a settings batch to a connection at the chosen scope.
///
/// The single execution seam both deliveries share: `transaction` delivery
/// passes `is_local = true` right after `BEGIN`; `connection` delivery passes
/// `false` once, at pin, and again whenever `set_config` changes the set.
///
/// # Arguments
/// * `conn` — Connection to apply the batch on.
/// * `settings` — Settings in declaration order; empty applies nothing.
/// * `is_local` — See [`render_set_config_batch`].
///
/// # Errors
/// The underlying `sqlx::Error`. Callers must abort whatever they were about
/// to do rather than continuing unscoped.
pub async fn apply_settings_batch(
    conn: &mut EngineConnection,
    settings: &[SessionSetting],
    is_local: bool,
) -> Result<(), sqlx::Error> {
    let Some((sql, binds)) = render_set_config_batch(settings, is_local) else {
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

/// Merge one `(key, value)` into a session's current effective settings:
/// replace an existing key's value in place, or append a new key at the end.
/// The same update policy Python's `{**effective, key: value}` follows for
/// `settings=` at open — performed here, on the Rust side, so that two
/// concurrent `set_config` calls on the same session always merge against the
/// session's actual last-committed settings rather than a Python-side mirror
/// that a sibling task's own concurrent `set_config` could have made stale.
/// `operations::set_session_config` calls this from inside the session's
/// `settings_write_lock`, so the read (`current`) and the eventual write are
/// one atomic step from the caller's perspective — two sibling-task
/// `set_config` calls setting two different keys can never lose one of them
/// to a lost-update race, because the second call's `current` always
/// reflects the first call's already-committed merge.
///
/// # Arguments
/// * `current` — The session's settings before this call, in declaration
///   order.
/// * `key` / `value` — The one setting `set_config` is applying.
#[must_use]
pub fn merge_setting(current: &[SessionSetting], key: &str, value: &str) -> Vec<SessionSetting> {
    let mut merged = current.to_vec();
    if let Some(existing) = merged.iter_mut().find(|(k, _)| k == key) {
        existing.1 = value.to_string();
    } else {
        merged.push((key.to_string(), value.to_string()));
    }
    merged
}

/// Snapshot the distinct connections a session currently has an open
/// transaction on, deduplicated by connection identity.
///
/// A session's nested `transaction()` savepoints all share their root's
/// connection (`SET LOCAL` is transaction-scoped, not savepoint-scoped), so
/// the same connection can appear under several transaction ids in
/// [`SessionState::transaction_registry`].
///
/// Deliberately synchronous and side-effect-free — it only clones `Arc`
/// handles into a `Vec` and returns. That is the whole fix for a real bug: a
/// `for entry in session.transaction_registry.iter() { ... await ... }` loop
/// holds that entry's `DashMap` shard guard for the entire loop body,
/// including the `.await` and the database round trip beneath it — blocking
/// any concurrent `tx_insert`/`tx_remove` on that shard for as long as the
/// statement takes. Collecting first and dropping the iterator before any
/// caller awaits removes the guard from the picture entirely.
fn open_transaction_connections(session: &SessionState) -> Vec<TransactionConnection> {
    // Address as `usize` rather than a raw pointer: a pointer held across an
    // `.await` makes an enclosing future non-`Send`, which `future_into_py`
    // requires (the pointer is never dereferenced — only compared). Moot for
    // this function itself (it never awaits), but callers hold the
    // `Vec<TransactionConnection>` this returns across their own awaits, so
    // the type collected into must stay `Send`-friendly.
    let mut seen: Vec<usize> = Vec::new();
    let mut connections: Vec<TransactionConnection> = Vec::new();
    for entry in session.transaction_registry.iter() {
        let conn = entry.value().conn.clone();
        let addr = std::sync::Arc::as_ptr(&conn) as usize;
        if seen.contains(&addr) {
            continue;
        }
        seen.push(addr);
        connections.push(conn);
    }
    connections
}

/// Run one statement against each of `connections`, in order.
///
/// No `SessionState`/`DashMap` involvement at all: by the time this runs,
/// [`open_transaction_connections`] has already collected and released
/// whatever bookkeeping decided which connections to target, so this loop's
/// `.await` points never contend with a session's transaction registry.
///
/// # Errors
/// `PyRuntimeError` when the statement fails on any connection.
async fn execute_on_connections(
    connections: &[TransactionConnection],
    sql: &str,
    binds: &[EngineBindValue],
) -> PyResult<()> {
    for conn in connections {
        let mut guard = conn.lock().await;
        guard
            .live()
            .map_err(|e| crate::errors::map_db_error("Failed to apply session settings", e))?
            .execute_sql_with_binds(sql, binds)
            .await
            .map_err(|e| crate::errors::map_db_error("Failed to apply session settings", e))?;
    }
    Ok(())
}

/// Deliver a session's just-changed effective settings to every transaction
/// it currently has open — the eager half of the mid-transaction `set_config`
/// contract (#411).
///
/// **Concurrency contract, stated precisely** (see `Session.set_config`'s
/// docstring for the user-facing version): an operation or transaction
/// already in flight when `set_config` runs completes under the scope it
/// began with; anything that *starts* after `set_config`'s `await` returns —
/// in any task — carries the new scope, because the settings swap
/// happens-before that return. This function is what makes the *same-task,
/// already-open* `transaction()` case keep up rather than lagging until that
/// transaction's next `BEGIN` (there won't be one until it ends) — and there
/// is no race to resolve there: the code that called `set_config` and the
/// code about to run the transaction's next statement are sequential in the
/// same task, by construction. A concurrent *sibling* task's already-open
/// transaction is a different, smaller promise — "finishes under the scope
/// it started with" — which is operation-atomicity working as intended, not
/// a gap this function needs to close.
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
/// transaction connections. Whatever the outcome, the settings swap and the
/// identity-map eviction the caller performs before calling this have
/// already committed (see `operations::set_session_config`) — a failure here
/// never leaves the session's recorded scope and its identity map out of
/// sync with each other; Postgres has by then aborted the transaction the
/// failing statement ran on (a failed statement poisons the transaction
/// until `ROLLBACK`), so that transaction cannot go on to leak an old-scope
/// read either.
pub async fn reapply_settings_to_open_transactions(
    session: &SessionState,
    settings: &[SessionSetting],
) -> PyResult<()> {
    let Some((sql, binds)) = render_set_config_batch(settings, true) else {
        return Ok(());
    };
    let connections = open_transaction_connections(session);
    for conn in &connections {
        // Invariant, checked rather than merely trusted: `set_config` already
        // requires the session's own connection to be Postgres
        // (`ensure_postgres_for_settings`, enforced before this function is
        // ever reached — see `operations::set_session_config`), and a
        // session's open transactions can only ever be on that same
        // connection (FF-D routing pins one connection per session), so
        // every entry here is Postgres today by construction. A
        // `debug_assert` — compiled out in release, per AGENTS.md I-3, which
        // forbids ever panicking across the FFI boundary — means a future
        // routing bug surfaces as this named invariant breaking under
        // `cargo test`, rather than as a confusing "no such function:
        // set_config" error from whichever backend it was sent to instead.
        let mut guard = conn.lock().await;
        let dialect = guard
            .live()
            .map(|conn| conn.dialect())
            .unwrap_or(Dialect::Postgres);
        drop(guard);
        debug_assert_eq!(
            dialect,
            Dialect::Postgres,
            "reapply_settings_to_open_transactions dispatched a Postgres-only \
             set_config batch to a non-Postgres connection"
        );
    }
    execute_on_connections(&connections, &sql, &binds).await
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

// ---------------------------------------------------------------------------
// `connection` settings delivery: one pool connection, pinned to one session
// ---------------------------------------------------------------------------

/// The pool connection a settings-bearing session holds for its whole life
/// under `connection` settings delivery, and the keys it has set on it.
///
/// The trade the mode makes, stated plainly. `transaction` delivery pays for
/// safety behind a pooler with a `BEGIN`, a `set_config` batch and a `COMMIT`
/// around every operation that is not already inside a `transaction()`.
/// `connection` delivery pays once: it takes a connection out of the pool at
/// the session's first operation, sends
///
/// ```sql
/// SELECT set_config($1, $2, false), set_config($3, $4, false)
/// -- binds: ['pinch.ledger_id', 'acme', 'ferro.pinned_keys', 'pinch.ledger_id']
/// ```
///
/// and then sends the session's statements *bare* — no wrap, no repetition —
/// because `false` (`set_config`'s `is_local`) makes the value last for the
/// whole database session rather than one transaction. What buys that back is
/// the pin: the connection belongs to this Ferro session and to nothing else
/// until it closes, at which point exactly the keys named above are reset and
/// the connection goes back to the pool.
///
/// The price is a concurrency cap: a settings-bearing session holds a pool
/// connection from its first operation to its close, so no more of them can
/// run at once than the pool has connections. Session N+1's first operation
/// waits for a connection, exactly as any other pool checkout does.
///
/// # Why it is discarded rather than salvaged
///
/// A connection carrying live session-level settings is one statement away
/// from being another tenant's connection, so it must never reach the pool
/// un-reset. `Drop` therefore *takes* the connection out of its slot and
/// detaches it — severing the pool's claim synchronously — whenever the pin
/// ends any way other than a clean release: a cancelled operation, a failed
/// reset, `reset_engine()` tearing the session registry down. Burning one
/// connection is the correct price; "roll it back and reuse it" leaves a
/// window in which the poisoned connection is reachable.
pub struct PinnedConnection {
    /// The checked-out connection. Emptied by [`release_pinned_connection`]
    /// (reset, then back to the pool) or by a discard (detached, gone).
    conn: TransactionConnection,
    /// Serializes whole *operations* on this connection.
    ///
    /// The connection's own mutex only serializes one statement at a time,
    /// which is not enough. Two sibling tasks sharing a pinned session would
    /// otherwise interleave the statements of two multi-statement operations
    /// on one connection: nested `BEGIN`s (which Postgres warns about and
    /// then ignores), one operation's `COMMIT` committing the other's
    /// half-written rows, and the other's later failure rolling back nothing.
    /// Per-operation atomicity would be gone, silently.
    ///
    /// So the unit of exclusion is the operation, not the statement: an
    /// operation takes this for its whole body, and a `transaction()` block
    /// takes it for its whole `BEGIN`→`COMMIT`/`ROLLBACK` span. A pinned
    /// session therefore serializes everything it does, which is the honest
    /// meaning of a session that owns exactly one connection — and exactly
    /// what the documented concurrency cap already says.
    ///
    /// Operations running *inside* an ambient `transaction()` do not take it:
    /// the block already owns the span they run in, so they are reentrant by
    /// construction and no recursive locking is needed.
    gate: Arc<Mutex<()>>,
    /// Every settings key ever applied on this connection, in the order it
    /// was first applied — the exact reset list at close.
    touched: RwLock<Vec<String>>,
    /// The engine's outstanding-pin gauge. See [`crate::backend::PoolSpec`]:
    /// it is what lets the pool's release hook cost nothing at all when no
    /// session is pinned, which is every moment on a `transaction`-delivery
    /// pool and every moment a `connection`-delivery pool is serving only
    /// settings-less work.
    pins: Arc<AtomicUsize>,
}

/// An operation's exclusive claim on a pinned connection, held for the whole
/// operation (or the whole `transaction()` block). See [`PinnedConnection::gate`].
pub type PinHold = tokio::sync::OwnedMutexGuard<()>;

impl PinnedConnection {
    /// Take ownership of a freshly pinned connection.
    fn new(conn: EngineConnection, keys: Vec<String>, pins: Arc<AtomicUsize>) -> Self {
        pins.fetch_add(1, Ordering::SeqCst);
        Self {
            conn: Arc::new(ConnectionSlot::new(conn)),
            gate: Arc::new(Mutex::new(())),
            touched: RwLock::new(keys),
            pins,
        }
    }

    /// Claim this connection for one whole operation, waiting for whatever
    /// operation or `transaction()` block currently holds it.
    pub async fn hold(&self) -> PinHold {
        self.gate.clone().lock_owned().await
    }

    /// The slot this session's statements run on.
    #[must_use]
    pub fn slot(&self) -> &ConnectionSlot {
        &self.conn
    }

    /// A shareable handle on the slot — for a `transaction()` opened inside
    /// the session, which `BEGIN`s on this very connection.
    #[must_use]
    pub fn shared_slot(&self) -> TransactionConnection {
        self.conn.clone()
    }

    /// Whether this pin still holds a usable connection.
    ///
    /// Read straight off the slot's tombstone, which is the single owner of
    /// the fact: whoever gives up on the connection — a cancelled operation,
    /// a failed reset, a `transaction()` whose `COMMIT` failed — tombstones
    /// the slot, and every reader agrees immediately. The check is lock-free
    /// on purpose: it happens while another task may be mid-statement.
    #[must_use]
    pub fn is_live(&self) -> bool {
        !self.conn.is_discarded()
    }

    /// Record keys applied on this connection, keeping first-applied order.
    ///
    /// The reset list only ever grows. `set_config` can add a key mid-session
    /// (and v1 has no way to remove one), and a key that was set must be reset
    /// even if a later change made it look redundant — tracking what was
    /// *touched* rather than what is currently effective is what keeps that
    /// true no matter how the settings API grows.
    pub fn record_keys(&self, settings: &[SessionSetting]) {
        let mut touched = match self.touched.write() {
            Ok(guard) => guard,
            Err(poisoned) => poisoned.into_inner(),
        };
        for (key, _) in settings {
            if !touched.iter().any(|existing| existing == key) {
                touched.push(key.clone());
            }
        }
    }

    /// Every settings key applied on this connection — the value
    /// [`PIN_MARKER_KEY`] advertises, and the one shape both writers use.
    #[must_use]
    pub fn touched_keys(&self) -> Vec<String> {
        match self.touched.read() {
            Ok(guard) => guard.clone(),
            Err(poisoned) => poisoned.into_inner().clone(),
        }
    }

    /// The exact reset list at close: every touched key, plus the marker that
    /// advertised them.
    #[must_use]
    pub fn reset_keys(&self) -> Vec<String> {
        let mut keys = self.touched_keys();
        keys.push(PIN_MARKER_KEY.to_string());
        keys
    }

    /// The settings batch to send on this connection: the effective settings,
    /// plus the marker naming every key they will have touched.
    ///
    /// The single marker writer, so pin time and a live `set_config` can never
    /// advertise two different shapes — the marker is a diagnostic, and one
    /// that sometimes lists itself is not one.
    #[must_use]
    fn batch_with_marker(&self, settings: &[SessionSetting]) -> Vec<SessionSetting> {
        let mut batch = settings.to_vec();
        batch.push((
            PIN_MARKER_KEY.to_string(),
            render_pin_marker(&self.touched_keys()),
        ));
        batch
    }

    /// Give up on this connection: it never goes back to the pool, and the
    /// session's next operation pins a fresh one.
    pub fn discard(&self) {
        discard_connection_slot(&self.conn);
    }
}

impl Drop for PinnedConnection {
    fn drop(&mut self) {
        self.pins.fetch_sub(1, Ordering::SeqCst);
        // A slot that still holds a connection at drop is one no close path
        // released — the session was torn down under it (`reset_engine()`), or
        // its close was itself cancelled. Either way the connection still
        // carries this session's settings, so it is discarded, not returned.
        discard_connection_slot(&self.conn);
    }
}

/// Take a connection out of its slot and sever the pool's claim on it.
///
/// Tombstoning comes first and needs no lock, so from this line on the slot
/// can never be used or pooled again whatever else happens. Taking the
/// connection out is then done by whoever can: this call if the lock is free
/// (which it is in every case that matters — a cancelled operation's future
/// was dropped, guard and all), a spawned task if it is not, and the slot's
/// own `Drop` if there is no runtime to spawn on.
pub fn discard_connection_slot(slot: &TransactionConnection) {
    slot.tombstone();
    if let Some(mut guard) = slot.try_lock() {
        discard_connection(guard.discard());
        return;
    }
    let slot = slot.clone();
    if let Ok(runtime) = tokio::runtime::Handle::try_current() {
        runtime.spawn(async move {
            let taken = slot.lock().await.discard();
            discard_connection(taken);
        });
    }
}

/// The session whose pinned connection this route runs on, when there is one.
/// The session whose pinned connection this route runs on, when there is one.
///
/// `Some` only for a settings-bearing session on a Postgres pool configured
/// for `connection` delivery. Everything else — every session on a
/// `transaction`-delivery pool, every settings-less session, every
/// non-Postgres connection — is `None` and keeps today's behavior exactly.
///
/// # Errors
/// `PyRuntimeError` when the session id is unknown.
pub fn pinned_session(engine: &EngineHandle, session_id: Option<&str>) -> PyResult<Option<String>> {
    if engine.settings_delivery() != SettingsDelivery::Connection {
        return Ok(None);
    }
    if !settings_would_apply(engine.backend(), session_id)? {
        return Ok(None);
    }
    // `settings_would_apply` is only ever true for a session that exists.
    Ok(session_id.map(str::to_string))
}

/// The connection this session runs every statement on, pinning one on first
/// use.
///
/// Idempotent and race-free: the session's pin slot is held across the whole
/// decision, so two sibling tasks racing a session's first operation produce
/// one pinned connection, not two. A pin whose connection was discarded (a
/// cancelled operation) is replaced here rather than reused.
///
/// # Arguments
/// * `engine` — Engine for the session's connection.
/// * `session_id` — The settings-bearing session.
///
/// # Errors
/// `PyRuntimeError` when the session id is unknown, when the pool cannot hand
/// out a connection, or when the settings batch fails — in which case the
/// connection is discarded rather than returned half-configured.
pub async fn pinned_connection_for_session(
    engine: &EngineHandle,
    session_id: &str,
) -> PyResult<Arc<PinnedConnection>> {
    let session = session_state(session_id)?;
    let mut slot = session.pinned.lock().await;

    if let Some(pin) = slot.as_ref()
        && pin.is_live()
    {
        return Ok(pin.clone());
    }

    let settings = session.settings_snapshot();
    let keys: Vec<String> = settings.iter().map(|(key, _)| key.clone()).collect();

    let mut conn = engine
        .acquire_pinned_connection()
        .await
        .map_err(|e| crate::errors::map_db_error("Failed to pin a session connection", e))?;

    let mut batch = settings.clone();
    batch.push((PIN_MARKER_KEY.to_string(), render_pin_marker(&keys)));
    if let Err(err) = apply_settings_batch(&mut conn, &batch, false).await {
        // The connection may already carry part of the batch, so it is never
        // handed back — the same discard-not-salvage posture every other
        // dirty-connection path takes.
        let _ = conn.detach_and_close().await;
        return Err(crate::errors::map_db_error(
            "Failed to apply session settings",
            err,
        ));
    }

    let pin = Arc::new(PinnedConnection::new(conn, keys, engine.pin_gauge()));
    *slot = Some(pin.clone());
    Ok(pin)
}

/// This session's pinned connection, claimed for one whole operation.
///
/// Two steps, in this order and never nested: pin (or reuse the pin), then
/// wait for whatever operation currently owns the connection. The session's
/// pin slot is released before the wait, so a task waiting its turn never
/// blocks another task from finding the same pin.
///
/// The re-check afterwards closes the one gap between them: a cancelled
/// operation can discard the pin while this call is queued for it, and a
/// discarded pin's connection is gone. Rather than fail an operation that
/// never started, the loop pins again — [`pinned_connection_for_session`]
/// replaces a dead pin with a fresh connection.
///
/// # Errors
/// Whatever [`pinned_connection_for_session`] raises.
pub async fn hold_pinned_connection(
    engine: &EngineHandle,
    session_id: &str,
) -> PyResult<(Arc<PinnedConnection>, PinHold)> {
    loop {
        let pin = pinned_connection_for_session(engine, session_id).await?;
        let hold = pin.hold().await;
        if pin.is_live() {
            return Ok((pin, hold));
        }
    }
}

/// `BEGIN` on a session's pinned connection, applying no settings.
///
/// A `transaction()` block inside a pinned session runs on the connection the
/// session already holds, and the settings are already *on* that connection at
/// database-session scope — so this `BEGIN` carries no `set_config` batch.
/// That is the whole point of the mode: the transaction costs a `BEGIN`, not a
/// `BEGIN` plus a re-application.
///
/// # Errors
/// `PyRuntimeError` when `BEGIN` fails or the pinned connection was discarded.
pub async fn begin_on_pinned(pin: &PinnedConnection, context: &str) -> PyResult<()> {
    let mut guard = pin.slot().lock().await;
    guard
        .live()
        .map_err(|e| crate::errors::map_db_error(context, e))?
        .execute_sql("BEGIN")
        .await
        .map_err(|e| crate::errors::map_db_error(context, e))?;
    Ok(())
}

/// Deliver a session's just-changed settings, whichever delivery it is under.
///
/// The one dispatch point for the mid-session `set_config` contract (#411):
///
/// - `connection` delivery, session already pinned — re-send the whole batch
///   at database-session scope on the pinned connection. It takes effect for
///   the very next statement, transaction open or not.
/// - `connection` delivery, not pinned yet — nothing to deliver here. A
///   session that had no settings at open has not pinned anything, so it pins
///   on its **next operation** and applies the new set at that point.
/// - `transaction` delivery — re-apply `SET LOCAL` to every transaction the
///   session currently has open (see
///   [`reapply_settings_to_open_transactions`]).
///
/// This deliberately does **not** take the pin's operation hold: `set_config`
/// is legal from inside an open `transaction()` in the same task, and that
/// block already owns the hold — waiting for it would deadlock. The
/// consequence is worth stating: on a pinned connection the new value lands
/// on the connection immediately, so a *sibling* task's multi-statement
/// operation that is already in flight can see it from its next statement
/// onward. Under `transaction` delivery that operation would finish under the
/// scope it began with. Sharing one session across tasks and changing its
/// scope underneath them is the only way to reach the difference.
///
/// # Errors
/// `PyRuntimeError` when the batch fails. See `operations::set_session_config`
/// for what has already committed by then and why that is sound.
pub async fn deliver_changed_settings(
    engine: &EngineHandle,
    session: &SessionState,
    settings: &[SessionSetting],
) -> PyResult<()> {
    if engine.settings_delivery() != SettingsDelivery::Connection {
        return reapply_settings_to_open_transactions(session, settings).await;
    }

    let pin = session
        .pinned
        .lock()
        .await
        .as_ref()
        .filter(|pin| pin.is_live())
        .cloned();
    let Some(pin) = pin else {
        return Ok(());
    };

    pin.record_keys(settings);
    let batch = pin.batch_with_marker(settings);

    let mut guard = pin.slot().lock().await;
    let conn = guard
        .live()
        .map_err(|e| crate::errors::map_db_error("Failed to apply session settings", e))?;
    apply_settings_batch(conn, &batch, false)
        .await
        .map_err(|e| crate::errors::map_db_error("Failed to apply session settings", e))
}

/// Put a session's pinned connection back the way it was found and return it
/// to the pool.
///
/// Resets exactly the keys this session applied (plus Ferro's own marker) —
/// never `RESET ALL`, which would take the pool's `search_path` with it. If
/// the reset fails, the connection is discarded instead of returned: a
/// connection Ferro cannot prove is clean is not one another session may have.
///
/// Takes the pin's operation hold first, so the reset can never land between
/// two statements of an operation a sibling task still has in flight. A close
/// therefore waits for such an operation to finish, which is the same wait
/// any other operation on the session would do.
///
/// The caller must already have removed the session from the registry, so
/// that a close whose `await` is cancelled here drops `pin` — and `Drop`
/// discards, rather than stranding a connection the pool can never reclaim.
///
/// # Arguments
/// * `pin` — The session's pin, owned (the registry's handle is gone).
///
/// # Errors
/// `PyRuntimeError` when the reset batch fails; the connection has been
/// discarded by then.
pub async fn release_pinned_connection(pin: Arc<PinnedConnection>) -> PyResult<()> {
    let _hold = pin.hold().await;

    let keys = pin.reset_keys();
    let Some((sql, binds)) = render_reset_config_batch(&keys) else {
        return Ok(());
    };

    let outcome = {
        let mut guard = pin.slot().lock().await;
        if guard.is_empty() || pin.slot().is_discarded() {
            // Already discarded (a cancelled operation). Nothing to reset and
            // nothing to hand back.
            return Ok(());
        }
        let conn = guard
            .live()
            .map_err(|e| crate::errors::map_db_error("Failed to reset session settings", e))?;
        match conn.execute_sql_with_binds(&sql, &binds).await {
            // Taken out of the slot deliberately: dropping it *after* the
            // guard is what returns it to the pool, clean.
            Ok(_) => Ok(guard.release()),
            Err(err) => Err(err),
        }
    };

    match outcome {
        Ok(released) => {
            drop(pin);
            drop(released);
            Ok(())
        }
        Err(err) => {
            pin.discard();
            Err(crate::errors::map_db_error(
                "Failed to reset session settings on the pinned connection",
                err,
            ))
        }
    }
}

/// One ORM operation's execution scope: the connection every statement of that
/// operation runs on, plus whatever that scope owes it when the operation ends.
///
/// The wrap is per *operation*, never per statement — a `save()` that probes
/// the table catalog before its `INSERT` sends both inside one wrap, and a
/// chunked `bulk_create` sends every chunk inside one wrap. That is the
/// operation-atomicity invariant (CONTEXT.md): an operation issues one
/// statement, or several inside one transaction. Settings delivery rides it
/// rather than inventing a second boundary.
///
/// Every operation opens a scope, and the scope takes one of four shapes:
///
/// | Route | Connection | What the operation sends |
/// |---|---|---|
/// | Inside `transaction()` | that transaction's | its own statements — the block owns `BEGIN`/`COMMIT` |
/// | Settings-bearing session, `transaction` delivery, no transaction | one pool connection, held for the whole operation | `BEGIN`, the `set_config` batch, its statements, `COMMIT` |
/// | Settings-bearing session, `connection` delivery | the session's pinned connection | its own statements — nothing else, unless the operation needs atomicity, and then a bare `BEGIN`/`COMMIT` |
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
    /// Taking it is how "this scope was closed deliberately" is recorded:
    /// still `Some` at drop time means the operation was cancelled, and
    /// [`Drop`] discards whatever connection the route was holding.
    route: Option<ScopeRoute>,
}

/// Where one operation's statements go, and what closing the scope owes.
enum ScopeRoute {
    /// The pool, one statement at a time. Nothing to close.
    Pool,
    /// An ambient `transaction()`'s connection. Borrowed from the transaction
    /// registry; this scope never ends it.
    Ambient(TransactionConnection),
    /// A transaction this scope opened on a pool connection of its own
    /// (`transaction` delivery). Owned outright — nothing else can reach it —
    /// so `finish` can `COMMIT`/`ROLLBACK` and hand the connection back, and
    /// `Drop` can discard it.
    Owned(ConnectionSlot),
    /// The session's pinned connection (`connection` delivery). The scope
    /// borrows it for the operation and never returns it to the pool — that is
    /// the session's job, at close. `wrapped` is set when the operation needed
    /// multi-statement atomicity and this scope opened a bare transaction on
    /// the pinned connection for it.
    ///
    /// `_hold` is this operation's exclusive claim on the connection, kept
    /// for its lifetime rather than its value: holding it is what keeps two
    /// sibling tasks' operations from interleaving their statements on the
    /// one connection the session owns, and dropping it — however the scope
    /// ends — is what lets the next one in (see [`PinnedConnection::hold`]).
    Pinned {
        pin: Arc<PinnedConnection>,
        wrapped: bool,
        _hold: PinHold,
    },
}

impl OperationScope {
    /// Decide one operation's connection, opening an implicit transaction —
    /// or pinning the session's connection — when the operation needs one.
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
    /// `PyRuntimeError` when the session id is unknown, when the pool cannot
    /// hand out a connection, or when `BEGIN` or the settings batch fails (see
    /// [`begin_transaction_with_settings`] and
    /// [`pinned_connection_for_session`]).
    pub async fn open(
        engine: &EngineHandle,
        session_id: Option<&str>,
        ambient: Option<TransactionConnection>,
        atomic_required: bool,
        context: &str,
    ) -> PyResult<Self> {
        if let Some(ambient) = ambient {
            // An open transaction already is the boundary, and its `BEGIN`
            // already carried the settings (or, in `connection` delivery, ran
            // on the connection that already has them).
            return Ok(Self {
                route: Some(ScopeRoute::Ambient(ambient)),
            });
        }

        if let Some(session_id) = pinned_session(engine, session_id)? {
            // Claimed for the whole operation, not just each statement: two
            // sibling tasks sharing this session run one after the other.
            let (pin, hold) = hold_pinned_connection(engine, &session_id).await?;
            // The settings are already on this connection at database-session
            // scope, so an atomic operation opens a bare transaction: the
            // atomicity it asked for, and not one round trip more.
            if atomic_required {
                begin_on_pinned(&pin, context).await?;
            }
            return Ok(Self {
                route: Some(ScopeRoute::Pinned {
                    pin,
                    wrapped: atomic_required,
                    _hold: hold,
                }),
            });
        }

        if !atomic_required && !settings_would_apply(engine.backend(), session_id)? {
            return Ok(Self {
                route: Some(ScopeRoute::Pool),
            });
        }

        let conn = begin_transaction_with_settings(engine, session_id, context).await?;
        Ok(Self {
            route: Some(ScopeRoute::Owned(ConnectionSlot::new(conn))),
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
    /// caller never asked for.
    ///
    /// Under `transaction` delivery such a statement is therefore not
    /// tenant-scoped — there is nowhere for a `SET LOCAL` to live. Under
    /// `connection` delivery it still runs on the session's pinned connection,
    /// which already carries the settings, so it *is* scoped: the mode has no
    /// wrap to skip in the first place.
    ///
    /// # Arguments
    /// * `engine` — Engine for the resolved connection.
    /// * `session_id` — Session the statement belongs to, or `None`.
    /// * `ambient` — The open `transaction()`'s connection, when there is one.
    ///
    /// # Errors
    /// `PyRuntimeError` when the session id is unknown, or when pinning the
    /// session's connection fails.
    pub async fn unwrapped(
        engine: &EngineHandle,
        session_id: Option<&str>,
        ambient: Option<TransactionConnection>,
    ) -> PyResult<Self> {
        if let Some(ambient) = ambient {
            return Ok(Self {
                route: Some(ScopeRoute::Ambient(ambient)),
            });
        }
        if let Some(session_id) = pinned_session(engine, session_id)? {
            let (pin, hold) = hold_pinned_connection(engine, &session_id).await?;
            return Ok(Self {
                route: Some(ScopeRoute::Pinned {
                    pin,
                    wrapped: false,
                    _hold: hold,
                }),
            });
        }
        Ok(Self {
            route: Some(ScopeRoute::Pool),
        })
    }

    /// The connection this operation's statements must run on, or `None` to
    /// run them on the pool one statement at a time (the unwrapped path).
    #[must_use]
    pub fn connection(&self) -> Option<&ConnectionSlot> {
        match self.route.as_ref()? {
            ScopeRoute::Pool => None,
            ScopeRoute::Ambient(conn) => Some(conn),
            ScopeRoute::Owned(slot) => Some(slot),
            ScopeRoute::Pinned { pin, .. } => Some(pin.slot()),
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
        match self.route.take() {
            None | Some(ScopeRoute::Pool) | Some(ScopeRoute::Ambient(_)) => outcome,
            // The mode's whole point: an unwrapped operation on a pinned
            // connection opened nothing, so it closes nothing. The connection
            // stays with the session until the session closes.
            Some(ScopeRoute::Pinned { wrapped: false, .. }) => outcome,
            Some(ScopeRoute::Pinned {
                pin, wrapped: true, ..
            }) => close_pinned_wrap(&pin, outcome).await,
            Some(ScopeRoute::Owned(mut slot)) => {
                let Some(mut conn) = slot.take_owned() else {
                    // Unreachable today: an owned slot is only ever emptied by
                    // a discard, which happens on the `Drop` path instead.
                    return outcome;
                };
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
                        // `ROLLBACK` failed may be stuck idle-in-transaction —
                        // sqlx only pings on release — so it is discarded rather
                        // than handed to the next checkout.
                        if conn.rollback().await.is_err() {
                            let _ = conn.detach_and_close().await;
                        }
                        Err(err)
                    }
                }
            }
        }
    }
}

/// End the bare transaction an atomic operation opened on a pinned connection.
///
/// The connection stays with the session either way — unless the `ROLLBACK`
/// fails, which means Ferro can no longer vouch for it, so the pin is
/// discarded and the session's next operation pins a fresh one.
async fn close_pinned_wrap<T>(pin: &PinnedConnection, outcome: PyResult<T>) -> PyResult<T> {
    let mut guard = pin.slot().lock().await;
    let Ok(conn) = guard.live() else {
        return outcome;
    };
    match outcome {
        Ok(value) => {
            let committed = conn.commit().await;
            drop(guard);
            match committed {
                Ok(()) => Ok(value),
                Err(err) => {
                    // Symmetric with the rollback arm below, and for the same
                    // reason: a connection whose COMMIT failed may be sitting
                    // in an aborted or still-open transaction, and every later
                    // statement on it — including the session's own close
                    // reset — would fail with it. Ferro cannot vouch for it,
                    // so it does not keep it.
                    pin.discard();
                    Err(crate::errors::map_db_error("Failed to COMMIT", err))
                }
            }
        }
        Err(err) => {
            let rolled_back = conn.rollback().await.is_ok();
            drop(guard);
            if !rolled_back {
                pin.discard();
            }
            Err(err)
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
/// Under `connection` delivery the stake is higher, not lower. The pinned
/// connection carries the session's settings at *database-session* scope, so a
/// connection abandoned mid-statement would hand the next checkout a live
/// tenant value with no transaction end to clear it. It is therefore discarded
/// here too and the pin marked dead: the session's next operation pins a fresh
/// connection, and its close finds nothing to reset.
///
/// So the connection is *discarded*, never salvaged: [`EngineConnection::detach`]
/// is synchronous, so severing the pool's claim always happens, right here,
/// before anything else can get the connection. The close handshake is spawned
/// when there is a runtime to spawn it on; without one the value simply drops
/// and the socket shuts. Burning one connection is the correct price for a
/// cancelled operation — and unlike "roll it back and reuse it", there is no
/// window in which the poisoned connection is reachable.
///
/// An *ambient* transaction is the one route this leaves alone: the operation
/// only borrowed it, and the `transaction()` block that owns it is still there
/// to roll it back.
impl Drop for OperationScope {
    fn drop(&mut self) {
        match self.route.take() {
            Some(ScopeRoute::Owned(mut slot)) => discard_connection(slot.take_owned()),
            Some(ScopeRoute::Pinned { pin, .. }) => pin.discard(),
            _ => {}
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
        assert!(render_set_config_batch(&[], true).is_none());
    }

    #[test]
    fn one_setting_renders_one_set_config() {
        let (sql, binds) = render_set_config_batch(&[setting("pinch.ledger_id", "acme")], true)
            .expect("one setting renders a statement");
        assert_eq!(sql, "SELECT set_config($1, $2, true)");
        assert_eq!(bind_strings(&binds), vec!["pinch.ledger_id", "acme"]);
    }

    #[test]
    fn many_settings_render_one_statement() {
        let (sql, binds) = render_set_config_batch(
            &[
                setting("pinch.ledger_id", "acme"),
                setting("app.role", "admin"),
                setting("app.request_id", "r-1"),
            ],
            true,
        )
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

    // ----------------------------------------------------------------
    // `connection` delivery: the mode token, the session-level batch, and
    // the targeted reset that puts a pinned connection back.
    // ----------------------------------------------------------------

    #[test]
    fn only_the_two_documented_modes_parse() {
        assert_eq!(
            SettingsDelivery::parse("transaction").expect("the default parses"),
            SettingsDelivery::Transaction
        );
        assert_eq!(
            SettingsDelivery::parse("connection").expect("the opt-in parses"),
            SettingsDelivery::Connection
        );
        assert_eq!(SettingsDelivery::default(), SettingsDelivery::Transaction);

        let err = SettingsDelivery::parse("session").expect_err("no third mode exists");
        let message = err.to_string();
        assert!(message.contains("transaction"), "message: {message}");
        assert!(message.contains("connection"), "message: {message}");
    }

    #[test]
    fn every_mode_token_round_trips() {
        for mode in [SettingsDelivery::Transaction, SettingsDelivery::Connection] {
            assert_eq!(
                SettingsDelivery::parse(mode.as_str()).expect("its own token parses"),
                mode
            );
        }
    }

    #[test]
    fn connection_delivery_renders_a_session_level_batch() {
        // The one difference between the deliveries, on the wire: `false`
        // instead of `true` for `set_config`'s is_local flag. Everything
        // else — one statement, keys and values both bound — is identical.
        let settings = [
            setting("pinch.ledger_id", "acme"),
            setting("app.role", "admin"),
        ];
        let (local_sql, local_binds) =
            render_set_config_batch(&settings, true).expect("two settings render a statement");
        let (session_sql, session_binds) =
            render_set_config_batch(&settings, false).expect("two settings render a statement");

        assert_eq!(
            session_sql,
            "SELECT set_config($1, $2, false), set_config($3, $4, false)"
        );
        assert_eq!(local_sql, session_sql.replace("false", "true"));
        assert_eq!(bind_strings(&local_binds), bind_strings(&session_binds));
    }

    #[test]
    fn the_reset_batch_names_every_touched_key_and_nothing_else() {
        // `RESET ALL` would take the pool's `search_path` with it, so the
        // reset is per key — and parameter-bound, because `RESET` itself
        // takes no parameters and a key must never be pasted into SQL.
        let keys = vec![
            "pinch.ledger_id".to_string(),
            "app.role".to_string(),
            PIN_MARKER_KEY.to_string(),
        ];
        let (sql, binds) = render_reset_config_batch(&keys).expect("three keys render a statement");

        assert_eq!(
            sql,
            "SELECT set_config($1, NULL, false), \
             set_config($2, NULL, false), \
             set_config($3, NULL, false)"
        );
        assert!(!sql.to_ascii_uppercase().contains("RESET ALL"));
        assert_eq!(
            bind_strings(&binds),
            vec!["pinch.ledger_id", "app.role", "ferro.pinned_keys"]
        );
    }

    #[test]
    fn nothing_touched_resets_nothing() {
        assert!(render_reset_config_batch(&[]).is_none());
    }

    #[test]
    fn reset_keys_are_never_interpolated_either() {
        let hostile = "a.b'; DROP TABLE users; --";
        let (sql, binds) =
            render_reset_config_batch(&[hostile.to_string()]).expect("one key renders");
        assert!(!sql.contains("DROP TABLE"));
        assert_eq!(bind_strings(&binds), vec![hostile]);
    }

    #[test]
    fn the_release_guard_keeps_a_clean_connection_and_refuses_a_dirty_one() {
        // The pure half of the pool's `after_release` net. A connection that
        // reached release still advertising a session's keys never went
        // through the close path, so it must not be pooled.
        assert!(!pinned_marker_is_dirty(""));
        assert!(!pinned_marker_is_dirty("   "));
        assert!(pinned_marker_is_dirty("pinch.ledger_id"));
        assert!(pinned_marker_is_dirty(&render_pin_marker(&[
            "pinch.ledger_id".to_string(),
            "app.role".to_string(),
        ])));
    }

    #[test]
    fn a_reset_connection_advertises_nothing() {
        // Closing a session resets the marker along with the keys it names,
        // so the very next release of that connection reads clean. This is
        // why the net never fires on the path that actually happens.
        assert_eq!(render_pin_marker(&[]), "");
        assert!(!pinned_marker_is_dirty(&render_pin_marker(&[])));
    }

    #[test]
    fn values_are_never_interpolated() {
        let hostile = "'); DROP TABLE users; --";
        let (sql, binds) = render_set_config_batch(&[setting("pinch.ledger_id", hostile)], true)
            .expect("one setting renders a statement");
        assert!(!sql.contains("DROP TABLE"));
        assert_eq!(bind_strings(&binds)[1], hostile);
    }

    #[test]
    fn declaration_order_is_preserved() {
        let (_, binds) =
            render_set_config_batch(&[setting("b.two", "2"), setting("a.one", "1")], true)
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
            .live()
            .expect("the wrap's connection is live")
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
            .live()
            .expect("the wrap's connection is live")
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
                .live()
                .expect("the wrap's connection is live")
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
        let scope = OperationScope::unwrapped(&engine, Some(&session_id), None)
            .await
            .expect("scope opens");
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

        let tx: TransactionConnection = Arc::new(ConnectionSlot::new(
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
            .live()
            .expect("the ambient transaction's connection is live")
            .execute_sql("INSERT INTO marker (id) VALUES (1)")
            .await
            .expect("insert");

        // `atomic_required` is set, and it still must not end a transaction it
        // does not own: `transaction()` decides when this commits.
        scope.finish(Ok(())).await.expect("finish");
        tx.lock()
            .await
            .live()
            .expect("the ambient transaction's connection is live")
            .rollback()
            .await
            .expect("rollback");
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

    #[test]
    fn merge_setting_replaces_an_existing_key_in_place() {
        let current = vec![
            setting("pinch.ledger_id", "acme"),
            setting("app.role", "owner"),
        ];
        let merged = merge_setting(&current, "pinch.ledger_id", "globex");
        assert_eq!(
            merged,
            vec![
                setting("pinch.ledger_id", "globex"),
                setting("app.role", "owner")
            ],
            "the existing key keeps its position; only its value changes"
        );
    }

    #[test]
    fn merge_setting_appends_a_new_key_at_the_end() {
        let current = vec![setting("pinch.ledger_id", "acme")];
        let merged = merge_setting(&current, "app.role", "owner");
        assert_eq!(
            merged,
            vec![
                setting("pinch.ledger_id", "acme"),
                setting("app.role", "owner")
            ]
        );
    }

    #[test]
    fn merge_setting_on_empty_current_yields_one_entry() {
        assert_eq!(
            merge_setting(&[], "pinch.ledger_id", "acme"),
            vec![setting("pinch.ledger_id", "acme")]
        );
    }

    #[test]
    fn merge_setting_is_the_basis_for_lost_update_safety() {
        // Two "concurrent" merges computed from the SAME snapshot (simulating
        // what would happen WITHOUT the settings_write_lock serializing the
        // read): each only sees its own key, which is exactly the lost-update
        // bug `operations::set_session_config` avoids by merging inside the
        // lock against a freshly re-read snapshot rather than a caller-suppled
        // one. This pins `merge_setting` itself is a pure, order-preserving
        // single-key update — the concurrency safety property is asserted at
        // the Python level (two sibling tasks, both keys present).
        let base = vec![setting("pinch.ledger_id", "acme")];
        let after_first = merge_setting(&base, "app.role", "owner");
        let after_second = merge_setting(&after_first, "app.request_id", "r-1");
        assert_eq!(
            after_second,
            vec![
                setting("pinch.ledger_id", "acme"),
                setting("app.role", "owner"),
                setting("app.request_id", "r-1"),
            ],
            "sequential merges against the latest snapshot keep every key"
        );
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

    // ----------------------------------------------------------------
    // The pinned connection's lifecycle: what happens to it when nobody
    // released it, and what the pool sees afterwards.
    //
    // Pinning itself is Postgres-only (`settings_would_apply` sees to that),
    // but the guarantee that matters here is backend-agnostic: a connection a
    // session pinned never goes back to the pool unless it was reset first.
    // SQLite proves that just as well, and without a live server.
    // ----------------------------------------------------------------

    #[tokio::test]
    async fn a_pin_records_every_key_it_ever_applied() {
        let (engine, path) = marker_engine().await;
        let pins = Arc::new(AtomicUsize::new(0));
        let conn = engine
            .acquire_pinned_connection()
            .await
            .expect("pool hands out a connection to pin");
        let pin = PinnedConnection::new(conn, vec!["pinch.ledger_id".to_string()], pins.clone());

        assert_eq!(
            pin.reset_keys(),
            vec!["pinch.ledger_id".to_string(), PIN_MARKER_KEY.to_string()],
            "the reset list is the keys applied at pin, plus Ferro's own marker"
        );

        // `set_config` adds one key and re-sends an existing one.
        pin.record_keys(&[
            setting("pinch.ledger_id", "globex"),
            setting("app.role", "auditor"),
        ]);
        assert_eq!(
            pin.reset_keys(),
            vec![
                "pinch.ledger_id".to_string(),
                "app.role".to_string(),
                PIN_MARKER_KEY.to_string(),
            ],
            "the list grows by the new key, in first-applied order, and never duplicates"
        );

        drop(pin);
        let _ = std::fs::remove_file(&path);
    }

    #[tokio::test]
    async fn a_pin_nobody_released_discards_its_connection() {
        // The pool has exactly one connection. If the pin had let it go back
        // in — still carrying a session's settings — the next checkout would
        // be another session running under someone else's scope.
        let (engine, path) = marker_engine().await;
        let pins = Arc::new(AtomicUsize::new(0));

        {
            let conn = engine
                .acquire_pinned_connection()
                .await
                .expect("pool hands out a connection to pin");
            let pin =
                PinnedConnection::new(conn, vec!["pinch.ledger_id".to_string()], pins.clone());
            assert!(pin.is_live());
            assert_eq!(
                pins.load(Ordering::SeqCst),
                1,
                "a live pin is what makes the pool's release hook look at all"
            );
            // Dropped without `release_pinned_connection`: the session was
            // torn down under it, or its close was cancelled.
        }
        tokio::task::yield_now().await;

        assert_eq!(
            pins.load(Ordering::SeqCst),
            0,
            "the gauge falls back to zero, so the release hook goes quiet again"
        );

        // The pool opened a replacement for the connection it lost, so it
        // still serves — the cost of a discard is one connection, never a
        // wedged pool.
        engine
            .execute_sql("INSERT INTO marker (id) VALUES (1)")
            .await
            .expect("the pool replaced the discarded connection");
        let _ = std::fs::remove_file(&path);
    }

    #[tokio::test]
    async fn a_discarded_pin_is_dead_and_its_slot_is_empty() {
        // What a cancelled operation leaves behind. The session's next
        // operation must see a pin it cannot use (and pin a fresh
        // connection); its close must find nothing to reset.
        let (engine, path) = marker_engine().await;
        let pins = Arc::new(AtomicUsize::new(0));
        let conn = engine
            .acquire_pinned_connection()
            .await
            .expect("pool hands out a connection to pin");
        let pin = PinnedConnection::new(conn, vec!["pinch.ledger_id".to_string()], pins.clone());

        pin.discard();
        tokio::task::yield_now().await;

        assert!(!pin.is_live(), "a discarded pin never gets reused");
        assert!(
            pin.slot().lock().await.is_empty(),
            "the connection was taken out of the slot, not left for a later drop to pool"
        );
        assert!(
            pin.slot().lock().await.live().is_err(),
            "and a straggler that tries to use it gets a loud error, not a silent reuse"
        );

        drop(pin);
        let _ = std::fs::remove_file(&path);
    }

    #[tokio::test]
    async fn a_tombstoned_slot_discards_even_when_the_discard_could_not_take_it() {
        // The one path the synchronous discard cannot finish itself: the slot
        // is locked by a statement still in flight, so the connection cannot
        // be taken out here and now. The tombstone is what closes it — every
        // later reader refuses the slot, and the slot's own `Drop` (which has
        // exclusive access and needs no runtime) throws the connection away
        // instead of letting it fall back into the pool.
        let (engine, path) = marker_engine().await;
        let pins = Arc::new(AtomicUsize::new(0));
        let conn = engine
            .acquire_pinned_connection()
            .await
            .expect("pool hands out a connection to pin");
        let pin = PinnedConnection::new(conn, vec!["pinch.ledger_id".to_string()], pins.clone());
        let slot = pin.shared_slot();

        // Hold the slot's lock, exactly as an in-flight statement would.
        let held = slot.lock().await;
        pin.discard();

        assert!(
            slot.is_discarded(),
            "the tombstone must not need the lock — a Drop cannot wait for one"
        );
        assert!(!pin.is_live(), "and the pin is dead the moment it is set");
        drop(held);

        // Even a reader that gets the lock afterwards is refused, so the
        // connection cannot be used while the take is still pending.
        let mut guard = slot.lock().await;
        assert!(guard.live().is_err());
        drop(guard);

        drop(pin);
        drop(slot);
        tokio::task::yield_now().await;

        // The pool of one replaced what it lost rather than handing the
        // tombstoned connection to the next caller.
        engine
            .execute_sql("INSERT INTO marker (id) VALUES (3)")
            .await
            .expect("the pool replaced the discarded connection");
        let _ = std::fs::remove_file(&path);
    }

    #[tokio::test]
    async fn open_transaction_connections_and_execute_dedupe_by_connection() {
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

        // `open_transaction_connections` collects (and dedupes) synchronously,
        // fully independent of `execute_on_connections`'s awaits — pinning
        // that the two are separable is the point of the fix this test
        // guards: the collection step must never hold a `DashMap` shard guard
        // across the execution step's `.await`s.
        let connections = open_transaction_connections(&session);
        assert_eq!(
            connections.len(),
            2,
            "two distinct connections, the shared savepoint collapsed"
        );
        execute_on_connections(&connections, "INSERT INTO marker (id) VALUES (1)", &[])
            .await
            .expect("statement runs on every distinct connection");

        for handle in [&handle_a, &handle_b] {
            let mut guard = handle.conn.lock().await;
            let rows = guard
                .live()
                .expect("the transaction's connection is live")
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
        // parameter-bound) piped into `open_transaction_connections` +
        // `execute_on_connections` (dedup pinned above). What is left to pin
        // here is that the two are actually wired together — settings that
        // render nothing dispatch nothing, so an open transaction that
        // received no statement at all proves the "no batch, no dispatch"
        // wiring without needing a Postgres-only statement to succeed
        // against SQLite.
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
            .live()
            .expect("the transaction's connection is live")
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
