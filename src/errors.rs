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
    /// A contract met a row that still holds `NULL` in a column it makes
    /// required (ADR-0040, ADR-0042): on Postgres the `VALIDATE` of the
    /// staged `_ferro_notnull_*` check (`23514`; the column is read off the
    /// catalog), on SQLite the rebuild's copy into the `NOT NULL` column
    /// (`NOT NULL constraint failed: _ferro_new_<table>.<column>`).
    NotNull {
        /// The table.
        table: String,
        /// The column, when the error names it (SQLite).
        column: Option<String>,
        /// The staged check, when the error names it (Postgres).
        constraint: Option<String>,
    },
    /// A label removal's swap-type contract (D2) met a row still holding a
    /// removed label: the `ALTER COLUMN … TYPE "<type>_new" USING …` cast
    /// refused it (`22P02`, `invalid input value for enum <type>_new:
    /// "<label>"`).
    RemovedLabel {
        /// The table.
        table: String,
        /// The column.
        column: String,
        /// The label the row holds.
        label: String,
    },
}

// The staged `NOT NULL` check's and the rebuild table's name prefixes are the
// generator's own: one constant each, beside the functions that build the
// names (`backfill::staged_not_null_name`, `rebuild::new_table_name`).
use ferro_migrate::NEW_TABLE_PREFIX as REBUILD_TABLE_PREFIX;
use ferro_migrate::STAGED_NOT_NULL_PREFIX;

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
        "23514" if validates && constraint.starts_with(STAGED_NOT_NULL_PREFIX) => {
            Some(CountedFailure::NotNull {
                table,
                column: None,
                constraint: Some(constraint),
            })
        }
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

/// The SQLite contract failure `statement` raised with `message`: a
/// rebuild's `INSERT INTO "_ferro_new_<table>" …` copy refused by `NOT NULL
/// constraint failed: _ferro_new_<table>.<column>`, or `None`.
pub(crate) fn not_null_copy_failure_of(statement: &str, message: &str) -> Option<CountedFailure> {
    let code = statement
        .lines()
        .skip_while(|line| line.trim().is_empty() || line.trim_start().starts_with("--"))
        .collect::<Vec<_>>()
        .join("\n");
    let copied = code
        .strip_prefix("INSERT INTO \"")?
        .strip_prefix(REBUILD_TABLE_PREFIX)?
        .split_once('"')?
        .0;
    let failed = message.strip_prefix("NOT NULL constraint failed: ")?;
    let (table, column) = failed.trim().split_once('.')?;
    let table = table.strip_prefix(REBUILD_TABLE_PREFIX)?;
    (table == copied).then(|| CountedFailure::NotNull {
        table: table.to_string(),
        column: Some(column.to_string()),
        constraint: None,
    })
}

/// The swap-type contract failure `statement` raised with `sqlstate` and
/// `message`: `ALTER TABLE "<table>" ALTER COLUMN "<column>" TYPE
/// "<type>_new" USING …` (`generate::enums::render_swap_type`) refused with
/// `invalid input value for enum <type>_new: "<label>"`, or `None`.
pub(crate) fn removed_label_failure_of(
    statement: &str,
    sqlstate: Option<&str>,
    message: &str,
) -> Option<CountedFailure> {
    if sqlstate? != "22P02" {
        return None;
    }
    let code = statement
        .lines()
        .skip_while(|line| line.trim().is_empty() || line.trim_start().starts_with("--"))
        .collect::<Vec<_>>()
        .join("\n");
    let (table, rest) = unquote_ident(code.strip_prefix("ALTER TABLE ")?)?;
    let (column, rest) = unquote_ident(rest.strip_prefix(" ALTER COLUMN ")?)?;
    let (staged, rest) = unquote_ident(rest.strip_prefix(" TYPE ")?)?;
    if !rest.starts_with(" USING ") || !staged.ends_with("_new") {
        return None;
    }
    // Postgres names the type as `format_type` prints it (quoted when it
    // must be): the label is what follows its last `: "`.
    let label = message
        .strip_prefix("invalid input value for enum ")?
        .rsplit_once(": \"")?
        .1
        .strip_suffix('"')?;
    Some(CountedFailure::RemovedLabel {
        table,
        column,
        label: label.to_string(),
    })
}

/// A leading `"…"` identifier of `text` (doubled quotes undone) and the rest.
fn unquote_ident(text: &str) -> Option<(String, &str)> {
    let body = text.strip_prefix('"')?;
    let mut out = String::new();
    let mut chars = body.char_indices().peekable();
    while let Some((at, ch)) = chars.next() {
        if ch != '"' {
            out.push(ch);
            continue;
        }
        if matches!(chars.peek(), Some((_, '"'))) {
            chars.next();
            out.push('"');
            continue;
        }
        return Some((out, &body[at + 1..]));
    }
    None
}

/// [`counted_failure_of`] (or, on SQLite, [`not_null_copy_failure_of`]; or
/// a label removal's [`removed_label_failure_of`]) for the database error
/// `err` that `statement` raised.
pub(crate) fn counted_failure_of_error(
    statement: Option<&str>,
    err: &sqlx::Error,
) -> Option<CountedFailure> {
    let sqlx::Error::Database(db) = err else {
        return None;
    };
    let statement = statement?;
    counted_failure_of(statement, db.code().as_deref(), db.table(), db.constraint())
        .or_else(|| not_null_copy_failure_of(statement, db.message()))
        .or_else(|| removed_label_failure_of(statement, db.code().as_deref(), db.message()))
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

/// The reconciliation pass's counted unique build failure (ADR-0051): `2
/// values are duplicated under "uq_author_email" on "author"; fix the rows
/// and connect again`, as its uncounted siblings say. The pass has no step to
/// resume at: connecting runs it again.
pub(crate) fn pass_unique_build_failure_message(index: &str, table: &str, count: i64) -> String {
    let values = if count == 1 { "value is" } else { "values are" };
    format!(
        "{count} {values} duplicated under \"{index}\" on \"{table}\"; fix the rows and connect \
         again"
    )
}

/// Whether `err` is the database refusing a unique index over duplicate
/// values (`23505` on Postgres, `SQLITE_CONSTRAINT_UNIQUE` on SQLite).
pub(crate) fn is_unique_violation(err: &sqlx::Error) -> bool {
    matches!(
        err,
        sqlx::Error::Database(db) if db.kind() == sqlx::error::ErrorKind::UniqueViolation
    )
}

/// The error for the pass's `CREATE UNIQUE INDEX` of `index` over `columns`
/// on `table` that duplicates refused (`err`): counted on `engine` now, by
/// the declared columns (the failed unit rolled back, so whatever index
/// stands under the name may be another definition). When the count cannot
/// be read, the database's own error with why the count is missing.
pub(crate) async fn pass_unique_build_failure(
    engine: &crate::backend::EngineHandle,
    table: &str,
    index: &str,
    columns: &[String],
    err: sqlx::Error,
) -> PyErr {
    let counted = engine
        .fetch_all_sql_unprepared(&duplicate_count_sql(table, columns))
        .await
        .map(|rows| first_count(&rows));
    let context = match counted {
        Ok(Some(count)) => pass_unique_build_failure_message(index, table, count),
        Ok(None) => format!(
            "Auto-migrate could not build unique index \"{index}\" on \"{table}\" (the \
             duplicated values could not be counted); fix the rows and connect again"
        ),
        Err(count_err) => format!(
            "Auto-migrate could not build unique index \"{index}\" on \"{table}\" (counting \
             the duplicated values failed: {count_err}); fix the rows and connect again"
        ),
    };
    map_db_error(&context, err)
}

/// `2 rows still have NULL "slug" in "author"; run ferro migrate down --to
/// 0012:01 then ferro migrate up to re-run the backfill`: a contract that met
/// rows written behind its migration's backfill (ADR-0040). The recipe
/// reverts every step after `expand_step` of `migration` (the backfill's
/// down reverses nothing, so only its record goes) and `up` re-runs the
/// backfill, whose query selects exactly the rows still needing a value.
/// `expand_step` is `0` when the backfill is the migration's first step:
/// the recipe then reverts to the migration before it.
pub(crate) fn map_contract_failure(
    table: &str,
    column: &str,
    count: i64,
    migration: u16,
    expand_step: u8,
) -> String {
    let (rows, have) = if count == 1 {
        ("row", "has")
    } else {
        ("rows", "have")
    };
    let target = rerun_target(migration, expand_step);
    format!(
        "{count} {rows} still {have} NULL \"{column}\" in \"{table}\"; run ferro migrate down \
         --to {target} then ferro migrate up to re-run the backfill"
    )
}

/// What `ferro migrate down --to` reverts to so `up` re-runs the backfill
/// after `expand_step` of `migration` (the migration before it when the
/// backfill is the first step).
fn rerun_target(migration: u16, expand_step: u8) -> String {
    if expand_step == 0 {
        format!("{:04}", migration.saturating_sub(1))
    } else {
        format!("{migration:04}:{expand_step:02}")
    }
}

/// `2 rows still hold 'canceled' in "status" of "order"; run ferro migrate
/// down --to 0011 then ferro migrate up to re-run the backfill`: a label
/// removal's contract that met rows written behind its backfill (D2), with
/// [`map_contract_failure`]'s recipe.
pub(crate) fn map_removed_label_failure(
    table: &str,
    column: &str,
    label: &str,
    count: i64,
    migration: u16,
    expand_step: u8,
) -> String {
    let (rows, hold) = if count == 1 {
        ("row", "holds")
    } else {
        ("rows", "hold")
    };
    format!(
        "{count} {rows} still {hold} {} in \"{column}\" of \"{table}\"; run ferro migrate down \
         --to {} then ferro migrate up to re-run the backfill",
        ferro_ddl_lowering::quote_label(label),
        rerun_target(migration, expand_step)
    )
}

/// The rows of `table` whose `column` holds `label`, read as text (the
/// column's type is the enum still declaring it).
pub(crate) fn removed_label_count_sql(table: &str, column: &str, label: &str) -> String {
    format!(
        "SELECT count(*) FROM {} WHERE {}::text = {}",
        quoted(table),
        quoted(column),
        ferro_ddl_lowering::quote_label(label)
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
        CountedFailure::NotNull { table, .. } => not_null_column(engine, failure)
            .await?
            .map(|column| not_null_count_sql(table, &column)),
        CountedFailure::RemovedLabel {
            table,
            column,
            label,
        } => Some(removed_label_count_sql(table, column, label)),
    };
    let Some(sql) = sql else {
        return Ok(None);
    };
    Ok(first_count(&engine.fetch_all_sql_unprepared(&sql).await?))
}

/// The rows of `table` whose `column` is still `NULL`.
pub(crate) fn not_null_count_sql(table: &str, column: &str) -> String {
    format!(
        "SELECT count(*) FROM {} WHERE {} IS NULL",
        quoted(table),
        quoted(column)
    )
}

/// The column a [`CountedFailure::NotNull`] names: SQLite's error names it;
/// Postgres's staged check is read off the catalog (its one column).
async fn not_null_column(
    engine: &crate::backend::EngineHandle,
    failure: &CountedFailure,
) -> Result<Option<String>, sqlx::Error> {
    use crate::backend::EngineBindValue;
    let CountedFailure::NotNull {
        table,
        column,
        constraint,
    } = failure
    else {
        return Ok(None);
    };
    if let Some(column) = column {
        return Ok(Some(column.clone()));
    }
    let Some(constraint) = constraint else {
        return Ok(None);
    };
    let facts = engine
        .fetch_all_sql_unprepared_with_binds(
            CONSTRAINT_FACTS_SQL,
            &[
                EngineBindValue::String(constraint.clone()),
                EngineBindValue::String(quoted(table)),
            ],
        )
        .await?;
    Ok(first_text(&facts, "column_name"))
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
        CountedFailure::NotNull { table, .. } | CountedFailure::RemovedLabel { table, .. } => table,
    };
    match counted {
        Ok(Some(count)) => match failure {
            CountedFailure::Validate { .. } => validate_failure_message(name, count, resume_at),
            CountedFailure::UniqueBuild { .. } => {
                unique_build_failure_message(name, count, resume_at)
            }
            CountedFailure::NotNull { .. } => format!(
                "{original} ({count} rows of \"{name}\" still hold NULL); give them a value and \
                 run ferro migrate up to resume at {resume_at}"
            ),
            CountedFailure::RemovedLabel { column, label, .. } => format!(
                "{original} ({count} rows still hold {} in \"{column}\" of \"{name}\"); give \
                 them another label and run ferro migrate up to resume at {resume_at}",
                ferro_ddl_lowering::quote_label(label)
            ),
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

/// The failure text of a contract that met `NULL` rows in `table`, given
/// what counting them gave (`counted`: the column and its `NULL` rows):
/// [`map_contract_failure`]'s count and recipe when `rerun` (the migration
/// and the step before its backfill) is known; otherwise the database's own
/// `original` message with the count, or with why it is missing.
pub(crate) fn contract_failure_text(
    table: &str,
    counted: Result<Option<(String, i64)>, String>,
    original: &str,
    resume_at: &str,
    rerun: Option<(u16, u8)>,
) -> String {
    match (counted, rerun) {
        (Ok(Some((column, count))), Some((migration, expand_step))) => {
            map_contract_failure(table, &column, count, migration, expand_step)
        }
        (Ok(Some((column, count))), None) => format!(
            "{original} ({count} rows still hold NULL \"{column}\" in \"{table}\"); give them a \
             value and run ferro migrate up to resume at {resume_at}"
        ),
        (Ok(None), _) => format!(
            "{original} (the NULL rows could not be counted: the catalog no longer describes \
             the staged check on \"{table}\"); give them a value and run ferro migrate up to \
             resume at {resume_at}"
        ),
        (Err(err), _) => format!(
            "{original} (counting the NULL rows failed: {err}); give them a value and run ferro \
             migrate up to resume at {resume_at}"
        ),
    }
}

/// The column a contract's `NULL` rows hold and how many there are, counted
/// on `engine` now.
async fn null_rows(
    engine: &crate::backend::EngineHandle,
    failure: &CountedFailure,
    table: &str,
) -> Result<Option<(String, i64)>, sqlx::Error> {
    let Some(column) = not_null_column(engine, failure).await? else {
        return Ok(None);
    };
    let rows = engine
        .fetch_all_sql_unprepared(&not_null_count_sql(table, &column))
        .await?;
    Ok(first_count(&rows).map(|count| (column, count)))
}

/// [`counted_failure_text`] for `failure`, counted on `engine` now; a
/// contract's [`contract_failure_text`], whose recipe re-runs the backfill
/// after `rerun` (`(migration, step before the backfill)`).
pub(crate) async fn counted_failure_message(
    engine: &crate::backend::EngineHandle,
    failure: &CountedFailure,
    original: &str,
    resume_at: &str,
    rerun: Option<(u16, u8)>,
) -> String {
    if let CountedFailure::NotNull { table, .. } = failure {
        let counted = null_rows(engine, failure, table)
            .await
            .map_err(|err| err.to_string());
        return contract_failure_text(table, counted, original, resume_at, rerun);
    }
    let counted = violation_count(engine, failure)
        .await
        .map_err(|err| err.to_string());
    // A label removal's contract re-runs its backfill, as a `NULL` one does.
    if let (
        CountedFailure::RemovedLabel {
            table,
            column,
            label,
        },
        Ok(Some(count)),
        Some((migration, expand_step)),
    ) = (failure, &counted, rerun)
    {
        return map_removed_label_failure(table, column, label, *count, migration, expand_step);
    }
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
    fn a_contract_failure_is_read_off_the_staged_check_or_the_rebuilds_copy() {
        let validate = "ALTER TABLE \"author\" VALIDATE CONSTRAINT \"_ferro_notnull_author_slug\"";
        assert_eq!(
            counted_failure_of(
                &format!("-- ferro: data-dependent\n\n{validate}"),
                Some("23514"),
                Some("author"),
                Some("_ferro_notnull_author_slug")
            ),
            Some(CountedFailure::NotNull {
                table: "author".into(),
                column: None,
                constraint: Some("_ferro_notnull_author_slug".into()),
            })
        );
        let copy = "-- ferro: foreign-keys-off\n-- ferro: data-dependent\n\nINSERT INTO \
                    \"_ferro_new_author\" (\"id\", \"slug\") SELECT \"id\", \"slug\" FROM \"author\"";
        assert_eq!(
            not_null_copy_failure_of(copy, "NOT NULL constraint failed: _ferro_new_author.slug"),
            Some(CountedFailure::NotNull {
                table: "author".into(),
                column: Some("slug".into()),
                constraint: None,
            })
        );
        // Another statement, another table, another failure: not a contract.
        assert_eq!(
            not_null_copy_failure_of(
                "INSERT INTO \"author\" (\"slug\") VALUES (NULL)",
                "NOT NULL constraint failed: author.slug"
            ),
            None
        );
        assert_eq!(
            not_null_copy_failure_of(copy, "CHECK constraint failed: ck_author_slug"),
            None
        );
        assert_eq!(
            not_null_count_sql("author", "slug"),
            "SELECT count(*) FROM \"author\" WHERE \"slug\" IS NULL"
        );
    }

    #[test]
    fn a_contract_failure_is_recognised_by_the_names_the_generator_builds() {
        use ferro_migrate::{new_table_name, staged_not_null_name};

        // Whatever the generator names the staging check (63-char guard
        // included), the runner reads its failed VALIDATE as a contract's.
        for (table, column) in [("author", "slug"), (&*"t".repeat(40), &*"c".repeat(40))] {
            let name = staged_not_null_name(table, column);
            let validate = format!("ALTER TABLE \"{table}\" VALIDATE CONSTRAINT \"{name}\"");
            assert_eq!(
                counted_failure_of(&validate, Some("23514"), Some(table), Some(&name)),
                Some(CountedFailure::NotNull {
                    table: table.into(),
                    column: None,
                    constraint: Some(name.clone()),
                })
            );
        }
        let copied = new_table_name("author");
        let copy = format!("INSERT INTO \"{copied}\" (\"slug\") SELECT \"slug\" FROM \"author\"");
        assert!(
            not_null_copy_failure_of(&copy, &format!("NOT NULL constraint failed: {copied}.slug"))
                .is_some()
        );
    }

    #[test]
    fn a_contract_failure_names_the_count_and_the_recipe_that_re_runs_the_backfill() {
        assert_eq!(
            map_contract_failure("author", "slug", 2, 12, 1),
            "2 rows still have NULL \"slug\" in \"author\"; run ferro migrate down --to \
             0012:01 then ferro migrate up to re-run the backfill"
        );
        // A backfill that is the migration's first step: back to the one before.
        assert_eq!(
            map_contract_failure("author", "slug", 1, 12, 0),
            "1 row still has NULL \"slug\" in \"author\"; run ferro migrate down --to 0011 \
             then ferro migrate up to re-run the backfill"
        );
        let original = "check constraint \"_ferro_notnull_author_slug\" is violated by some row";
        assert_eq!(
            contract_failure_text(
                "author",
                Ok(Some(("slug".into(), 2))),
                original,
                "0012_x:05",
                Some((12, 1))
            ),
            map_contract_failure("author", "slug", 2, 12, 1)
        );
        assert!(
            contract_failure_text("author", Err("gone".into()), original, "0012_x:05", None)
                .starts_with(original)
        );
    }

    #[test]
    fn a_swap_type_contract_meeting_a_removed_label_is_counted_with_the_recipe() {
        let statement = "-- ferro: data-dependent\n\nALTER TABLE \"order\" ALTER COLUMN \
                         \"status\" TYPE \"orderstatus_new\" USING \
                         \"status\"::text::\"orderstatus_new\"";
        let message = "invalid input value for enum orderstatus_new: \"canceled\"";
        let failure = CountedFailure::RemovedLabel {
            table: "order".into(),
            column: "status".into(),
            label: "canceled".into(),
        };
        assert_eq!(
            removed_label_failure_of(statement, Some("22P02"), message),
            Some(failure.clone())
        );
        assert_eq!(
            removed_label_failure_of(statement, Some("23514"), message),
            None
        );
        assert_eq!(
            removed_label_failure_of("UPDATE \"order\" SET x = 1", Some("22P02"), message),
            None
        );
        assert_eq!(
            removed_label_count_sql("order", "status", "canceled"),
            "SELECT count(*) FROM \"order\" WHERE \"status\"::text = 'canceled'"
        );
        assert_eq!(
            map_removed_label_failure("order", "status", "canceled", 2, 12, 0),
            "2 rows still hold 'canceled' in \"status\" of \"order\"; run ferro migrate down \
             --to 0011 then ferro migrate up to re-run the backfill"
        );
        assert!(
            counted_failure_text(&failure, Ok(Some(1)), message, "0012_x:02").starts_with(message)
        );
    }

    #[test]
    fn the_pass_names_the_duplicates_and_connecting_again() {
        assert_eq!(
            pass_unique_build_failure_message("uq_author_email", "author", 2),
            "2 values are duplicated under \"uq_author_email\" on \"author\"; fix the rows \
             and connect again"
        );
        assert!(
            pass_unique_build_failure_message("uq_author_email", "author", 1)
                .starts_with("1 value is duplicated")
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
