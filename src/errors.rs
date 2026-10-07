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
//!   Ferro's integrity-code table is the authority (`23001`/`23503`/`1811`
//!   FK, `23505` unique, `23502` not-null, `23514` check); sqlx `kind()`
//!   is the fallback for other SQLite codes and unknown SQLSTATEs.
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
/// Known codes win so Postgres 18 `23001` (`restrict_violation`) and
/// SQLite `1811` (`SQLITE_CONSTRAINT_TRIGGER`, how RESTRICT is
/// implemented) are `ForeignKeyViolationError` even though sqlx reports
/// `Other`. Unknown codes (including `23000` / `23P01`) fall through to
/// `kind()`.
pub(crate) fn exception_name_for_database(
    kind: sqlx::error::ErrorKind,
    code: Option<&str>,
) -> &'static str {
    use sqlx::error::ErrorKind;

    match code {
        Some("23001") | Some("23503") | Some("1811") => "ForeignKeyViolationError",
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

// ---------------------------------------------------------------------------
// The counted failure of a validate or unique index step (ADR-0043, ADR-0044).
//
//   0005_author_email_unique/03_validate.up.postgres.sql failed:
//   3 rows violate "ck_author_email_nonempty"; fix them and run ferro migrate
//   up to resume at 0005_author_email_unique:03
//
// Postgres reports only that some row violated the constraint (or that some
// value is duplicated), so after the failure the runner asks the one count
// query below and names the count and the recipe.
// ---------------------------------------------------------------------------

/// What stopped a validate or unique index step, read off the database's
/// error and the statement that raised it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum CountedFailure {
    /// `ALTER TABLE … VALIDATE CONSTRAINT` met a row the check or foreign key
    /// refuses (`23514`, `23503`).
    Validate {
        /// The constraint's table.
        table: String,
        /// The constraint.
        constraint: String,
    },
    /// `CREATE UNIQUE INDEX` met duplicate values (`23505`).
    UniqueBuild {
        /// The index's table.
        table: String,
        /// The index.
        index: String,
    },
}

/// The counted failure `statement` raised with `sqlstate`, naming `table`
/// and `constraint` (the database's error fields), or `None` when it is no
/// validate or unique index build.
pub(crate) fn counted_failure_of(
    statement: &str,
    sqlstate: Option<&str>,
    table: Option<&str>,
    constraint: Option<&str>,
) -> Option<CountedFailure> {
    let (table, constraint) = (table?.to_string(), constraint?.to_string());
    // A file's first statement carries the file's `-- ferro:` header lines.
    let statement = statement
        .lines()
        .skip_while(|line| line.trim().is_empty() || line.trim_start().starts_with("--"))
        .collect::<Vec<_>>()
        .join("\n");
    let statement = statement.as_str();
    let validates =
        statement.starts_with("ALTER TABLE ") && statement.contains(" VALIDATE CONSTRAINT ");
    match sqlstate? {
        "23514" | "23503" if validates => Some(CountedFailure::Validate { table, constraint }),
        "23505" if statement.starts_with("CREATE UNIQUE INDEX ") => {
            Some(CountedFailure::UniqueBuild {
                table,
                index: constraint,
            })
        }
        _ => None,
    }
}

/// [`counted_failure_of`] for the database error `err` that `statement`
/// raised.
pub(crate) fn counted_failure_of_error(
    statement: Option<&str>,
    err: &sqlx::Error,
) -> Option<CountedFailure> {
    let sqlx::Error::Database(db) = err else {
        return None;
    };
    counted_failure_of(
        statement?,
        db.code().as_deref(),
        db.table(),
        db.constraint(),
    )
}

fn quoted(ident: &str) -> String {
    format!("\"{}\"", ident.replace('"', "\"\""))
}

/// The kind, definition and (for a foreign key) the column pair of the
/// constraint named `$1` on the table `$2` (a quoted identifier).
pub(crate) const CONSTRAINT_FACTS_SQL: &str = "SELECT c.contype::text AS kind, \
     pg_get_constraintdef(c.oid) AS definition, \
     (SELECT a.attname::text FROM pg_attribute a \
      WHERE a.attrelid = c.conrelid AND a.attnum = c.conkey[1]) AS column_name, \
     (SELECT r.relname::text FROM pg_class r WHERE r.oid = c.confrelid) AS ref_table, \
     (SELECT a.attname::text FROM pg_attribute a \
      WHERE a.attrelid = c.confrelid AND a.attnum = c.confkey[1]) AS ref_column \
     FROM pg_constraint c WHERE c.conname = $1 AND c.conrelid = $2::regclass";

/// The columns, in order, of the index named `$1` (a quoted identifier). A
/// failed concurrent build leaves its invalid index behind, so the columns
/// are there to read.
pub(crate) const INDEX_COLUMNS_SQL: &str = "SELECT a.attname::text AS column_name \
     FROM pg_index i JOIN pg_attribute a \
     ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey::int2[]) \
     WHERE i.indexrelid = $1::regclass \
     ORDER BY array_position(i.indkey::int2[], a.attnum)";

/// The rows of `table` a check refuses, from its catalog `definition`
/// (`CHECK (<body>) NOT VALID`, as `pg_get_constraintdef` prints it): the
/// rows where the body is false (a `NULL` body passes a check).
pub(crate) fn check_violation_count_sql(table: &str, definition: &str) -> Option<String> {
    let body = definition.trim().strip_prefix("CHECK ")?;
    let body = body.strip_suffix(" NOT VALID").unwrap_or(body);
    Some(format!(
        "SELECT count(*) FROM {} WHERE NOT ({body})",
        quoted(table)
    ))
}

/// The rows of `table` whose non-null `column` matches no `ref_column` of
/// `ref_table`: a foreign key's orphans.
pub(crate) fn fk_orphan_count_sql(
    table: &str,
    column: &str,
    ref_table: &str,
    ref_column: &str,
) -> String {
    let column = quoted(column);
    format!(
        "SELECT count(*) FROM {} AS child WHERE child.{column} IS NOT NULL AND NOT EXISTS \
         (SELECT 1 FROM {} AS parent WHERE parent.{} = child.{column})",
        quoted(table),
        quoted(ref_table),
        quoted(ref_column),
    )
}

/// How many values of `columns` more than one row of `table` holds: what a
/// unique index over them refuses (rows with a `NULL` in any of them are
/// distinct).
pub(crate) fn duplicate_count_sql(table: &str, columns: &[String]) -> String {
    let list = columns
        .iter()
        .map(|c| quoted(c))
        .collect::<Vec<_>>()
        .join(", ");
    let present = columns
        .iter()
        .map(|c| format!("{} IS NOT NULL", quoted(c)))
        .collect::<Vec<_>>()
        .join(" AND ");
    format!(
        "SELECT count(*) FROM (SELECT 1 FROM {} WHERE {present} GROUP BY {list} \
         HAVING count(*) > 1) AS duplicates",
        quoted(table)
    )
}

/// `3 rows violate "ck_author_email_nonempty"; fix them and run ferro
/// migrate up to resume at 0005_author_email_unique:03`.
pub(crate) fn validate_failure_message(constraint: &str, count: i64, resume_at: &str) -> String {
    let (rows, verb, them) = if count == 1 {
        ("row", "violates", "it")
    } else {
        ("rows", "violate", "them")
    };
    format!(
        "{count} {rows} {verb} \"{constraint}\"; fix {them} and run ferro migrate up to \
         resume at {resume_at}"
    )
}

/// `2 values are duplicated under "uq_author_email"; fix the rows and run
/// ferro migrate up to resume at 0005_author_email_unique:02`.
pub(crate) fn unique_build_failure_message(index: &str, count: i64, resume_at: &str) -> String {
    let values = if count == 1 { "value is" } else { "values are" };
    format!(
        "{count} {values} duplicated under \"{index}\"; fix the rows and run ferro migrate up \
         to resume at {resume_at}"
    )
}

fn first_text(rows: &[crate::backend::EngineRow], column: &str) -> Option<String> {
    let (_, value) = rows
        .first()?
        .values
        .iter()
        .find(|(name, _)| name == column)?;
    match value {
        crate::backend::EngineValue::String(text) => Some(text.clone()),
        _ => None,
    }
}

fn first_count(rows: &[crate::backend::EngineRow]) -> Option<i64> {
    match rows.first()?.values.first()? {
        (_, crate::backend::EngineValue::I64(count)) => Some(*count),
        _ => None,
    }
}

/// The count `failure` names, asked of the database after the step failed:
/// the violating rows of a check, a foreign key's orphans, or a unique
/// index's duplicated values. `None` when the catalog no longer describes
/// the constraint or index.
///
/// # Errors
/// A database error running the queries.
pub(crate) async fn violation_count(
    engine: &crate::backend::EngineHandle,
    failure: &CountedFailure,
) -> Result<Option<i64>, sqlx::Error> {
    use crate::backend::EngineBindValue;
    let sql = match failure {
        CountedFailure::Validate { table, constraint } => {
            let facts = engine
                .fetch_all_sql_unprepared_with_binds(
                    CONSTRAINT_FACTS_SQL,
                    &[
                        EngineBindValue::String(constraint.clone()),
                        EngineBindValue::String(quoted(table)),
                    ],
                )
                .await?;
            match first_text(&facts, "kind").as_deref() {
                Some("c") => first_text(&facts, "definition")
                    .and_then(|definition| check_violation_count_sql(table, &definition)),
                Some("f") => match (
                    first_text(&facts, "column_name"),
                    first_text(&facts, "ref_table"),
                    first_text(&facts, "ref_column"),
                ) {
                    (Some(column), Some(ref_table), Some(ref_column)) => {
                        Some(fk_orphan_count_sql(table, &column, &ref_table, &ref_column))
                    }
                    _ => None,
                },
                _ => None,
            }
        }
        CountedFailure::UniqueBuild { table, index } => {
            let columns: Vec<String> = engine
                .fetch_all_sql_unprepared_with_binds(
                    INDEX_COLUMNS_SQL,
                    &[EngineBindValue::String(quoted(index))],
                )
                .await?
                .iter()
                .filter_map(|row| first_text(std::slice::from_ref(row), "column_name"))
                .collect();
            (!columns.is_empty()).then(|| duplicate_count_sql(table, &columns))
        }
    };
    let Some(sql) = sql else {
        return Ok(None);
    };
    Ok(first_count(&engine.fetch_all_sql_unprepared(&sql).await?))
}

/// The failure text for `failure` given what counting it gave (`counted`):
/// the counted message with the recipe resuming at `resume_at`
/// (`<migration>:<step>`); when the count could not be read, the database's
/// own `original` message with why the count is missing chained after it,
/// so neither is hidden.
pub(crate) fn counted_failure_text(
    failure: &CountedFailure,
    counted: Result<Option<i64>, String>,
    original: &str,
    resume_at: &str,
) -> String {
    let name = match failure {
        CountedFailure::Validate { constraint, .. } => constraint,
        CountedFailure::UniqueBuild { index, .. } => index,
    };
    match counted {
        Ok(Some(count)) => match failure {
            CountedFailure::Validate { .. } => validate_failure_message(name, count, resume_at),
            CountedFailure::UniqueBuild { .. } => {
                unique_build_failure_message(name, count, resume_at)
            }
        },
        Ok(None) => format!(
            "{original} (the offending rows could not be counted: the catalog no longer \
             describes \"{name}\"); fix them and run ferro migrate up to resume at {resume_at}"
        ),
        Err(err) => format!(
            "{original} (counting the offending rows failed: {err}); fix them and run ferro \
             migrate up to resume at {resume_at}"
        ),
    }
}

/// [`counted_failure_text`] for `failure`, counted on `engine` now.
pub(crate) async fn counted_failure_message(
    engine: &crate::backend::EngineHandle,
    failure: &CountedFailure,
    original: &str,
    resume_at: &str,
) -> String {
    let counted = violation_count(engine, failure)
        .await
        .map_err(|err| err.to_string());
    counted_failure_text(failure, counted, original, resume_at)
}

#[cfg(test)]
mod counted_failure_tests {
    use super::*;

    #[test]
    fn a_validate_or_unique_build_failure_is_read_off_its_statement_and_sqlstate() {
        let validate = "ALTER TABLE \"author\" VALIDATE CONSTRAINT \"ck_author_email_nonempty\"";
        assert_eq!(
            counted_failure_of(
                validate,
                Some("23514"),
                Some("author"),
                Some("ck_author_email_nonempty")
            ),
            Some(CountedFailure::Validate {
                table: "author".into(),
                constraint: "ck_author_email_nonempty".into(),
            })
        );
        // A file's first statement carries the file's header lines.
        assert!(
            counted_failure_of(
                &format!("-- ferro: data-dependent\n\n{validate}"),
                Some("23514"),
                Some("author"),
                Some("ck_author_email_nonempty")
            )
            .is_some()
        );
        assert!(matches!(
            counted_failure_of(
                validate,
                Some("23503"),
                Some("post"),
                Some("fk_post_author_id_author")
            ),
            Some(CountedFailure::Validate { .. })
        ));
        let build =
            "CREATE UNIQUE INDEX CONCURRENTLY \"uq_author_email\" ON \"author\" (\"email\")";
        assert_eq!(
            counted_failure_of(
                build,
                Some("23505"),
                Some("author"),
                Some("uq_author_email")
            ),
            Some(CountedFailure::UniqueBuild {
                table: "author".into(),
                index: "uq_author_email".into(),
            })
        );
        // Anything else keeps the database's own message.
        assert_eq!(
            counted_failure_of(build, Some("23514"), Some("author"), Some("x")),
            None
        );
        assert_eq!(
            counted_failure_of(
                "INSERT INTO \"author\" VALUES (1)",
                Some("23505"),
                Some("author"),
                Some("x")
            ),
            None
        );
        assert_eq!(
            counted_failure_of(validate, Some("23514"), None, None),
            None
        );
    }

    #[test]
    fn the_count_queries_read_the_check_body_the_orphans_and_the_duplicates() {
        assert_eq!(
            check_violation_count_sql("author", "CHECK ((email IS NOT NULL)) NOT VALID").as_deref(),
            Some("SELECT count(*) FROM \"author\" WHERE NOT (((email IS NOT NULL)))")
        );
        assert_eq!(
            check_violation_count_sql("author", "CHECK ((email <> ''::text))").as_deref(),
            Some("SELECT count(*) FROM \"author\" WHERE NOT (((email <> ''::text)))")
        );
        assert_eq!(check_violation_count_sql("author", "UNIQUE (email)"), None);
        assert_eq!(
            fk_orphan_count_sql("post", "author_id", "author", "id"),
            "SELECT count(*) FROM \"post\" AS child WHERE child.\"author_id\" IS NOT NULL AND \
             NOT EXISTS (SELECT 1 FROM \"author\" AS parent WHERE parent.\"id\" = \
             child.\"author_id\")"
        );
        assert_eq!(
            duplicate_count_sql("author", &["email".into(), "team_id".into()]),
            "SELECT count(*) FROM (SELECT 1 FROM \"author\" WHERE \"email\" IS NOT NULL AND \
             \"team_id\" IS NOT NULL GROUP BY \"email\", \"team_id\" HAVING count(*) > 1) AS \
             duplicates"
        );
    }

    #[test]
    fn a_count_that_cannot_be_read_chains_onto_the_databases_own_message() {
        let failure = CountedFailure::Validate {
            table: "author".into(),
            constraint: "ck_author_email_nonempty".into(),
        };
        let original = "check constraint \"ck_author_email_nonempty\" of relation \"author\" \
                        is violated by some row";
        assert_eq!(
            counted_failure_text(&failure, Ok(Some(3)), original, "0005_x:03"),
            validate_failure_message("ck_author_email_nonempty", 3, "0005_x:03")
        );
        assert_eq!(
            counted_failure_text(
                &failure,
                Err("connection reset".into()),
                original,
                "0005_x:03"
            ),
            format!(
                "{original} (counting the offending rows failed: connection reset); fix them \
                 and run ferro migrate up to resume at 0005_x:03"
            )
        );
        assert!(
            counted_failure_text(&failure, Ok(None), original, "0005_x:03").starts_with(original)
        );
    }

    #[test]
    fn the_messages_name_the_count_and_the_step_up_resumes_at() {
        assert_eq!(
            validate_failure_message("ck_author_email_nonempty", 3, "0005_author_email_unique:03"),
            "3 rows violate \"ck_author_email_nonempty\"; fix them and run ferro migrate up \
             to resume at 0005_author_email_unique:03"
        );
        assert_eq!(
            validate_failure_message("fk_post_author_id_author", 1, "0006_x:02"),
            "1 row violates \"fk_post_author_id_author\"; fix it and run ferro migrate up to \
             resume at 0006_x:02"
        );
        assert_eq!(
            unique_build_failure_message("uq_author_email", 2, "0005_author_email_unique:02"),
            "2 values are duplicated under \"uq_author_email\"; fix the rows and run ferro \
             migrate up to resume at 0005_author_email_unique:02"
        );
    }
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
    fn sqlite_restrict_trigger_1811_is_foreign_key() {
        assert_eq!(
            exception_name_for_database(ErrorKind::Other, Some("1811")),
            "ForeignKeyViolationError"
        );
    }

    #[test]
    fn integrity_codes_win_over_kind() {
        let cases = [
            ("23001", "ForeignKeyViolationError"),
            ("23503", "ForeignKeyViolationError"),
            ("1811", "ForeignKeyViolationError"),
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
