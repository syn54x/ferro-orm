//! The DDL lock timeout (ADR-0044): how every DDL statement ferro executes on
//! Postgres waits for a table lock, and what happens when it gives up.
//!
//! ```text
//! $ ferro migrate up
//! 0003_add_slug  01_expand  waiting for a lock on "author" (attempt 1 of 10, retry in 1s)
//! 0003_add_slug  01_expand  waiting for a lock on "author" (attempt 2 of 10, retry in 2s)
//! 0003_add_slug  01_expand  applied (5214 ms)
//! ```
//!
//! A long report query holds a lock on `author`; `ALTER TABLE "author" …`
//! queues behind it, and while it waits every new query on `author` queues
//! behind the `ALTER`. Under [`DdlExecutor`] the `ALTER` gives up after
//! `ddl_lock_timeout` (Postgres `lock_timeout`, SQLSTATE `55P03`) and the
//! whole step is tried again from its first statement, up to ten attempts,
//! the wait between them doubling from one second and capped at thirty.
//!
//! A caller hands [`DdlExecutor::run`] a statement list and the [`Unit`] it
//! runs as; the executor owns the rest: the connection, the transaction, the
//! lock-timeout statements, the debug line before each statement, the
//! retries, and which statement failed ([`Failed::index`]). Every statement
//! runs unprepared, so no connection keeps a statement prepared against a
//! schema the next migration changes
//! (`docs/solutions/patterns/ddl-on-live-engine.md`).
//!
//! - [`Unit::Transactional`] gets `SET LOCAL lock_timeout` as its
//!   transaction's first statement; a timeout rolls the transaction back.
//! - [`Unit::Unwrapped`] gets `SET lock_timeout` before and
//!   `RESET lock_timeout` after, on its own connection; a timeout re-runs it
//!   from the first statement.
//!
//! A timeout of `None` (`ddl_lock_timeout = "0"`) issues no `SET` and never
//! retries. SQLite has no lock queue of this shape: nothing is set there and
//! nothing is retried. The executor runs the statements it is given and adds
//! only the `SET LOCAL` / `SET` / `RESET` (AGENTS.md § I-1), which the
//! [`Executed`] it returns lists as [`Role::LockTimeout`].
//!
//! Two units stay outside on purpose and return the same [`Executed`]: the
//! SQLite reconciliation pass (`migrate.rs`: statement at a time, each column
//! drop through its index-dependency path) and a SQLite `foreign-keys-off`
//! step (`run.rs`: the pragma, `BEGIN IMMEDIATE`, `foreign_key_check`). They
//! send each statement through [`Executed::send`] / [`Executed::send_on`],
//! so they log, run unprepared and record exactly as the executor does.

use crate::backend::{EngineBindValue, EngineConnection, EngineHandle};
use ferro_ddl_lowering::Dialect;
use pyo3::prelude::*;
use std::time::Duration;

/// The configuration key the timeout is read from, named by every message.
pub const SETTING: &str = "ddl_lock_timeout";

/// How many times a unit is tried before it fails.
pub const MAX_ATTEMPTS: u8 = 10;

/// Test-only: caps the backoff at this many milliseconds, so a test can
/// exhaust ten attempts without waiting minutes. Read each time an executor
/// is built; never set it in production.
pub const BACKOFF_CAP_ENV: &str = "FERRO_TEST_BACKOFF_CAP_MS";

/// Postgres `lock_not_available`: what a statement that outlived
/// `lock_timeout` fails with.
const LOCK_NOT_AVAILABLE: &str = "55P03";

/// The wait between attempts: `initial`, doubling after each attempt, never
/// more than `cap`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Backoff {
    pub initial: Duration,
    pub cap: Duration,
}

impl Backoff {
    /// One second, doubling, capped at thirty (ADR-0044).
    pub const DEFAULT: Backoff = Backoff {
        initial: Duration::from_secs(1),
        cap: Duration::from_secs(30),
    };

    /// The wait after attempt `attempt` (1-based) failed.
    pub fn after(&self, attempt: u8) -> Duration {
        let doublings = u32::from(attempt.saturating_sub(1)).min(31);
        self.initial.saturating_mul(1u32 << doublings).min(self.cap)
    }
}

/// One attempt that timed out waiting for a lock, reported before the wait
/// that follows it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Attempt {
    /// The attempt that just timed out (1-based).
    pub number: u8,
    /// How many attempts the unit gets in all.
    pub of: u8,
    /// How long until the next one.
    pub retry_in: Duration,
    /// The table the failing statement targets, when its text names one
    /// (Postgres' `lock_timeout` error does not say which lock it waited on).
    pub table: Option<String>,
}

impl Attempt {
    /// `waiting for a lock on "author" (attempt 2 of 10, retry in 2s)`.
    pub fn describe(&self) -> String {
        let on = match &self.table {
            Some(table) => format!(" on \"{table}\""),
            None => String::new(),
        };
        format!(
            "waiting for a lock{on} (attempt {} of {}, retry in {})",
            self.number,
            self.of,
            crate::run::show_duration(self.retry_in)
        )
    }
}

/// A unit that timed out on every attempt.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DdlLockTimeout {
    /// How many attempts were made.
    pub attempts: u8,
    /// The configuration key to raise: always [`SETTING`].
    pub setting: &'static str,
    /// The timeout each attempt waited.
    pub timeout: Duration,
    /// The table the last failing statement targets, when known.
    pub table: Option<String>,
}

impl std::fmt::Display for DdlLockTimeout {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let what = match &self.table {
            Some(table) => format!("lock on \"{table}\""),
            None => "a table lock".to_string(),
        };
        write!(
            f,
            "{what} not acquired within {} after {} attempts; set {} under [tool.ferro] \
             or run when the table is quieter",
            crate::run::show_duration(self.timeout),
            self.attempts,
            self.setting
        )
    }
}

/// How one attempt failed.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AttemptOutcome {
    /// It gave up waiting for a lock (SQLSTATE `55P03`).
    LockTimeout,
    /// Anything else: never retried.
    Failed,
}

/// What follows a failed attempt.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Next {
    /// Wait this long, then try again from the first statement.
    Retry(Duration),
    /// Stop: the failure stands.
    Fail,
}

/// How a unit runs.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Unit {
    /// One transaction: every statement commits together or none does.
    Transactional,
    /// Autocommit on one connection: each statement commits as it runs
    /// (what `CREATE INDEX CONCURRENTLY` and an enum label need).
    Unwrapped,
}

/// Whose statements a unit runs: the subject of every statement, and the
/// voice of the debug line logged before it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Door<'a> {
    /// The create or reconciliation pass, on this table or enum type.
    Pass(&'a str),
    /// `ferro migrate`, running this step (`0003_add_slug/01_expand.up.sql`).
    Run(&'a str),
}

impl Door<'_> {
    fn statement(self, role: Role, sql: &str) -> Statement {
        let (Door::Pass(subject) | Door::Run(subject)) = self;
        Statement {
            subject: subject.to_string(),
            sql: sql.to_string(),
            role,
        }
    }

    /// The line logged before `statement` runs.
    fn log(self, statement: &Statement) {
        let (subject, sql) = (statement.subject.as_str(), statement.sql.as_str());
        match (self, statement.role) {
            (Door::Pass(_), Role::Schema) => crate::migrate::log_reconcile_statement(subject, sql),
            (Door::Pass(_), Role::LockTimeout) => {
                crate::migrate::log_lock_timeout_statement(subject, sql)
            }
            (Door::Run(_), _) => crate::run::log_step_statement(subject, sql),
        }
    }
}

/// What a statement the executor sent was for.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Role {
    /// The caller's own statement.
    Schema,
    /// A `SET LOCAL` / `SET` / `RESET lock_timeout` the executor added
    /// (ADR-0044).
    LockTimeout,
}

/// One statement sent to the database.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Statement {
    /// The table, enum type or step file it belongs to.
    pub subject: String,
    pub sql: String,
    pub role: Role,
}

/// Every statement a unit sent and the database accepted, in order, the
/// lock-timeout statements included: those of the attempt that succeeded,
/// never of one that was rolled back and retried.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Executed {
    pub statements: Vec<Statement>,
}

impl Executed {
    /// Log `sql` as `door`'s, run it unprepared on `conn`, and record it once
    /// it succeeded.
    ///
    /// # Errors
    /// The database error; nothing is recorded.
    pub async fn send(
        &mut self,
        conn: &mut EngineConnection,
        door: Door<'_>,
        role: Role,
        sql: &str,
    ) -> Result<u64, sqlx::Error> {
        let statement = door.statement(role, sql);
        door.log(&statement);
        let affected = conn.execute_sql_unprepared(sql).await?;
        self.statements.push(statement);
        Ok(affected)
    }

    /// [`Self::send`] on a connection out of `engine`'s pool, given back
    /// afterwards: for a unit that reads the catalog between statements.
    ///
    /// # Errors
    /// The pool's or the database's error; nothing is recorded.
    pub async fn send_on(
        &mut self,
        engine: &EngineHandle,
        door: Door<'_>,
        role: Role,
        sql: &str,
    ) -> Result<u64, sqlx::Error> {
        let statement = door.statement(role, sql);
        door.log(&statement);
        let affected = engine.execute_sql_unprepared(sql).await?;
        self.statements.push(statement);
        Ok(affected)
    }
}

/// A unit's failure that is not a lock timeout: the database error and,
/// when one of the caller's statements raised it, that statement's position
/// in the caller's list. `None` is the executor's own work failing: the
/// connection, `BEGIN`, a lock-timeout statement, the settle, `COMMIT`.
#[derive(Debug)]
pub struct Failed {
    pub index: Option<usize>,
    pub error: sqlx::Error,
}

impl Failed {
    fn outside(error: sqlx::Error) -> Self {
        Self { index: None, error }
    }
}

/// The last statement of a unit, run after the caller's statements on the
/// same connection (inside the transaction, for a transactional unit) and
/// rendered only then: a migration step's record write, which carries the
/// time the step took. It is the record of the work, not the work, so it is
/// neither logged nor listed in [`Executed`].
pub struct Settle<'a>(Box<dyn FnMut() -> (String, Vec<EngineBindValue>) + Send + 'a>);

impl<'a> Settle<'a> {
    pub fn new(render: impl FnMut() -> (String, Vec<EngineBindValue>) + Send + 'a) -> Self {
        Self(Box::new(render))
    }

    async fn run(&mut self, conn: &mut EngineConnection) -> Result<(), sqlx::Error> {
        let (sql, binds) = (self.0)();
        conn.fetch_all_sql_unprepared_with_binds(&sql, &binds)
            .await
            .map(|_| ())
    }
}

/// How a unit run under the executor failed.
#[derive(Debug)]
pub enum DdlError<E> {
    /// Every attempt timed out waiting for a lock.
    LockTimeout(DdlLockTimeout),
    /// A failure that is not a lock timeout, or any failure with the
    /// timeout disabled.
    Failed(E),
}

/// Whether `err` is Postgres giving up on a lock (`55P03`).
pub fn is_lock_timeout(err: &sqlx::Error) -> bool {
    match err {
        sqlx::Error::Database(db) => db.code().as_deref() == Some(LOCK_NOT_AVAILABLE),
        _ => false,
    }
}

/// The `SET LOCAL` a transactional unit starts with.
pub fn set_local_sql(timeout: Duration) -> String {
    format!("SET LOCAL lock_timeout = '{}ms'", whole_ms(timeout))
}

/// The `SET` a no-transaction unit starts with.
pub fn set_session_sql(timeout: Duration) -> String {
    format!("SET lock_timeout = '{}ms'", whole_ms(timeout))
}

/// The `RESET` a no-transaction unit ends with.
pub const RESET_SQL: &str = "RESET lock_timeout";

/// Milliseconds, rounded up: a configured sub-millisecond wait must never
/// become `0`, which Postgres reads as "wait forever".
fn whole_ms(timeout: Duration) -> u128 {
    let ms = timeout.as_millis();
    if Duration::from_millis(u64::try_from(ms).unwrap_or(u64::MAX)) < timeout {
        ms + 1
    } else {
        ms.max(1)
    }
}

/// The table a DDL statement names, for the attempt line: the target of
/// `ALTER TABLE`, `DROP TABLE`, `LOCK`, `TRUNCATE`, the first `REFERENCES`
/// table of `CREATE TABLE`, and the `ON` table of `CREATE INDEX`, triggers
/// and policies. `None` for anything else.
pub fn lock_target(statement: &str) -> Option<String> {
    let words = words(statement);
    let at = |i: usize| words.get(i).map(String::as_str).unwrap_or("");
    let is = |i: usize, keyword: &str| at(i).eq_ignore_ascii_case(keyword);
    // The name after `start`, skipping the optional `IF [NOT] EXISTS` / `ONLY`.
    let name_from = |mut i: usize| -> Option<String> {
        loop {
            if is(i, "IF") && is(i + 1, "EXISTS") {
                i += 2;
            } else if is(i, "IF") && is(i + 1, "NOT") && is(i + 2, "EXISTS") {
                i += 3;
            } else if is(i, "ONLY") {
                i += 1;
            } else {
                break;
            }
        }
        identifier(words.get(i)?)
    };
    if (is(0, "ALTER") || is(0, "DROP")) && is(1, "TABLE") {
        return name_from(2);
    }
    if is(0, "LOCK") || is(0, "TRUNCATE") {
        return name_from(if is(1, "TABLE") { 2 } else { 1 });
    }
    // A new table locks nothing that exists but the tables it references.
    if is(0, "CREATE") && is(1, "TABLE") {
        let references = (2..words.len()).find(|&i| is(i, "REFERENCES"))?;
        return name_from(references + 1);
    }
    if is(0, "COMMENT") && is(1, "ON") && is(2, "TABLE") {
        return name_from(3);
    }
    // `CREATE [UNIQUE] INDEX`, `CREATE [OR REPLACE] [CONSTRAINT] TRIGGER`,
    // `… POLICY` / `… RULE`: the table follows the first `ON`.
    let mut object = 1;
    while ["UNIQUE", "OR", "REPLACE", "CONSTRAINT"]
        .iter()
        .any(|word| is(object, word))
    {
        object += 1;
    }
    let on_a_table = ["INDEX", "TRIGGER", "POLICY", "RULE"]
        .iter()
        .any(|word| is(object, word));
    if (is(0, "CREATE") || is(0, "ALTER") || is(0, "DROP")) && on_a_table {
        let on = (object..words.len()).find(|&i| is(i, "ON"))?;
        return name_from(on + 1);
    }
    None
}

/// `statement` as words: whitespace-separated, with `(`, `)`, `,` and `;`
/// their own words, a double-quoted identifier kept whole, and leading `--`
/// comment lines skipped.
fn words(statement: &str) -> Vec<String> {
    let mut words = Vec::new();
    let mut current = String::new();
    let mut chars = statement.chars().peekable();
    let flush = |current: &mut String, words: &mut Vec<String>| {
        if !current.is_empty() {
            words.push(std::mem::take(current));
        }
    };
    while let Some(c) = chars.next() {
        match c {
            '"' => {
                current.push('"');
                while let Some(q) = chars.next() {
                    current.push(q);
                    if q == '"' {
                        if chars.peek() == Some(&'"') {
                            current.push('"');
                            chars.next();
                        } else {
                            break;
                        }
                    }
                }
            }
            '-' if current.is_empty() && chars.peek() == Some(&'-') => {
                for skipped in chars.by_ref() {
                    if skipped == '\n' {
                        break;
                    }
                }
            }
            '(' | ')' | ',' | ';' => {
                flush(&mut current, &mut words);
                words.push(c.to_string());
            }
            c if c.is_whitespace() => flush(&mut current, &mut words),
            c => current.push(c),
        }
    }
    flush(&mut current, &mut words);
    words
}

/// A word read as a (possibly schema-qualified) identifier, unquoted:
/// `"public"."author"` is `public.author`. `None` for punctuation.
fn identifier(word: &str) -> Option<String> {
    if matches!(word, "(" | ")" | "," | ";") {
        return None;
    }
    let mut parts = Vec::new();
    let mut part = String::new();
    let mut chars = word.chars().peekable();
    while let Some(c) = chars.next() {
        match c {
            '"' => {
                while let Some(q) = chars.next() {
                    if q == '"' {
                        if chars.peek() == Some(&'"') {
                            part.push('"');
                            chars.next();
                        } else {
                            break;
                        }
                    } else {
                        part.push(q);
                    }
                }
            }
            '.' => parts.push(std::mem::take(&mut part)),
            c => part.push(c.to_ascii_lowercase()),
        }
    }
    parts.push(part);
    Some(parts.join("."))
}

/// A connection out of `engine`'s pool, outside any transaction.
///
/// # Errors
/// The pool's error; `PoolClosed` for an engine with no pool.
pub async fn pool_connection(engine: &EngineHandle) -> Result<EngineConnection, sqlx::Error> {
    if let Some(pool) = engine.sqlite_pool() {
        return Ok(EngineConnection::Sqlite(pool.acquire().await?));
    }
    match engine.postgres_pool() {
        Some(pool) => Ok(EngineConnection::Postgres(pool.acquire().await?)),
        None => Err(sqlx::Error::PoolClosed),
    }
}

/// The DDL lock-timeout policy (ADR-0044): the timeout each attempt waits
/// for a lock, how many attempts, and the backoff between them.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DdlExecutor {
    /// `None` disables both the timeout and the retry.
    pub timeout: Option<Duration>,
    pub max_attempts: u8,
    pub backoff: Backoff,
}

impl DdlExecutor {
    /// The policy for `timeout`: ten attempts, one second doubling to thirty
    /// (capped lower by [`BACKOFF_CAP_ENV`] in tests).
    pub fn new(timeout: Option<Duration>) -> Self {
        let mut backoff = Backoff::DEFAULT;
        if let Some(cap) = std::env::var(BACKOFF_CAP_ENV)
            .ok()
            .and_then(|ms| ms.trim().parse::<u64>().ok())
        {
            backoff.cap = backoff.cap.min(Duration::from_millis(cap));
        }
        Self {
            timeout: timeout.filter(|t| !t.is_zero()),
            max_attempts: MAX_ATTEMPTS,
            backoff,
        }
    }

    /// The policy for `ddl_lock_timeout` in seconds, as Python passes it
    /// (`0` disables).
    ///
    /// # Errors
    /// `ValueError` for a negative or non-finite number.
    pub fn from_seconds(seconds: f64) -> PyResult<Self> {
        if !(seconds.is_finite() && seconds >= 0.0) {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "{SETTING} must be a non-negative number of seconds; got {seconds}"
            )));
        }
        Ok(Self::new(Some(Duration::from_secs_f64(seconds))))
    }

    /// The timeout to set on `dialect`: none on SQLite.
    fn timeout_on(&self, dialect: Dialect) -> Option<Duration> {
        match dialect {
            Dialect::Postgres => self.timeout,
            Dialect::Sqlite => None,
        }
    }

    /// The decision after attempt `attempt` (1-based) failed with `outcome`:
    /// a lock timeout is retried after the backoff until `max_attempts`;
    /// anything else, and anything with the timeout disabled, fails at once.
    pub fn next_attempt(&self, attempt: u8, outcome: AttemptOutcome) -> Next {
        match outcome {
            AttemptOutcome::LockTimeout
                if self.timeout.is_some() && attempt < self.max_attempts =>
            {
                Next::Retry(self.backoff.after(attempt))
            }
            _ => Next::Fail,
        }
    }

    /// Run `statements` as one `unit` of `door`'s, then `settle` on the same
    /// connection, under the lock timeout: on Postgres with a timeout set, a
    /// statement that waits longer than it for a lock fails the attempt, and
    /// the unit runs again from its first statement after the backoff
    /// (`on_attempt` hears each such attempt, before the wait), up to
    /// `max_attempts`. Each statement is logged as `door`'s before it runs,
    /// and runs unprepared.
    ///
    /// A transactional unit commits when every statement and the settle
    /// succeeded and rolls back otherwise; a connection whose `ROLLBACK`
    /// failed is closed rather than returned to the pool (#416). An unwrapped
    /// unit's `RESET lock_timeout` runs whether or not the unit failed, and a
    /// `RESET` that fails closes the connection instead of returning it with
    /// the setting on it.
    ///
    /// # Errors
    /// [`DdlError::LockTimeout`] after the last attempt; otherwise the first
    /// failure, as [`Failed`].
    pub async fn run<S: AsRef<str>>(
        &self,
        engine: &EngineHandle,
        unit: Unit,
        door: Door<'_>,
        statements: &[S],
        on_attempt: impl FnMut(Attempt),
        settle: Option<Settle<'_>>,
    ) -> Result<Executed, DdlError<Failed>> {
        let timeout = self.timeout_on(engine.backend());
        let mut live = LiveUnit {
            engine,
            unit,
            door,
            statements,
            timeout,
            settle,
        };
        self.attempts(timeout, statements, on_attempt, &mut live)
            .await
    }

    /// The retry loop: one `unit` attempt after another until one succeeds,
    /// fails with anything but a lock timeout, or is the last.
    async fn attempts<S: AsRef<str>>(
        &self,
        timeout: Option<Duration>,
        statements: &[S],
        mut on_attempt: impl FnMut(Attempt),
        unit: &mut impl AttemptUnit,
    ) -> Result<Executed, DdlError<Failed>> {
        let mut attempt: u8 = 1;
        loop {
            let mut executed = Executed::default();
            let result = unit.attempt(&mut executed).await;
            match self.judge(attempt, timeout, statements, result, &mut on_attempt) {
                Judged::Done(result) => return result.map(|()| executed),
                Judged::RetryAfter(wait) => tokio::time::sleep(wait).await,
            }
            attempt = attempt.saturating_add(1);
        }
    }

    fn judge<S: AsRef<str>>(
        &self,
        attempt: u8,
        timeout: Option<Duration>,
        statements: &[S],
        result: Result<(), Failed>,
        on_attempt: &mut impl FnMut(Attempt),
    ) -> Judged {
        let failed = match result {
            Ok(()) => return Judged::Done(Ok(())),
            Err(failed) => failed,
        };
        let timed_out = is_lock_timeout(&failed.error);
        let outcome = if timed_out {
            AttemptOutcome::LockTimeout
        } else {
            AttemptOutcome::Failed
        };
        let table = || {
            failed
                .index
                .and_then(|index| statements.get(index))
                .and_then(|statement| lock_target(statement.as_ref()))
        };
        match (self.next_attempt(attempt, outcome), timeout) {
            (Next::Retry(wait), _) => {
                on_attempt(Attempt {
                    number: attempt,
                    of: self.max_attempts,
                    retry_in: wait,
                    table: table(),
                });
                Judged::RetryAfter(wait)
            }
            (Next::Fail, Some(timeout)) if timed_out => {
                Judged::Done(Err(DdlError::LockTimeout(DdlLockTimeout {
                    attempts: attempt,
                    setting: SETTING,
                    timeout,
                    table: table(),
                })))
            }
            (Next::Fail, _) => Judged::Done(Err(DdlError::Failed(failed))),
        }
    }
}

enum Judged {
    Done(Result<(), DdlError<Failed>>),
    RetryAfter(Duration),
}

/// One attempt of a unit, recording what it sent in `executed`. The live
/// unit is [`LiveUnit`]; the retry loop's tests drive a scripted one.
trait AttemptUnit {
    async fn attempt(&mut self, executed: &mut Executed) -> Result<(), Failed>;
}

/// A unit against the database: [`DdlExecutor::run`]'s arguments.
struct LiveUnit<'a, S> {
    engine: &'a EngineHandle,
    unit: Unit,
    door: Door<'a>,
    statements: &'a [S],
    timeout: Option<Duration>,
    settle: Option<Settle<'a>>,
}

impl<S: AsRef<str>> LiveUnit<'_, S> {
    /// The caller's statements, then the settle.
    async fn body(
        &mut self,
        conn: &mut EngineConnection,
        executed: &mut Executed,
    ) -> Result<(), Failed> {
        for (index, sql) in self.statements.iter().enumerate() {
            executed
                .send(conn, self.door, Role::Schema, sql.as_ref())
                .await
                .map_err(|error| Failed {
                    index: Some(index),
                    error,
                })?;
        }
        if let Some(settle) = &mut self.settle {
            settle.run(conn).await.map_err(Failed::outside)?;
        }
        Ok(())
    }
}

impl<S: AsRef<str>> AttemptUnit for LiveUnit<'_, S> {
    async fn attempt(&mut self, executed: &mut Executed) -> Result<(), Failed> {
        let door = self.door;
        match self.unit {
            Unit::Transactional => {
                let mut conn = self
                    .engine
                    .begin_transaction_connection()
                    .await
                    .map_err(Failed::outside)?;
                let result = async {
                    if let Some(timeout) = self.timeout {
                        executed
                            .send(&mut conn, door, Role::LockTimeout, &set_local_sql(timeout))
                            .await
                            .map_err(Failed::outside)?;
                    }
                    self.body(&mut conn, executed).await?;
                    conn.commit().await.map_err(Failed::outside)
                }
                .await;
                if result.is_err() && conn.rollback().await.is_err() {
                    let _ = conn.detach_and_close().await;
                }
                result
            }
            Unit::Unwrapped => {
                let mut conn = pool_connection(self.engine)
                    .await
                    .map_err(Failed::outside)?;
                if let Some(timeout) = self.timeout {
                    executed
                        .send(
                            &mut conn,
                            door,
                            Role::LockTimeout,
                            &set_session_sql(timeout),
                        )
                        .await
                        .map_err(Failed::outside)?;
                }
                let result = self.body(&mut conn, executed).await;
                if self.timeout.is_some()
                    && executed
                        .send(&mut conn, door, Role::LockTimeout, RESET_SQL)
                        .await
                        .is_err()
                {
                    let _ = conn.detach_and_close().await;
                }
                result
            }
        }
    }
}

/// `ferro.exceptions.OperationalError(message)` with SQLSTATE `55P03`.
pub fn lock_timeout_error(message: &str) -> PyErr {
    Python::attach(|py| {
        let built = (|| -> PyResult<PyErr> {
            let class = py.import("ferro.exceptions")?.getattr("OperationalError")?;
            let kwargs = pyo3::types::PyDict::new(py);
            kwargs.set_item("sqlstate", LOCK_NOT_AVAILABLE)?;
            Ok(PyErr::from_value(class.call((message,), Some(&kwargs))?))
        })();
        built.unwrap_or_else(|lookup| lookup)
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn executor(timeout_ms: Option<u64>) -> DdlExecutor {
        DdlExecutor {
            timeout: timeout_ms.map(Duration::from_millis),
            max_attempts: MAX_ATTEMPTS,
            backoff: Backoff::DEFAULT,
        }
    }

    #[test]
    fn a_lock_timeout_is_retried_with_the_wait_doubling_from_one_second_to_thirty() {
        let ddl = executor(Some(5000));
        let waits: Vec<Next> = (1..=9)
            .map(|n| ddl.next_attempt(n, AttemptOutcome::LockTimeout))
            .collect();
        let secs = |s| Next::Retry(Duration::from_secs(s));
        assert_eq!(
            waits,
            vec![
                secs(1),
                secs(2),
                secs(4),
                secs(8),
                secs(16),
                secs(30),
                secs(30),
                secs(30),
                secs(30)
            ]
        );
    }

    #[test]
    fn the_tenth_timeout_fails_and_so_does_any_other_failure() {
        let ddl = executor(Some(5000));
        assert_eq!(
            ddl.next_attempt(10, AttemptOutcome::LockTimeout),
            Next::Fail
        );
        assert_eq!(ddl.next_attempt(1, AttemptOutcome::Failed), Next::Fail);
    }

    #[test]
    fn a_disabled_timeout_never_retries() {
        let ddl = executor(None);
        assert_eq!(ddl.next_attempt(1, AttemptOutcome::LockTimeout), Next::Fail);
        assert_eq!(DdlExecutor::new(Some(Duration::ZERO)).timeout, None);
        assert_eq!(
            DdlExecutor::from_seconds(0.0).map(|d| d.timeout).ok(),
            Some(None)
        );
        assert!(DdlExecutor::from_seconds(-1.0).is_err());
        assert!(DdlExecutor::from_seconds(f64::NAN).is_err());
    }

    #[test]
    fn the_backoff_cap_bounds_every_wait() {
        let capped = Backoff {
            initial: Duration::from_secs(1),
            cap: Duration::from_millis(50),
        };
        assert_eq!(capped.after(1), Duration::from_millis(50));
        assert_eq!(Backoff::DEFAULT.after(255), Duration::from_secs(30));
    }

    #[test]
    fn the_set_statements_carry_whole_milliseconds_and_never_zero() {
        assert_eq!(
            set_local_sql(Duration::from_secs(5)),
            "SET LOCAL lock_timeout = '5000ms'"
        );
        assert_eq!(
            set_session_sql(Duration::from_millis(100)),
            "SET lock_timeout = '100ms'"
        );
        assert_eq!(
            set_local_sql(Duration::from_micros(500)),
            "SET LOCAL lock_timeout = '1ms'"
        );
        assert_eq!(
            set_local_sql(Duration::from_micros(1500)),
            "SET LOCAL lock_timeout = '2ms'"
        );
        assert_eq!(RESET_SQL, "RESET lock_timeout");
    }

    #[test]
    fn the_attempt_line_names_the_table_the_attempt_and_the_wait() {
        let attempt = Attempt {
            number: 2,
            of: 10,
            retry_in: Duration::from_secs(2),
            table: Some("author".to_string()),
        };
        assert_eq!(
            attempt.describe(),
            "waiting for a lock on \"author\" (attempt 2 of 10, retry in 2s)"
        );
        let unnamed = Attempt {
            number: 1,
            of: 10,
            retry_in: Duration::from_millis(50),
            table: None,
        };
        assert_eq!(
            unnamed.describe(),
            "waiting for a lock (attempt 1 of 10, retry in 50ms)"
        );
    }

    #[test]
    fn the_exhausted_message_names_the_setting_the_wait_and_the_attempts() {
        let timeout = DdlLockTimeout {
            attempts: 10,
            setting: SETTING,
            timeout: Duration::from_secs(5),
            table: Some("post".to_string()),
        };
        assert_eq!(
            timeout.to_string(),
            "lock on \"post\" not acquired within 5s after 10 attempts; set \
             ddl_lock_timeout under [tool.ferro] or run when the table is quieter"
        );
    }

    #[test]
    fn the_lock_target_is_the_table_the_statement_names() {
        let cases = [
            (
                "ALTER TABLE \"author\" ADD COLUMN \"slug\" TEXT",
                Some("author"),
            ),
            (
                "alter table if exists only author drop column x",
                Some("author"),
            ),
            (
                "-- ferro: x\nALTER TABLE \"s\".\"Author\" ADD x int",
                Some("s.Author"),
            ),
            ("DROP TABLE IF EXISTS \"post\"", Some("post")),
            (
                "LOCK TABLE \"author\" IN ACCESS EXCLUSIVE MODE",
                Some("author"),
            ),
            ("TRUNCATE author", Some("author")),
            (
                "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS \"uq\" ON ONLY \"post\" (\"slug\")",
                Some("post"),
            ),
            ("CREATE INDEX ON \"post\"(\"slug\")", Some("post")),
            (
                "CREATE POLICY \"rls_post_owner\" ON \"post\" USING (true)",
                Some("post"),
            ),
            ("DROP POLICY IF EXISTS \"p\" ON \"post\"", Some("post")),
            ("COMMENT ON TABLE \"post\" IS 'x'", Some("post")),
            ("COMMENT ON COLUMN \"post\".\"x\" IS 'x'", None),
            ("ALTER TYPE \"status\" ADD VALUE IF NOT EXISTS 'x'", None),
            (
                "CREATE TABLE \"post\" (\"a\" INT REFERENCES \"author\" ON DELETE CASCADE)",
                Some("author"),
            ),
            ("CREATE TABLE \"post\" (\"a\" INT)", None),
            ("DROP INDEX CONCURRENTLY IF EXISTS \"idx_a\"", None),
            ("INSERT INTO \"author\" VALUES (1)", None),
            ("", None),
        ];
        for (statement, expected) in cases {
            assert_eq!(lock_target(statement).as_deref(), expected, "{statement}");
        }
    }

    /// What Postgres raises when a statement outlives `lock_timeout`.
    #[derive(Debug)]
    struct LockNotAvailable;

    impl std::fmt::Display for LockNotAvailable {
        fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
            f.write_str("canceling statement due to lock timeout")
        }
    }

    impl std::error::Error for LockNotAvailable {}

    impl sqlx::error::DatabaseError for LockNotAvailable {
        fn message(&self) -> &str {
            "canceling statement due to lock timeout"
        }
        fn code(&self) -> Option<std::borrow::Cow<'_, str>> {
            Some(LOCK_NOT_AVAILABLE.into())
        }
        fn as_error(&self) -> &(dyn std::error::Error + Send + Sync + 'static) {
            self
        }
        fn as_error_mut(&mut self) -> &mut (dyn std::error::Error + Send + Sync + 'static) {
            self
        }
        fn into_error(self: Box<Self>) -> Box<dyn std::error::Error + Send + Sync + 'static> {
            self
        }
        fn kind(&self) -> sqlx::error::ErrorKind {
            sqlx::error::ErrorKind::Other
        }
    }

    /// A unit whose attempts fail as scripted, in order, at a statement of
    /// `statements`; an attempt past the script succeeds. Each attempt
    /// records the statements it got through.
    struct Scripted<'a> {
        statements: &'a [&'a str],
        script: Vec<(usize, sqlx::Error)>,
        attempts: usize,
    }

    impl AttemptUnit for Scripted<'_> {
        async fn attempt(&mut self, executed: &mut Executed) -> Result<(), Failed> {
            self.attempts += 1;
            let failure = (!self.script.is_empty()).then(|| self.script.remove(0));
            let stop = failure
                .as_ref()
                .map_or(self.statements.len(), |(at, _)| *at);
            for sql in &self.statements[..stop] {
                executed
                    .statements
                    .push(Door::Pass("author").statement(Role::Schema, sql));
            }
            match failure {
                Some((index, error)) => Err(Failed {
                    index: Some(index),
                    error,
                }),
                None => Ok(()),
            }
        }
    }

    const UNIT: [&str; 2] = [
        "UPDATE \"author\" SET \"slug\" = 'x'",
        "ALTER TABLE \"author\" ADD COLUMN \"slug\" TEXT",
    ];

    fn quick(max_attempts: u8) -> DdlExecutor {
        DdlExecutor {
            timeout: Some(Duration::from_millis(5000)),
            max_attempts,
            backoff: Backoff {
                initial: Duration::from_millis(1),
                cap: Duration::from_millis(1),
            },
        }
    }

    fn timed_out() -> sqlx::Error {
        sqlx::Error::Database(Box::new(LockNotAvailable))
    }

    #[tokio::test]
    async fn a_unit_that_times_out_runs_again_from_its_first_statement() {
        let ddl = quick(10);
        let mut unit = Scripted {
            statements: &UNIT,
            script: vec![(1, timed_out()), (1, timed_out())],
            attempts: 0,
        };
        let mut heard = Vec::new();
        let executed = ddl
            .attempts(ddl.timeout, &UNIT, |attempt| heard.push(attempt), &mut unit)
            .await
            .expect("the third attempt succeeds");

        assert_eq!(unit.attempts, 3);
        let lines: Vec<String> = heard.iter().map(Attempt::describe).collect();
        assert_eq!(
            lines,
            [
                "waiting for a lock on \"author\" (attempt 1 of 10, retry in 1ms)",
                "waiting for a lock on \"author\" (attempt 2 of 10, retry in 1ms)",
            ]
        );
        // Only the attempt that succeeded is reported, once each.
        let sent: Vec<&str> = executed.statements.iter().map(|s| s.sql.as_str()).collect();
        assert_eq!(sent, UNIT);
    }

    #[tokio::test]
    async fn the_last_timeout_names_the_table_of_the_failing_statement() {
        let ddl = quick(3);
        let mut unit = Scripted {
            statements: &UNIT,
            script: (0..3).map(|_| (1, timed_out())).collect(),
            attempts: 0,
        };
        let mut heard = 0;
        let err = ddl
            .attempts(ddl.timeout, &UNIT, |_| heard += 1, &mut unit)
            .await
            .expect_err("every attempt timed out");

        assert_eq!((unit.attempts, heard), (3, 2));
        let DdlError::LockTimeout(timeout) = err else {
            panic!("a lock timeout, not {err:?}");
        };
        assert_eq!(timeout.attempts, 3);
        assert_eq!(timeout.table.as_deref(), Some("author"));
    }

    #[tokio::test]
    async fn any_other_failure_stops_at_once_naming_its_statement() {
        let ddl = quick(10);
        let mut unit = Scripted {
            statements: &UNIT,
            script: vec![(1, sqlx::Error::RowNotFound)],
            attempts: 0,
        };
        let err = ddl
            .attempts(ddl.timeout, &UNIT, |_| panic!("not retried"), &mut unit)
            .await
            .expect_err("the failure stands");

        assert_eq!(unit.attempts, 1);
        assert!(matches!(
            err,
            DdlError::Failed(Failed { index: Some(1), .. })
        ));
    }

    #[tokio::test]
    async fn with_the_timeout_disabled_a_timeout_is_not_retried() {
        let ddl = DdlExecutor {
            timeout: None,
            ..quick(10)
        };
        let mut unit = Scripted {
            statements: &UNIT,
            script: vec![(0, timed_out())],
            attempts: 0,
        };
        let err = ddl
            .attempts(None, &UNIT, |_| panic!("not retried"), &mut unit)
            .await
            .expect_err("the failure stands");
        assert_eq!(unit.attempts, 1);
        assert!(matches!(
            err,
            DdlError::Failed(Failed { index: Some(0), .. })
        ));
    }

    mod on_sqlite {
        use super::super::*;
        use crate::backend::PoolSpec;
        use crate::session_settings::SettingsDelivery;
        use std::sync::Arc;
        use std::sync::atomic::AtomicUsize;

        async fn engine() -> EngineHandle {
            EngineHandle::connect(PoolSpec {
                backend: Dialect::Sqlite,
                url: "sqlite::memory:".to_string(),
                search_path: None,
                max_connections: 1,
                min_connections: 0,
                settings_delivery: SettingsDelivery::Transaction,
                pins: Arc::new(AtomicUsize::new(0)),
            })
            .await
            .expect("in-memory engine")
        }

        async fn count(engine: &EngineHandle, table: &str) -> usize {
            engine
                .fetch_all_sql_unprepared(&format!("SELECT * FROM {table}"))
                .await
                .map(|rows| rows.len())
                .unwrap_or(usize::MAX)
        }

        #[tokio::test]
        async fn a_transactional_unit_reports_what_ran_and_settles_inside_it() {
            let engine = engine().await;
            let ddl = DdlExecutor::new(Some(Duration::from_secs(5)));
            let statements = [
                "CREATE TABLE \"author\" (\"id\" INTEGER PRIMARY KEY)",
                "CREATE TABLE \"note\" (\"text\" TEXT)",
            ];
            let mut settled = 0;
            let executed = ddl
                .run(
                    &engine,
                    Unit::Transactional,
                    Door::Run("0001_init/01_tables.up.sql"),
                    &statements,
                    |_| {},
                    Some(Settle::new(|| {
                        settled += 1;
                        (
                            "INSERT INTO \"note\" VALUES ('settled')".to_string(),
                            vec![],
                        )
                    })),
                )
                .await
                .expect("ran");

            // SQLite sets no lock timeout, so only the statements ran; the
            // settle is not reported.
            assert_eq!(
                executed.statements,
                statements
                    .iter()
                    .map(|sql| Statement {
                        subject: "0001_init/01_tables.up.sql".to_string(),
                        sql: (*sql).to_string(),
                        role: Role::Schema,
                    })
                    .collect::<Vec<_>>()
            );
            assert_eq!(settled, 1);
            assert_eq!(count(&engine, "note").await, 1);
        }

        #[tokio::test]
        async fn a_failing_statement_is_named_by_its_place_and_the_unit_rolls_back() {
            let engine = engine().await;
            let ddl = DdlExecutor::new(None);
            let statements = [
                "CREATE TABLE \"author\" (\"id\" INTEGER PRIMARY KEY)",
                "CREATE TABLE \"author\" (\"id\" INTEGER PRIMARY KEY)",
            ];
            let err = ddl
                .run(
                    &engine,
                    Unit::Transactional,
                    Door::Pass("author"),
                    &statements,
                    |_| {},
                    None,
                )
                .await
                .expect_err("the second CREATE fails");
            assert!(matches!(
                err,
                DdlError::Failed(Failed { index: Some(1), .. })
            ));
            assert_eq!(count(&engine, "author").await, usize::MAX, "rolled back");
        }

        #[tokio::test]
        async fn an_unwrapped_unit_keeps_what_ran_before_its_failure() {
            let engine = engine().await;
            let ddl = DdlExecutor::new(None);
            let statements = [
                "CREATE TABLE \"author\" (\"id\" INTEGER PRIMARY KEY)",
                "CREATE TABLE \"author\" (\"id\" INTEGER PRIMARY KEY)",
            ];
            let err = ddl
                .run(
                    &engine,
                    Unit::Unwrapped,
                    Door::Pass("author"),
                    &statements,
                    |_| {},
                    None,
                )
                .await
                .expect_err("the second CREATE fails");
            assert!(matches!(
                err,
                DdlError::Failed(Failed { index: Some(1), .. })
            ));
            assert_eq!(count(&engine, "author").await, 0, "the first committed");
        }
    }
}
