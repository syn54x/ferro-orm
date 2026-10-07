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
//! - A transactional unit gets `SET LOCAL lock_timeout` as its transaction's
//!   first statement ([`DdlExecutor::transactional`]); a timeout rolls the
//!   transaction back.
//! - A no-transaction unit gets `SET lock_timeout` before and
//!   `RESET lock_timeout` after, on its own connection
//!   ([`DdlExecutor::unwrapped`]); a timeout re-runs it from the first
//!   statement.
//!
//! A timeout of `None` (`ddl_lock_timeout = "0"`) issues no `SET` and never
//! retries. SQLite has no lock queue of this shape: nothing is set there and
//! nothing is retried. The executor runs the statements it is given and adds
//! only the `SET LOCAL` / `SET` / `RESET` (AGENTS.md § I-1).

use crate::backend::{EngineConnection, EngineHandle};
use ferro_ddl_lowering::Dialect;
use pyo3::prelude::*;
use std::future::Future;
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
    /// How long until the next one.
    pub retry_in: Duration,
    /// The table the failing statement targets, when its text names one
    /// (Postgres' `lock_timeout` error does not say which lock it waited on).
    pub table: Option<String>,
}

impl Attempt {
    /// `waiting for a lock on "author" (attempt 2 of 10, retry in 2s)`.
    pub fn describe(&self, max_attempts: u8) -> String {
        let on = match &self.table {
            Some(table) => format!(" on \"{table}\""),
            None => String::new(),
        };
        format!(
            "waiting for a lock{on} (attempt {} of {max_attempts}, retry in {})",
            self.number,
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

/// A failure inside a unit, as the executor needs to see it: the database
/// error (to tell a lock timeout from anything else) and the statement that
/// raised it (to name its table).
pub trait DdlFailure: From<sqlx::Error> {
    /// The database error behind the failure, when there is one.
    fn database_error(&self) -> Option<&sqlx::Error>;
    /// The statement that failed, when one did.
    fn statement(&self) -> Option<&str>;
}

/// The plain [`DdlFailure`]: a database error and, when a statement raised
/// it, that statement.
#[derive(Debug)]
pub struct StatementError {
    pub statement: Option<String>,
    pub error: sqlx::Error,
}

impl StatementError {
    pub fn at(statement: &str, error: sqlx::Error) -> Self {
        Self {
            statement: Some(statement.to_string()),
            error,
        }
    }
}

impl From<sqlx::Error> for StatementError {
    fn from(error: sqlx::Error) -> Self {
        Self {
            statement: None,
            error,
        }
    }
}

impl DdlFailure for StatementError {
    fn database_error(&self) -> Option<&sqlx::Error> {
        Some(&self.error)
    }

    fn statement(&self) -> Option<&str> {
        self.statement.as_deref()
    }
}

/// How a unit run under the executor failed.
#[derive(Debug)]
pub enum DdlError<E> {
    /// Every attempt timed out waiting for a lock.
    LockTimeout(DdlLockTimeout),
    /// A failure that is not a lock timeout, or any failure with the
    /// timeout disabled: as the unit raised it.
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

    /// Run `body` in a transaction whose first statement is
    /// `SET LOCAL lock_timeout` (Postgres, timeout set), committing when it
    /// succeeds and rolling back when it fails; a lock timeout is retried
    /// from a fresh transaction. `body` gets the connection and hands it
    /// back with its result. `log` sees the `SET LOCAL` before it runs;
    /// `on_attempt` hears each attempt that timed out, before the wait.
    ///
    /// # Errors
    /// [`DdlError::LockTimeout`] after the last attempt; otherwise the
    /// failure as `body` (or `BEGIN` / `COMMIT`) raised it.
    pub async fn transactional<T, E, L, A, F, Fut>(
        &self,
        engine: &EngineHandle,
        log: L,
        mut on_attempt: A,
        mut body: F,
    ) -> Result<T, DdlError<E>>
    where
        E: DdlFailure,
        L: Fn(&str),
        A: FnMut(Attempt),
        F: FnMut(EngineConnection) -> Fut,
        Fut: Future<Output = (EngineConnection, Result<T, E>)>,
    {
        let timeout = self.timeout_on(engine.backend());
        let mut attempt: u8 = 1;
        loop {
            let result = async {
                let mut conn = engine.begin_transaction_connection().await?;
                let set = match timeout {
                    Some(timeout) => {
                        let sql = set_local_sql(timeout);
                        log(&sql);
                        conn.execute_sql_unprepared(&sql).await.map_err(E::from)
                    }
                    None => Ok(0),
                };
                let (mut conn, result) = match set {
                    Ok(_) => body(conn).await,
                    Err(err) => (conn, Err(err)),
                };
                let result = match result {
                    Ok(value) => conn.commit().await.map(|()| value).map_err(E::from),
                    Err(err) => Err(err),
                };
                if result.is_err() && conn.rollback().await.is_err() {
                    let _ = conn.detach_and_close().await;
                }
                result
            }
            .await;
            match self.settle_attempt(attempt, timeout, result, &mut on_attempt) {
                Settled::Done(result) => return result,
                Settled::RetryAfter(wait) => tokio::time::sleep(wait).await,
            }
            attempt = attempt.saturating_add(1);
        }
    }

    /// Run `body` on a connection of its own outside any transaction, with
    /// `SET lock_timeout` before and `RESET lock_timeout` after (Postgres,
    /// timeout set); a lock timeout re-runs it from the start. A `RESET`
    /// that fails closes the connection instead of returning it to the pool
    /// with the setting on it, and leaves `body`'s result as it was.
    ///
    /// # Errors
    /// As [`Self::transactional`].
    pub async fn unwrapped<T, E, L, A, F, Fut>(
        &self,
        engine: &EngineHandle,
        log: L,
        mut on_attempt: A,
        mut body: F,
    ) -> Result<T, DdlError<E>>
    where
        E: DdlFailure,
        L: Fn(&str),
        A: FnMut(Attempt),
        F: FnMut(EngineConnection) -> Fut,
        Fut: Future<Output = (EngineConnection, Result<T, E>)>,
    {
        let timeout = self.timeout_on(engine.backend());
        let mut attempt: u8 = 1;
        loop {
            let result = async {
                let mut conn = pool_connection(engine).await?;
                if let Some(timeout) = timeout {
                    let sql = set_session_sql(timeout);
                    log(&sql);
                    conn.execute_sql_unprepared(&sql).await?;
                }
                let (mut conn, result) = body(conn).await;
                if timeout.is_some() {
                    log(RESET_SQL);
                    if conn.execute_sql_unprepared(RESET_SQL).await.is_err() {
                        let _ = conn.detach_and_close().await;
                    }
                }
                result
            }
            .await;
            match self.settle_attempt(attempt, timeout, result, &mut on_attempt) {
                Settled::Done(result) => return result,
                Settled::RetryAfter(wait) => tokio::time::sleep(wait).await,
            }
            attempt = attempt.saturating_add(1);
        }
    }

    fn settle_attempt<T, E: DdlFailure>(
        &self,
        attempt: u8,
        timeout: Option<Duration>,
        result: Result<T, E>,
        on_attempt: &mut impl FnMut(Attempt),
    ) -> Settled<T, E> {
        let err = match result {
            Ok(value) => return Settled::Done(Ok(value)),
            Err(err) => err,
        };
        let timed_out = err.database_error().is_some_and(is_lock_timeout);
        let outcome = if timed_out {
            AttemptOutcome::LockTimeout
        } else {
            AttemptOutcome::Failed
        };
        let table = || err.statement().and_then(lock_target);
        match (self.next_attempt(attempt, outcome), timeout) {
            (Next::Retry(wait), _) => {
                on_attempt(Attempt {
                    number: attempt,
                    retry_in: wait,
                    table: table(),
                });
                Settled::RetryAfter(wait)
            }
            (Next::Fail, Some(timeout)) if timed_out => {
                Settled::Done(Err(DdlError::LockTimeout(DdlLockTimeout {
                    attempts: attempt,
                    setting: SETTING,
                    timeout,
                    table: table(),
                })))
            }
            (Next::Fail, _) => Settled::Done(Err(DdlError::Failed(err))),
        }
    }
}

enum Settled<T, E> {
    Done(Result<T, DdlError<E>>),
    RetryAfter(Duration),
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
            retry_in: Duration::from_secs(2),
            table: Some("author".to_string()),
        };
        assert_eq!(
            attempt.describe(10),
            "waiting for a lock on \"author\" (attempt 2 of 10, retry in 2s)"
        );
        let unnamed = Attempt {
            number: 1,
            retry_in: Duration::from_millis(50),
            table: None,
        };
        assert_eq!(
            unnamed.describe(10),
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
}
