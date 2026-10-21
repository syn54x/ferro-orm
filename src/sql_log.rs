//! The statement log (ADR-0057): every statement ferro sends, one record on
//! the `ferro.sql` logger as it finishes.
//!
//! ```text
//! >>> logging.getLogger("ferro.sql").setLevel(logging.DEBUG)
//! >>> await ferro.migrate()
//! ferro.sql DEBUG ALTER TABLE "author" ADD COLUMN "bio" varchar params=() 0.8ms
//! ```
//!
//! The logger is the only switch. An FFI entry asks [`enabled`] once, while
//! it holds the GIL, and opens a [`scope`] only when the answer is yes. The
//! engine's execute and fetch layer (`crate::backend`), which every statement
//! passes through, calls [`observe`] after each statement; outside a scope
//! that is a task-local read and nothing more. Inside one it takes the GIL
//! once and hands the statement's data to `ferro._sql_log.emit`, the one
//! place a record's message is built: Rust formats nothing.
//!
//! The emitter raising is an error of the operation, not something to
//! swallow: the scope keeps the first such error and returns it in place of
//! the operation's result once the operation finishes.

use crate::backend::EngineBindValue;
use ferro_ddl_lowering::Dialect;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBytes, PyDict, PyTuple};
use std::future::Future;
use std::sync::{Arc, Mutex};
use std::time::Instant;

/// `logging.DEBUG`.
const DEBUG: i32 = 10;

/// The `operation` of every statement the reconciliation pass or a run
/// sends.
pub const MIGRATE: &str = "migrate";

static LOGGER: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static EMITTER_MODULE: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static UUID_CLASS: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

/// Whether `ferro.sql` is enabled for `DEBUG`: the statement log's one
/// switch, asked once per FFI entry.
///
/// # Errors
/// The `PyErr` from importing `logging` or asking the logger.
pub fn enabled(py: Python<'_>) -> PyResult<bool> {
    LOGGER
        .get_or_try_init(py, || -> PyResult<Py<PyAny>> {
            Ok(py
                .import("logging")?
                .call_method1("getLogger", ("ferro.sql",))?
                .unbind())
        })?
        .bind(py)
        .call_method1("isEnabledFor", (DEBUG,))?
        .extract()
}

/// What every record written inside one [`scope`] shares.
#[derive(Clone, Debug)]
pub struct StatementContext {
    /// What sent the statements: `"migrate"` for the reconciliation pass and
    /// a run.
    pub operation: &'static str,
    /// The connection's name (`"default"` unless named).
    pub connection: String,
    /// The table, enum type or step file the statement belongs to, when
    /// there is one (see [`with_subject`]).
    pub subject: Option<String>,
    /// Bound values a record shows as `***`.
    pub secret_values: Vec<EngineBindValue>,
}

impl StatementContext {
    /// A context for `operation` on `connection`, with no subject and no
    /// secret values.
    pub fn new(operation: &'static str, connection: impl Into<String>) -> Self {
        Self {
            operation,
            connection: connection.into(),
            subject: None,
            secret_values: Vec::new(),
        }
    }
}

/// An open scope: its context, and the first error the emitter raised in it
/// (shared with every [`with_subject`] nested inside).
#[derive(Debug)]
struct Active {
    context: StatementContext,
    failure: Arc<Mutex<Option<PyErr>>>,
}

impl Active {
    fn fail(&self, err: PyErr) {
        match self.failure.lock() {
            Ok(mut slot) => {
                if slot.is_none() {
                    *slot = Some(err);
                }
            }
            // A poisoned slot cannot hold the error; it must still be seen.
            Err(_) => Python::attach(|py| err.write_unraisable(py, None)),
        }
    }

    fn take_failure(&self) -> Option<PyErr> {
        match self.failure.lock() {
            Ok(mut slot) => slot.take(),
            Err(poisoned) => poisoned.into_inner().take(),
        }
    }
}

tokio::task_local! {
    static ACTIVE: Option<Arc<Active>>;
}

fn current() -> Option<Arc<Active>> {
    ACTIVE.try_with(Clone::clone).ok().flatten()
}

/// Run `fut` with the statement log open under `ctx`; with `None` (the log
/// is off) run it as it is. Every statement `fut` sends on this task is
/// written under `ctx`.
///
/// # Errors
/// `fut`'s error; when `fut` succeeded, the first error the emitter raised
/// while it ran.
pub async fn scope<F, T>(ctx: Option<StatementContext>, fut: F) -> PyResult<T>
where
    F: Future<Output = PyResult<T>>,
{
    let Some(context) = ctx else {
        return fut.await;
    };
    let active = Arc::new(Active {
        context,
        failure: Arc::new(Mutex::new(None)),
    });
    let result = ACTIVE.scope(Some(Arc::clone(&active)), fut).await;
    match (result, active.take_failure()) {
        (result, None) => result,
        (Ok(_), Some(failure)) => Err(failure),
        (Err(err), Some(failure)) => {
            // The operation's own error is the answer; the emitter's still
            // reaches the reader.
            Python::attach(|py| failure.write_unraisable(py, None));
            Err(err)
        }
    }
}

/// Run `fut` with the open scope's subject set to `subject` (a pass's table,
/// a run's `NNNN_name/<step file>`). Outside a scope it is `fut` itself.
pub async fn with_subject<F: Future>(subject: &str, fut: F) -> F::Output {
    match current() {
        Some(active) => {
            let child = Active {
                context: StatementContext {
                    subject: Some(subject.to_string()),
                    ..active.context.clone()
                },
                failure: Arc::clone(&active.failure),
            };
            ACTIVE.scope(Some(Arc::new(child)), fut).await
        }
        None => fut.await,
    }
}

/// Write one finished statement to the statement log: `sql` with `binds`,
/// on `dialect`, sent at `started`, with what it returned (rows affected,
/// `None` for a fetch, or the database's error). A no-op outside a scope.
pub fn observe(
    sql: &str,
    binds: &[EngineBindValue],
    dialect: Dialect,
    started: Instant,
    outcome: Result<Option<u64>, &sqlx::Error>,
) {
    let Some(active) = current() else {
        return;
    };
    let duration_ms = started.elapsed().as_secs_f64() * 1000.0;
    let written = Python::attach(|py| {
        emit(
            py,
            &active.context,
            sql,
            binds,
            dialect,
            duration_ms,
            outcome,
        )
    });
    if let Err(err) = written {
        active.fail(err);
    }
}

/// Which of `binds` are secret: equal to one of `secrets`.
fn mask_of(binds: &[EngineBindValue], secrets: &[EngineBindValue]) -> Vec<bool> {
    binds.iter().map(|bind| secrets.contains(bind)).collect()
}

fn dialect_name(dialect: Dialect) -> &'static str {
    match dialect {
        Dialect::Postgres => "postgres",
        Dialect::Sqlite => "sqlite",
    }
}

fn bind_to_py<'py>(py: Python<'py>, bind: &EngineBindValue) -> PyResult<Bound<'py, PyAny>> {
    use pyo3::IntoPyObjectExt;
    match bind {
        EngineBindValue::Bool(value) => value.into_bound_py_any(py),
        EngineBindValue::I64(value) => value.into_bound_py_any(py),
        EngineBindValue::F64(value) => value.into_bound_py_any(py),
        EngineBindValue::String(value) => value.into_bound_py_any(py),
        EngineBindValue::Bytes(value) => Ok(PyBytes::new(py, value).into_any()),
        EngineBindValue::Uuid(value) => UUID_CLASS
            .get_or_try_init(py, || -> PyResult<Py<PyAny>> {
                Ok(py.import("uuid")?.getattr("UUID")?.unbind())
            })?
            .bind(py)
            .call1((value.to_string(),)),
        EngineBindValue::Null(_) => Ok(py.None().into_bound(py)),
    }
}

/// Hand one statement's data to `ferro._sql_log.emit`, looked up on its
/// module at each call so the emitter is always the module's current one.
fn emit(
    py: Python<'_>,
    context: &StatementContext,
    sql: &str,
    binds: &[EngineBindValue],
    dialect: Dialect,
    duration_ms: f64,
    outcome: Result<Option<u64>, &sqlx::Error>,
) -> PyResult<()> {
    let mask = mask_of(binds, &context.secret_values);
    let params = binds
        .iter()
        .zip(&mask)
        .map(|(bind, &masked)| {
            if masked {
                Ok(py.None().into_bound(py))
            } else {
                bind_to_py(py, bind)
            }
        })
        .collect::<PyResult<Vec<_>>>()?;
    let (rows, error) = match outcome {
        Ok(rows) => (rows, None),
        Err(err) => (None, Some(err.to_string())),
    };
    let record = PyDict::new(py);
    record.set_item("sql", sql)?;
    record.set_item("params", PyTuple::new(py, params)?)?;
    record.set_item("mask", PyTuple::new(py, mask)?)?;
    record.set_item("dialect", dialect_name(dialect))?;
    record.set_item("duration_ms", duration_ms)?;
    record.set_item("operation", context.operation)?;
    record.set_item("subject", context.subject.as_deref())?;
    record.set_item("connection", context.connection.as_str())?;
    record.set_item("rows", rows)?;
    record.set_item("error", error)?;
    EMITTER_MODULE
        .get_or_try_init(py, || -> PyResult<Py<PyAny>> {
            Ok(py.import("ferro._sql_log")?.into_any().unbind())
        })?
        .bind(py)
        .getattr("emit")?
        .call1((record,))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::backend::NullKind;

    #[test]
    fn a_bind_is_masked_when_it_equals_a_secret_value() {
        let secret = EngineBindValue::String("hunter2".into());
        let binds = [
            EngineBindValue::I64(7),
            secret.clone(),
            EngineBindValue::Null(NullKind::String),
        ];
        assert_eq!(mask_of(&binds, &[secret]), vec![false, true, false]);
        assert_eq!(mask_of(&binds, &[]), vec![false, false, false]);
    }

    #[tokio::test]
    async fn outside_a_scope_there_is_no_context_and_observe_does_nothing() {
        assert!(current().is_none());
        // No scope: observe returns before touching Python.
        observe("SELECT 1", &[], Dialect::Sqlite, Instant::now(), Ok(None));
        let ran = scope(None, async { Ok::<_, PyErr>(current().is_none()) }).await;
        assert!(ran.is_ok_and(|none| none));
    }

    #[tokio::test]
    async fn with_subject_names_the_statements_inside_it_and_no_others() {
        let seen = scope(Some(StatementContext::new("migrate", "default")), async {
            let before = current().and_then(|a| a.context.subject.clone());
            let inside = with_subject("author", async {
                current().and_then(|a| a.context.subject.clone())
            })
            .await;
            let after = current().and_then(|a| a.context.subject.clone());
            let connection = current().map(|a| a.context.connection.clone());
            Ok::<_, PyErr>((before, inside, after, connection))
        })
        .await;
        let (before, inside, after, connection) = match seen {
            Ok(seen) => seen,
            Err(err) => panic!("scope failed: {err}"),
        };
        assert_eq!(before, None);
        assert_eq!(inside.as_deref(), Some("author"));
        assert_eq!(after, None);
        assert_eq!(connection.as_deref(), Some("default"));
    }

    #[tokio::test]
    async fn with_subject_outside_a_scope_opens_none() {
        let inside = with_subject("author", async { current().is_none() }).await;
        assert!(inside);
    }
}
