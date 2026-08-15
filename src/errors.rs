//! Mapping from `sqlx` failures to Ferro's typed Python exceptions.
//!
//! The exception classes live in `ferro.exceptions` (pure Python, so
//! `__module__`, pickling, and docs behave); this module looks them up
//! lazily at first raise and caches the module handle. `_core` never
//! imports `ferro` at module init, so there is no circular import — by
//! the time an error is raised, `ferro` is fully imported.
//!
//! Classification policy (DBAPI-shaped):
//! - Constraint violations map to the `IntegrityError` subclasses.
//!   Ferro's integrity-code table is the authority (`23001`/`23503` FK,
//!   `23505` unique, `23502` not-null, `23514` check); sqlx `kind()` is
//!   the fallback for SQLite and unknown codes.
//! - Environment/runtime failures (I/O, TLS, pool exhaustion) map to
//!   `OperationalError`.
//! - Client-side misuse of the interface (configuration, protocol,
//!   unknown columns) maps to `InterfaceError`.
//! - Value decode/conversion failures map to `DataError`.

use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::PyDict;

static EXCEPTIONS_MODULE: GILOnceCell<Py<PyAny>> = GILOnceCell::new();

/// Python exception class name for a database error, from Ferro's
/// integrity-code table then sqlx `kind()` as fallback.
///
/// Known codes win so Postgres 18 `23001` (`restrict_violation`) is
/// `ForeignKeyViolationError` even though sqlx still reports `Other`.
/// Unknown codes (including `23000` / `23P01`) and SQLite (no SQLSTATE)
/// fall through to `kind()`.
pub(crate) fn exception_name_for_database(
    kind: sqlx::error::ErrorKind,
    code: Option<&str>,
) -> &'static str {
    use sqlx::error::ErrorKind;

    match code {
        Some("23001") | Some("23503") => "ForeignKeyViolationError",
        Some("23505") => "UniqueViolationError",
        Some("23502") => "NotNullViolationError",
        Some("23514") => "CheckViolationError",
        _ => match kind {
            ErrorKind::UniqueViolation => "UniqueViolationError",
            ErrorKind::ForeignKeyViolation => "ForeignKeyViolationError",
            ErrorKind::NotNullViolation => "NotNullViolationError",
            ErrorKind::CheckViolation => "CheckViolationError",
            _ => "OperationalError",
        },
    }
}

/// Python exception class name in `ferro.exceptions` for a `sqlx::Error`.
///
/// Pure classifier so the mapping table is unit-testable without a live
/// database or the GIL.
pub(crate) fn exception_name_for(err: &sqlx::Error) -> &'static str {
    match err {
        sqlx::Error::Database(db) => exception_name_for_database(db.kind(), db.code().as_deref()),
        sqlx::Error::Io(_)
        | sqlx::Error::Tls(_)
        | sqlx::Error::PoolTimedOut
        | sqlx::Error::PoolClosed
        | sqlx::Error::WorkerCrashed
        | sqlx::Error::RowNotFound => "OperationalError",
        sqlx::Error::Configuration(_)
        | sqlx::Error::Protocol(_)
        | sqlx::Error::ColumnNotFound(_)
        | sqlx::Error::ColumnIndexOutOfBounds { .. } => "InterfaceError",
        sqlx::Error::Decode(_)
        | sqlx::Error::ColumnDecode { .. }
        | sqlx::Error::TypeNotFound { .. } => "DataError",
        _ => "OperationalError",
    }
}

fn ferro_exception<'py>(py: Python<'py>, name: &str) -> PyResult<Bound<'py, PyAny>> {
    let module = EXCEPTIONS_MODULE.get_or_try_init(py, || {
        PyResult::Ok(py.import("ferro.exceptions")?.into_any().unbind())
    })?;
    module.bind(py).getattr(name)
}

fn build_exception(
    py: Python<'_>,
    name: &str,
    message: &str,
    driver_message: Option<String>,
    sqlstate: Option<String>,
    constraint: Option<String>,
) -> PyResult<PyErr> {
    let cls = ferro_exception(py, name)?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("driver_message", driver_message)?;
    kwargs.set_item("sqlstate", sqlstate)?;
    kwargs.set_item("constraint", constraint)?;
    let instance = cls.call((message,), Some(&kwargs))?;
    Ok(PyErr::from_value(instance))
}

/// Convert a database failure into the matching typed Ferro exception.
///
/// The message is `"{context}: {driver error}"`; the original driver
/// message, SQLSTATE code, and violated constraint name (when the backend
/// reports them) are preserved as structured attributes on the exception.
pub(crate) fn map_db_error(context: &str, err: sqlx::Error) -> PyErr {
    let name = exception_name_for(&err);
    let (driver_message, sqlstate, constraint) = match &err {
        sqlx::Error::Database(db) => (
            Some(db.message().to_string()),
            db.code().map(|code| code.to_string()),
            db.constraint().map(|constraint| constraint.to_string()),
        ),
        _ => (None, None, None),
    };
    let message = format!("{}: {}", context, err);

    Python::attach(|py| {
        build_exception(py, name, &message, driver_message, sqlstate, constraint)
            .unwrap_or_else(|lookup_err| lookup_err)
    })
}

/// Raise `ferro.exceptions.InterfaceError` for client-side interface misuse
/// that does not originate in a `sqlx::Error` (e.g. routing to an unknown
/// connection name, unsupported connection URL schemes).
pub(crate) fn interface_error(message: impl Into<String>) -> PyErr {
    let message = message.into();
    Python::attach(|py| {
        build_exception(py, "InterfaceError", &message, None, None, None)
            .unwrap_or_else(|lookup_err| lookup_err)
    })
}

#[cfg(test)]
mod tests {
    use super::{exception_name_for, exception_name_for_database};
    use sqlx::error::ErrorKind;

    #[test]
    fn restrict_violation_23001_is_foreign_key() {
        assert_eq!(
            exception_name_for_database(ErrorKind::Other, Some("23001")),
            "ForeignKeyViolationError"
        );
    }

    #[test]
    fn integrity_codes_win_over_kind() {
        let cases = [
            ("23001", "ForeignKeyViolationError"),
            ("23503", "ForeignKeyViolationError"),
            ("23505", "UniqueViolationError"),
            ("23502", "NotNullViolationError"),
            ("23514", "CheckViolationError"),
        ];
        for (code, name) in cases {
            assert_eq!(
                exception_name_for_database(ErrorKind::Other, Some(code)),
                name,
                "code {code}"
            );
        }
    }

    #[test]
    fn unknown_codes_and_sqlite_fall_back_to_kind() {
        assert_eq!(
            exception_name_for_database(ErrorKind::Other, Some("23000")),
            "OperationalError"
        );
        assert_eq!(
            exception_name_for_database(ErrorKind::Other, Some("23P01")),
            "OperationalError"
        );
        assert_eq!(
            exception_name_for_database(ErrorKind::ForeignKeyViolation, None),
            "ForeignKeyViolationError"
        );
        assert_eq!(
            exception_name_for_database(ErrorKind::Other, None),
            "OperationalError"
        );
    }

    #[test]
    fn pool_and_io_failures_are_operational() {
        assert_eq!(
            exception_name_for(&sqlx::Error::PoolTimedOut),
            "OperationalError"
        );
        assert_eq!(
            exception_name_for(&sqlx::Error::PoolClosed),
            "OperationalError"
        );
        assert_eq!(
            exception_name_for(&sqlx::Error::WorkerCrashed),
            "OperationalError"
        );
        assert_eq!(
            exception_name_for(&sqlx::Error::Io(std::io::Error::other("boom"))),
            "OperationalError"
        );
    }

    #[test]
    fn row_not_found_is_operational() {
        assert_eq!(
            exception_name_for(&sqlx::Error::RowNotFound),
            "OperationalError"
        );
    }

    #[test]
    fn configuration_and_protocol_are_interface_errors() {
        assert_eq!(
            exception_name_for(&sqlx::Error::Configuration("bad".into())),
            "InterfaceError"
        );
        assert_eq!(
            exception_name_for(&sqlx::Error::Protocol("bad".into())),
            "InterfaceError"
        );
        assert_eq!(
            exception_name_for(&sqlx::Error::ColumnNotFound("missing".into())),
            "InterfaceError"
        );
    }

    #[test]
    fn decode_failures_are_data_errors() {
        assert_eq!(
            exception_name_for(&sqlx::Error::Decode("bad value".into())),
            "DataError"
        );
        assert_eq!(
            exception_name_for(&sqlx::Error::ColumnDecode {
                index: "0".into(),
                source: "bad value".into(),
            }),
            "DataError"
        );
        assert_eq!(
            exception_name_for(&sqlx::Error::TypeNotFound {
                type_name: "ghost".into(),
            }),
            "DataError"
        );
    }
}
