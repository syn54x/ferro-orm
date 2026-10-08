//! The SQLite table rebuild (ADR-0034, ADR-0046): every change SQLite's
//! `ALTER TABLE` cannot express, rendered as the table built again in its new
//! shape under a temporary name, its rows copied across, and swapped in.
//!
//! ```text
//! class Author(Model):          0004_author_age_text/01_schema.up.sqlite.sql
//!     age: str  # was int  ──▶  -- ferro: foreign-keys-off
//!                               -- ferro: data-dependent
//!                               CREATE TABLE IF NOT EXISTS "_ferro_new_author" (…, "age" varchar NOT NULL );
//!                               INSERT INTO "_ferro_new_author" ("age", …) SELECT "age", … FROM "author";
//!                               CREATE TEMP TABLE "_ferro_rebuild_guard" ("value", CONSTRAINT "ferro: author.age has a value that cannot become varchar" CHECK (0));
//!                               INSERT INTO "_ferro_rebuild_guard" ("value") SELECT "age" FROM "_ferro_new_author" WHERE typeof("age") NOT IN ('text', 'null');
//!                               DROP TABLE "_ferro_rebuild_guard";
//!                               DELETE FROM sqlite_sequence WHERE name = '_ferro_new_author';
//!                               INSERT INTO sqlite_sequence (name, seq) SELECT '_ferro_new_author', seq FROM sqlite_sequence WHERE name = 'author';
//!                               DROP TABLE "author";
//!                               ALTER TABLE "_ferro_new_author" RENAME TO "author";
//!                               CREATE UNIQUE INDEX IF NOT EXISTS "uq_author_name" ON "author" ("name");
//! ```
//!
//! Whether SQLite runs an op natively or only through a rebuild is the op's
//! verdict ([`crate::Execution::Rebuild`], decided once by the planner);
//! [`render`] writes the rebuild. The `CREATE TABLE` is the create pass's own
//! ([`crate::emit::render_create_table_as`]) for the shape the step leaves,
//! byte for byte apart from the table's name, and the indexes after the
//! rename are the create pass's own statements (AGENTS.md § I-1). The file
//! holds only these statements: the `foreign-keys-off` header hands the
//! pragma, the transaction and `PRAGMA foreign_key_check` to the runner.

use crate::emit::{backfill_value_sql, render_create_table_as};
use crate::{Dialect, EmissionError, Execution, PlannedOp};
use ferro_ddl_lowering::{
    CanonicalType, ResolvedStorage, quote_ident, render_relabel_copy, resolve_column_storage,
    sqlite_declared_type,
};
use ferro_schema_ir::{IrEnvelope, SchemaColumn, SchemaIrPayload, SchemaModel};
use std::collections::BTreeSet;

/// The prefix of the table a rebuild creates and renames into place:
/// `_ferro_new_<table>`. It never outlives its step, which is one
/// transaction.
pub const NEW_TABLE_PREFIX: &str = "_ferro_new_";

/// The temporary name `table` is rebuilt under.
pub fn new_table_name(table: &str) -> String {
    format!("{NEW_TABLE_PREFIX}{table}")
}

/// The tables a file rebuilds: each table one of `ops` has the
/// [`Execution::Rebuild`] verdict for. Every other op on such a table folds
/// into its one rebuild (ADR-0046: one copy per table per phase step).
pub fn tables_to_rebuild(ops: &[PlannedOp]) -> BTreeSet<String> {
    ops.iter()
        .filter(|planned| planned.verdict.execution == Execution::Rebuild)
        .filter_map(|planned| planned.op.table().map(str::to_string))
        .collect()
}

/// [`render`] for `table` in a file turning `old` into `new`, copying the
/// backfill literal into each `NOT NULL` column new to the rows, and each
/// `(column, from, to)` of `relabels` relabelled as it is copied (a label
/// rename of the same step, ADR-0032).
///
/// # Errors
/// `table` is missing from either side (a rebuild is of a table that exists
/// before and after the file), or its rendering fails.
pub fn render_table(
    table: &str,
    old: &IrEnvelope<SchemaIrPayload>,
    new: &IrEnvelope<SchemaIrPayload>,
    relabels: &[(String, String, String)],
) -> Result<Vec<String>, EmissionError> {
    let find = |ir: &'_ IrEnvelope<SchemaIrPayload>, side: &str| {
        ir.payload
            .models
            .iter()
            .find(|model| model.table_name == table)
            .cloned()
            .ok_or_else(|| EmissionError {
                message: format!(
                    "cannot rebuild table '{table}': it is missing from the {side} snapshot"
                ),
            })
    };
    let (before, after) = (find(old, "old")?, find(new, "new")?);
    let mut values = Vec::new();
    for col in &after.columns {
        if let Some(value) = copy_value(col, &before)? {
            values.push((col.name.clone(), value));
            continue;
        }
        let renames: Vec<(String, String)> = relabels
            .iter()
            .filter(|(column, _, _)| *column == col.name)
            .map(|(_, from, to)| (from.clone(), to.clone()))
            .collect();
        if !renames.is_empty() {
            values.push((col.name.clone(), render_relabel_copy(&col.name, &renames)));
        }
    }
    render(table, &after, &before, &values)
}

/// The value a rebuild copies into `col` of the new table when the old table
/// has no such column: the literal default a `NOT NULL` column new to the
/// rows takes, as `ADD COLUMN`'s backfill `DEFAULT` would give them. `None`
/// for a column the old table holds (copied as it stands) or one with no
/// literal default.
///
/// # Errors
/// The column's storage cannot be resolved.
pub fn copy_value(
    col: &SchemaColumn,
    before: &SchemaModel,
) -> Result<Option<String>, EmissionError> {
    if before.columns.iter().any(|old| old.name == col.name) {
        return Ok(None);
    }
    backfill_value_sql(col, Dialect::Sqlite)
}

/// How SQLite stores a column's values, for deciding whether a rebuild can
/// move them from one declared type to another.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Stored {
    /// `integer`, `smallint`, `bigint`, and `boolean` (SQLite lowers it to
    /// `integer`): INTEGER affinity.
    Integer,
    /// `double`: REAL affinity.
    Real,
    /// `NUMERIC`.
    Decimal,
    /// `text`, `varchar`, `char`: TEXT affinity.
    Text,
    /// `JSON` text.
    Json,
    /// `CHAR(32)` hex.
    Uuid,
    /// ISO text under `DATETIME`, `DATE` or `TIME`.
    Temporal,
    /// `blob`.
    Blob,
}

fn sqlite_canonical(col: &SchemaColumn) -> Result<CanonicalType, EmissionError> {
    match resolve_column_storage(col, Dialect::Sqlite)
        .map_err(|message| EmissionError { message })?
    {
        ResolvedStorage::Scalar(canonical) => Ok(canonical),
        ResolvedStorage::PgEnum { .. } => Err(EmissionError {
            message: format!(
                "column '{}' resolves to a native enum type on SQLite",
                col.name
            ),
        }),
    }
}

fn stored(canonical: CanonicalType) -> Stored {
    match canonical {
        CanonicalType::Integer | CanonicalType::SmallInt | CanonicalType::BigInt => Stored::Integer,
        CanonicalType::Boolean => Stored::Integer,
        CanonicalType::Double => Stored::Real,
        CanonicalType::Decimal => Stored::Decimal,
        CanonicalType::Text | CanonicalType::Varchar(_) | CanonicalType::Char(_) => Stored::Text,
        CanonicalType::Json | CanonicalType::Jsonb => Stored::Json,
        CanonicalType::Uuid => Stored::Uuid,
        CanonicalType::DateTime
        | CanonicalType::Timestamp
        | CanonicalType::TimestampTz
        | CanonicalType::Date
        | CanonicalType::Time => Stored::Temporal,
        CanonicalType::Blob => Stored::Blob,
    }
}

/// The rows a rebuild must refuse after copying `old` into `new` of `table`:
/// a SQL condition over the new table's `new.name` column, true for each
/// value that did not become `new`'s type. `None` when the storage did not
/// change.
///
/// The copy never `CAST`s: SQLite's `CAST` never fails (`CAST('abc' AS
/// integer)` is `0`, `CAST('2026-03-01T15:00:00Z' AS DATETIME)` is `2026`).
/// The value is copied as it stands and the new column's affinity converts
/// it only where the conversion is lossless (`'12'` becomes `12`; `'12abc'`
/// and `1.9` stay what they were), so a value that did not convert is one
/// whose storage class is still wrong, and the step fails on it, as
/// Postgres's `USING` cast fails on the same row.
///
/// # Errors
/// A change SQLite has no conversion for that this check can verify row by
/// row (to or from a timestamp, a UUID, JSON or a blob; into a boolean; a
/// non-integer into a decimal): the refusal names the column and the way to
/// make the change.
pub fn conversion_guard(
    table: &str,
    old: &SchemaColumn,
    new: &SchemaColumn,
) -> Result<Option<String>, EmissionError> {
    let (from, to) = (sqlite_canonical(old)?, sqlite_canonical(new)?);
    if from == to {
        return Ok(None);
    }
    let col = quote_ident(&new.name);
    let class_is = |classes: &str| format!("typeof({col}) NOT IN ({classes}, 'null')");
    // SQLite stores a boolean as an integer, so its storage cannot tell one
    // from the other; the declaration can. Postgres has no `boolean` to
    // `double` cast and renders `boolean::text` as `'true'`, where SQLite's
    // copy would keep `1`.
    let boolean = |c: &SchemaColumn| {
        matches!(
            resolve_column_storage(c, Dialect::Postgres),
            Ok(ResolvedStorage::Scalar(CanonicalType::Boolean))
        )
    };
    let guard = match (stored(from), stored(to)) {
        _ if boolean(old) || boolean(new) => None,
        (Stored::Integer | Stored::Real | Stored::Decimal | Stored::Text, Stored::Integer) => {
            Some(class_is("'integer'"))
        }
        (Stored::Integer | Stored::Real | Stored::Decimal | Stored::Text, Stored::Real) => {
            Some(class_is("'real'"))
        }
        (Stored::Integer, Stored::Decimal) => Some(class_is("'integer'")),
        (
            Stored::Integer | Stored::Real | Stored::Decimal | Stored::Text | Stored::Temporal,
            Stored::Text,
        ) => Some(class_is("'text'")),
        // Everything else, JSON included: SQLite's `JSON` column has NUMERIC
        // affinity, so `'00123'` would land as `123` and still be valid JSON.
        _ => None,
    };
    guard.map(Some).ok_or_else(|| EmissionError {
        message: format!(
            "a SQLite rebuild cannot change {table}.{} from {} to {}: SQLite has no \
             conversion between them that ferro can check row by row, so the copy could \
             rewrite values silently. Add the new column beside the old one, fill it in a \
             data step, then drop the old column",
            new.name,
            sqlite_declared_type(from),
            sqlite_declared_type(to)
        ),
    })
}

/// The temporary table a rebuild's conversion check inserts each refused
/// row into; its one constraint always fails, naming the column and type.
const GUARD_TABLE: &str = "_ferro_rebuild_guard";

/// The statements that fail the step when a row of `new_table` holds a value
/// `bad_rows` (a [`conversion_guard`] condition) refuses: `CHECK` failing as
/// `ferro: <table>.<col> has a value that cannot become <type>`.
fn guard_statements(
    table: &str,
    new_table: &str,
    col: &SchemaColumn,
    bad_rows: &str,
) -> Result<Vec<String>, EmissionError> {
    let guard = quote_ident(GUARD_TABLE);
    let target = sqlite_declared_type(sqlite_canonical(col)?);
    Ok(vec![
        format!(
            "CREATE TEMP TABLE {guard} (\"value\", CONSTRAINT {} CHECK (0))",
            quote_ident(&format!(
                "ferro: {table}.{} has a value that cannot become {target}",
                col.name
            ))
        ),
        format!(
            "INSERT INTO {guard} (\"value\") SELECT {} FROM {} WHERE {bad_rows}",
            quote_ident(&col.name),
            quote_ident(new_table)
        ),
        format!("DROP TABLE {guard}"),
    ])
}

/// Whether the create pass writes `model`'s primary key `INTEGER PRIMARY KEY
/// AUTOINCREMENT` on SQLite: a primary-key column flagged autoincrement, the
/// flags [`render_create_table_as`] reads.
fn is_autoincrement(model: &SchemaModel) -> bool {
    model
        .columns
        .iter()
        .any(|col| col.primary_key && col.autoincrement)
}

/// The statements that hand `table`'s `AUTOINCREMENT` high-water mark to
/// `new_table` before `table` is dropped (dropping it deletes its
/// `sqlite_sequence` row): without them the new table's sequence would
/// restart at the largest id copied, and the id of a row deleted from the
/// end would be handed out again, which `AUTOINCREMENT` exists to prevent.
/// The copy may or may not have written `new_table` a row, and
/// `sqlite_sequence` has no key to replace on, so the row is deleted and
/// written again from `table`'s; `table`'s mark is at least every id it
/// holds, and with no row for `table` (it was not `AUTOINCREMENT`) SQLite
/// starts after the largest id, as it would have. `ALTER TABLE … RENAME`
/// then renames the row with the table.
fn carry_sequence(table: &str, new_table: &str) -> [String; 2] {
    let literal = |name: &str| format!("'{}'", name.replace('\'', "''"));
    [
        format!(
            "DELETE FROM sqlite_sequence WHERE name = {}",
            literal(new_table)
        ),
        format!(
            "INSERT INTO sqlite_sequence (name, seq) SELECT {}, seq FROM sqlite_sequence \
             WHERE name = {}",
            literal(new_table),
            literal(table)
        ),
    ]
}

/// The statements that rebuild `table` from `shape_before` into
/// `shape_after` on SQLite: `CREATE TABLE "_ferro_new_<table>"` as the create
/// pass writes `shape_after`; `INSERT … SELECT` of every column both shapes
/// hold, as it stands, and of each column `casts` names (`(column,
/// sql_expr)`: the value to copy, such as the backfill literal for a new
/// `NOT NULL` column); for each column whose type changed, a check that
/// every copied value became the new type ([`conversion_guard`]); for an
/// `AUTOINCREMENT` table, its sequence carried to the new one
/// ([`carry_sequence`]); `DROP TABLE`; the rename; then `shape_after`'s
/// ferro-owned indexes as the create pass builds them.
///
/// A column only `shape_after` holds and `casts` does not name gets no
/// value: `NULL`, which a `NOT NULL` column refuses on a populated table
/// (ADR-0033: a down restores schema, never data).
///
/// # Errors
/// `shape_after` does not render (an unresolvable column type), names a
/// table other than `table`, or changes a column's type in a way SQLite
/// cannot verify ([`conversion_guard`]).
pub fn render(
    table: &str,
    shape_after: &SchemaModel,
    shape_before: &SchemaModel,
    casts: &[(String, String)],
) -> Result<Vec<String>, EmissionError> {
    if shape_after.table_name != table || shape_before.table_name != table {
        return Err(EmissionError {
            message: format!(
                "cannot rebuild table '{table}' from '{}' into '{}'",
                shape_before.table_name, shape_after.table_name
            ),
        });
    }
    let new_table = new_table_name(table);
    let emission = render_create_table_as(shape_after, Dialect::Sqlite, Some(&new_table))?;
    let mut targets = Vec::new();
    let mut values = Vec::new();
    let mut guards = Vec::new();
    for col in &shape_after.columns {
        let cast = casts.iter().find(|(name, _)| name == &col.name);
        let old = shape_before.columns.iter().find(|old| old.name == col.name);
        let value = match (cast, old) {
            (Some((_, expr)), _) => expr.clone(),
            (None, Some(old)) => {
                if let Some(bad_rows) = conversion_guard(table, old, col)? {
                    guards.extend(guard_statements(table, &new_table, col, &bad_rows)?);
                }
                quote_ident(&col.name)
            }
            (None, None) => continue,
        };
        targets.push(quote_ident(&col.name));
        values.push(value);
    }
    let mut out = emission.pre_create_sqls;
    out.push(emission.create_sql);
    if !targets.is_empty() {
        out.push(format!(
            "INSERT INTO {} ({}) SELECT {} FROM {}",
            quote_ident(&new_table),
            targets.join(", "),
            values.join(", "),
            quote_ident(table)
        ));
    }
    out.extend(guards);
    if is_autoincrement(shape_after) {
        out.extend(carry_sequence(table, &new_table));
    }
    out.push(format!("DROP TABLE {}", quote_ident(table)));
    out.push(format!(
        "ALTER TABLE {} RENAME TO {}",
        quote_ident(&new_table),
        quote_ident(table)
    ));
    out.extend(emission.post_create_sqls);
    Ok(out)
}

/// The tables a step file rebuilds, from its statements: every
/// `CREATE TABLE [IF NOT EXISTS] "_ferro_new_<table>"` [`render`] writes, in
/// order. What the runner checks the live database against before such a
/// step runs.
pub fn rebuilt_tables(statements: &[String]) -> Vec<String> {
    let mut out = Vec::new();
    for statement in statements {
        // A file's first statement carries its header lines.
        let code = statement
            .lines()
            .skip_while(|line| line.trim().is_empty() || line.trim_start().starts_with("--"))
            .collect::<Vec<_>>()
            .join("\n");
        let Some(rest) = code.strip_prefix("CREATE TABLE ") else {
            continue;
        };
        let rest = rest.strip_prefix("IF NOT EXISTS ").unwrap_or(rest);
        let Some(rest) = rest.strip_prefix('"') else {
            continue;
        };
        let Some((name, _)) = rest.split_once('"') else {
            continue;
        };
        if let Some(table) = name.strip_prefix(NEW_TABLE_PREFIX)
            && !table.is_empty()
            && !out.iter().any(|seen| seen == table)
        {
            out.push(table.to_string());
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::super::tests::{column, ir, model, pk};
    use super::*;
    use crate::MigrationOp;
    use crate::render_create_table;
    use ferro_schema_ir::{SchemaCheck, SchemaForeignKey, SchemaIndex, SchemaUnique};

    fn nullable(name: &str, logical_type: &str) -> SchemaColumn {
        SchemaColumn {
            nullable: true,
            ..column(name, logical_type)
        }
    }

    fn author(extra: Vec<SchemaColumn>) -> SchemaModel {
        let mut columns = vec![pk(), column("name", "string")];
        columns.extend(extra);
        model("Author", columns)
    }

    fn with_fk(mut model: SchemaModel, col: &str) -> SchemaModel {
        model.foreign_keys.push(SchemaForeignKey {
            renamed_from: None,
            column: col.into(),
            to_table: "team".into(),
            to_column: "id".into(),
            on_delete: Some("CASCADE".into()),
            name: Some(format!("fk_author_{col}_team")),
        });
        model
    }

    fn checked(mut model: SchemaModel, col: &str) -> SchemaModel {
        model.checks.push(SchemaCheck {
            name: format!("ck_author_{col}"),
            column: col.into(),
            values: vec!["'a'".into()],
        });
        model
    }

    /// Whether `op`'s verdict between `before` and `after` is a rebuild.
    fn rebuilds(
        op: &MigrationOp,
        before: &SchemaModel,
        after: &SchemaModel,
        dialect: Dialect,
    ) -> bool {
        let planned = PlannedOp::of(
            op.clone(),
            &crate::Side::declared(ir(vec![before.clone()])),
            &crate::Side::declared(ir(vec![after.clone()])),
            dialect,
        );
        planned.verdict.execution == Execution::Rebuild
    }

    fn t() -> String {
        "author".to_string()
    }

    /// Every op the planner emits, with the table shapes it is decided
    /// against and whether SQLite rebuilds it: one answer, whichever way a
    /// file runs it (ADR-0050).
    fn the_table() -> Vec<(MigrationOp, SchemaModel, SchemaModel, bool)> {
        let plain = author(vec![]);
        let bio = author(vec![nullable("bio", "string")]);
        let req_bio = author(vec![column("bio", "string")]);
        let defaulted = author(vec![SchemaColumn {
            default: Some(serde_json::json!("free")),
            ..column("bio", "string")
        }]);
        let opt_fk = with_fk(author(vec![nullable("team_id", "integer")]), "team_id");
        let req_fk = with_fk(
            author(vec![SchemaColumn {
                default: Some(serde_json::json!(1)),
                ..column("team_id", "integer")
            }]),
            "team_id",
        );
        let age_int = author(vec![nullable("age", "integer")]);
        let age_text = author(vec![nullable("age", "string")]);
        let checked_bio = checked(bio.clone(), "bio");
        let indexed = SchemaModel {
            indexes: vec![SchemaIndex {
                name: "idx_author_bio".into(),
                columns: vec!["bio".into()],
                unique: false,
            }],
            ..bio.clone()
        };
        let add = |c: &str| MigrationOp::AddColumn {
            table: t(),
            column: c.into(),
        };
        let drop = |c: &str| MigrationOp::DropColumn {
            table: t(),
            column: c.into(),
        };
        vec![
            (
                MigrationOp::AddTable { table: t() },
                plain.clone(),
                plain.clone(),
                false,
            ),
            (
                MigrationOp::DropTable { table: t() },
                plain.clone(),
                plain.clone(),
                false,
            ),
            (
                MigrationOp::CreateEnumType {
                    type_name: "status".into(),
                    labels: vec![],
                },
                plain.clone(),
                plain.clone(),
                false,
            ),
            (
                MigrationOp::DropEnumType {
                    type_name: "status".into(),
                },
                plain.clone(),
                plain.clone(),
                false,
            ),
            (
                MigrationOp::AddEnumLabel {
                    type_name: "status".into(),
                    label: "x".into(),
                },
                plain.clone(),
                plain.clone(),
                false,
            ),
            // A1; A2: `ADD COLUMN … NOT NULL DEFAULT` would keep the
            // DEFAULT ferro never persists (ADR-0027), so the rebuild copies
            // the literal instead.
            (add("bio"), plain.clone(), bio.clone(), false),
            (add("bio"), plain.clone(), defaulted.clone(), true),
            // A3: SQLite has no `SET NOT NULL` to reach it (an up answers it
            // with a backfill first; the down of an A4 drop rebuilds).
            (add("bio"), plain.clone(), req_bio.clone(), true),
            // A1 with a foreign key: inline REFERENCES.
            (add("team_id"), plain.clone(), opt_fk.clone(), false),
            // A required foreign-key column: a required column.
            (add("team_id"), plain.clone(), req_fk.clone(), true),
            // A4 plain, nullable or not.
            (drop("bio"), bio.clone(), plain.clone(), false),
            (drop("bio"), req_bio.clone(), plain.clone(), false),
            // C2, and the down of A1-with-FK.
            (drop("team_id"), opt_fk.clone(), plain.clone(), true),
            // A6.
            (
                MigrationOp::AlterColumnType {
                    table: t(),
                    column: "age".into(),
                },
                age_int.clone(),
                age_text.clone(),
                true,
            ),
            // A7a, A7b.
            (
                MigrationOp::AlterColumnNullability {
                    table: t(),
                    column: "bio".into(),
                },
                req_bio.clone(),
                bio.clone(),
                true,
            ),
            (
                // SQLite would rebuild it, but its verdict is the primary-key
                // refusal, which no door runs.
                MigrationOp::ChangePrimaryKey {
                    table: t(),
                    from: vec!["id".into()],
                    to: vec!["name".into()],
                },
                plain.clone(),
                plain.clone(),
                false,
            ),
            // A9.
            (
                MigrationOp::AddIndex {
                    table: t(),
                    name: "idx_author_bio".into(),
                    columns: vec!["bio".into()],
                    unique: false,
                },
                bio.clone(),
                indexed.clone(),
                false,
            ),
            (
                MigrationOp::DropIndex {
                    table: t(),
                    name: "idx_author_bio".into(),
                },
                indexed.clone(),
                bio.clone(),
                false,
            ),
            (
                MigrationOp::RebuildIndex {
                    table: t(),
                    name: "idx_author_bio".into(),
                    columns: vec!["bio".into()],
                    unique: false,
                },
                indexed.clone(),
                indexed.clone(),
                false,
            ),
            // A10 add, change, drop.
            (
                MigrationOp::AddCheck {
                    table: t(),
                    name: "ck_author_bio".into(),
                },
                bio.clone(),
                checked_bio.clone(),
                true,
            ),
            (
                MigrationOp::RebuildCheck {
                    table: t(),
                    name: "ck_author_bio".into(),
                },
                checked_bio.clone(),
                checked_bio.clone(),
                true,
            ),
            (
                MigrationOp::DropCheck {
                    table: t(),
                    name: "ck_author_bio".into(),
                },
                checked_bio.clone(),
                bio.clone(),
                true,
            ),
            // A dropped column's own check goes with its DROP COLUMN.
            (
                MigrationOp::DropCheck {
                    table: t(),
                    name: "ck_author_bio".into(),
                },
                checked_bio.clone(),
                plain.clone(),
                false,
            ),
            // C3, and a foreign key added to an existing column.
            (
                MigrationOp::AddForeignKey {
                    table: t(),
                    column: "team_id".into(),
                },
                author(vec![nullable("team_id", "integer")]),
                opt_fk.clone(),
                true,
            ),
            (
                MigrationOp::RebuildForeignKey {
                    table: t(),
                    column: "team_id".into(),
                    old_name: "fk_author_team_id_team".into(),
                },
                opt_fk.clone(),
                opt_fk.clone(),
                true,
            ),
            (
                MigrationOp::ValidateConstraint {
                    table: t(),
                    name: "ck_author_bio".into(),
                },
                checked_bio.clone(),
                checked_bio.clone(),
                false,
            ),
            (
                MigrationOp::AddRowPolicy {
                    table: t(),
                    name: "p".into(),
                },
                plain.clone(),
                plain.clone(),
                false,
            ),
            (
                MigrationOp::RebuildRowPolicy {
                    table: t(),
                    name: "p".into(),
                },
                plain.clone(),
                plain.clone(),
                false,
            ),
            (
                MigrationOp::DropRowPolicy {
                    table: t(),
                    name: "p".into(),
                },
                plain.clone(),
                plain.clone(),
                false,
            ),
            (
                MigrationOp::EnableRowSecurity { table: t() },
                plain.clone(),
                plain.clone(),
                false,
            ),
            (
                MigrationOp::ForceRowSecurity { table: t() },
                plain.clone(),
                plain.clone(),
                false,
            ),
            (
                MigrationOp::DisableRowSecurity { table: t() },
                plain.clone(),
                plain.clone(),
                false,
            ),
            (
                MigrationOp::NoForceRowSecurity { table: t() },
                plain.clone(),
                plain,
                false,
            ),
        ]
    }

    #[test]
    fn the_native_or_rebuild_verdict_for_every_op() {
        let table = the_table();
        // Every planner op kind is pinned.
        let kinds: std::collections::BTreeSet<String> = table
            .iter()
            .map(|(op, ..)| super::super::op_kind(op))
            .collect();
        assert_eq!(kinds.len(), 26, "{kinds:?}");
        for (op, before, after, expected) in table {
            assert_eq!(
                rebuilds(&op, &before, &after, Dialect::Sqlite),
                expected,
                "{op:?}"
            );
            assert!(
                !rebuilds(&op, &before, &after, Dialect::Postgres),
                "Postgres never rebuilds: {op:?}"
            );
        }
    }

    #[test]
    fn a_type_change_rebuild_is_the_create_pass_table_a_cast_copy_and_its_indexes() {
        let mut before = author(vec![
            nullable("age", "integer"),
            nullable("email", "string"),
        ]);
        before.uniques.push(SchemaUnique {
            name: "uq_author_email".into(),
            columns: vec!["email".into()],
        });
        let mut after = before.clone();
        after.columns[2] = nullable("age", "string");
        let statements = render("author", &after, &before, &[]).expect("render");
        let create = render_create_table(&after, Dialect::Sqlite).expect("create");
        // The I-1 pin: byte-identical to the create pass apart from the name.
        assert_eq!(
            statements[0],
            create
                .create_sql
                .replacen("\"author\"", "\"_ferro_new_author\"", 1)
        );
        assert!(statements[0].starts_with("CREATE TABLE IF NOT EXISTS \"_ferro_new_author\" ("));
        assert_eq!(
            statements[1],
            "INSERT INTO \"_ferro_new_author\" (\"id\", \"name\", \"age\", \"email\") \
             SELECT \"id\", \"name\", \"age\", \"email\" FROM \"author\""
        );
        assert_eq!(
            statements[7..9],
            [
                "DROP TABLE \"author\"".to_string(),
                "ALTER TABLE \"_ferro_new_author\" RENAME TO \"author\"".to_string(),
            ]
        );
        assert_eq!(statements[9..], create.post_create_sqls[..]);
        assert_eq!(
            statements[9],
            "CREATE UNIQUE INDEX IF NOT EXISTS \"uq_author_email\" ON \"author\" (\"email\")"
        );
        assert_eq!(rebuilt_tables(&statements), ["author"]);
    }

    #[test]
    fn a_rebuild_keeps_every_constraint_name_and_reference_on_the_real_table() {
        let mut after = checked(
            with_fk(
                author(vec![
                    nullable("team_id", "integer"),
                    nullable("bio", "string"),
                ]),
                "team_id",
            ),
            "bio",
        );
        // A self-reference names the table the new one is renamed to.
        after.foreign_keys.push(SchemaForeignKey {
            renamed_from: None,
            column: "mentor_id".into(),
            to_table: "author".into(),
            to_column: "id".into(),
            on_delete: Some("SET NULL".into()),
            name: Some("fk_author_mentor_id_author".into()),
        });
        after.columns.push(nullable("mentor_id", "integer"));
        let statements = render("author", &after, &after, &[]).expect("render");
        let create = render_create_table(&after, Dialect::Sqlite).expect("create");
        assert_eq!(
            statements[0],
            create
                .create_sql
                .replacen("\"author\"", "\"_ferro_new_author\"", 1)
        );
        for kept in [
            "CONSTRAINT \"fk_author_team_id_team\"",
            "CONSTRAINT \"ck_author_bio\"",
            "CONSTRAINT \"fk_author_mentor_id_author\" FOREIGN KEY (\"mentor_id\") REFERENCES \"author\"",
        ] {
            assert!(statements[0].contains(kept), "{kept}: {}", statements[0]);
        }
    }

    #[test]
    fn a_column_only_the_new_shape_holds_copies_its_value_or_nothing() {
        let before = author(vec![]);
        let after = author(vec![nullable("bio", "string"), column("slug", "string")]);
        let statements =
            render("author", &after, &before, &[("slug".into(), "'x'".into())]).expect("render");
        assert_eq!(
            statements[1],
            "INSERT INTO \"_ferro_new_author\" (\"id\", \"name\", \"slug\") \
             SELECT \"id\", \"name\", 'x' FROM \"author\""
        );
        let required_fk = SchemaColumn {
            default: Some(serde_json::json!(1)),
            ..column("team_id", "integer")
        };
        assert_eq!(
            copy_value(&required_fk, &before).expect("value"),
            Some("1".into())
        );
        assert_eq!(
            copy_value(&column("name", "integer"), &before).expect("value"),
            None,
            "a column the old table holds is copied as it stands, never CAST"
        );
        assert_eq!(
            copy_value(&nullable("bio", "string"), &before).expect("value"),
            None
        );
    }

    /// `conversion_guard` from a column of `from` to one of `to`.
    fn guard(from: SchemaColumn, to: SchemaColumn) -> Result<Option<String>, String> {
        conversion_guard("author", &from, &to).map_err(|err| err.message)
    }

    fn typed(logical: &str, format: Option<&str>) -> SchemaColumn {
        SchemaColumn {
            format: format.map(str::to_string),
            ..nullable("x", logical)
        }
    }

    /// A column `conversion_guard` sees as each storage, two for the
    /// temporal storage (different declared types). The match is exhaustive,
    /// so a new storage cannot be added without a representative here.
    fn representatives(kind: Stored) -> Vec<(&'static str, SchemaColumn)> {
        match kind {
            Stored::Integer => vec![
                ("integer", typed("integer", None)),
                ("boolean", typed("boolean", None)),
            ],
            Stored::Real => vec![("real", typed("number", None))],
            Stored::Decimal => vec![("decimal", typed("string", Some("decimal")))],
            Stored::Text => vec![("text", typed("string", None))],
            Stored::Json => vec![("json", typed("json", None))],
            Stored::Uuid => vec![("uuid", typed("uuid", None))],
            Stored::Temporal => vec![
                ("datetime", typed("datetime", None)),
                ("date", typed("date", None)),
            ],
            Stored::Blob => vec![("blob", typed("binary", None))],
        }
    }

    const STORAGES: [Stored; 8] = [
        Stored::Integer,
        Stored::Real,
        Stored::Decimal,
        Stored::Text,
        Stored::Json,
        Stored::Uuid,
        Stored::Temporal,
        Stored::Blob,
    ];

    /// Every change a rebuild copies and checks, `(from, to, storage class
    /// each copied value must have)`. Every other change between different
    /// columns is refused.
    const CHECKED: [(&str, &str, &str); 12] = [
        ("real", "integer", "integer"),
        ("decimal", "integer", "integer"),
        ("text", "integer", "integer"),
        ("integer", "real", "real"),
        ("decimal", "real", "real"),
        ("text", "real", "real"),
        ("integer", "decimal", "integer"),
        ("integer", "text", "text"),
        ("real", "text", "text"),
        ("decimal", "text", "text"),
        ("datetime", "text", "text"),
        ("date", "text", "text"),
    ];

    #[test]
    fn the_conversion_check_for_every_storage_pair() {
        let columns: Vec<(&str, SchemaColumn)> =
            STORAGES.into_iter().flat_map(representatives).collect();
        for kind in STORAGES {
            for (_, col) in representatives(kind) {
                let canonical = sqlite_canonical(&col).expect("resolves");
                assert_eq!(stored(canonical), kind, "{col:?}");
            }
        }
        assert_eq!(columns.len(), 10);
        // A boolean and an integer column are the same column on SQLite.
        let same = |a: &str, b: &str| {
            a == b || matches!((a, b), ("integer", "boolean") | ("boolean", "integer"))
        };
        for (from_name, from) in &columns {
            for (to_name, to) in &columns {
                let verdict = guard(from.clone(), to.clone());
                if same(from_name, to_name) {
                    assert_eq!(verdict, Ok(None), "{from_name} → {to_name}");
                    continue;
                }
                match CHECKED
                    .iter()
                    .find(|(f, t, _)| f == from_name && t == to_name)
                {
                    Some((_, _, class)) => assert_eq!(
                        verdict,
                        Ok(Some(format!("typeof(\"x\") NOT IN ('{class}', 'null')"))),
                        "{from_name} → {to_name}"
                    ),
                    None => {
                        let err = verdict.expect_err(&format!("{from_name} → {to_name} refused"));
                        assert!(
                            err.starts_with("a SQLite rebuild cannot change author.x from "),
                            "{err}"
                        );
                        assert!(err.contains("fill it in a data step"), "{err}");
                    }
                }
            }
        }
    }

    #[test]
    fn a_type_change_copies_the_value_as_it_stands_and_checks_it_converted() {
        let before = author(vec![nullable("age", "string")]);
        let after = author(vec![nullable("age", "integer")]);
        let statements = render("author", &after, &before, &[]).expect("render");
        assert_eq!(
            statements[1..9],
            [
                "INSERT INTO \"_ferro_new_author\" (\"id\", \"name\", \"age\") \
                 SELECT \"id\", \"name\", \"age\" FROM \"author\""
                    .to_string(),
                "CREATE TEMP TABLE \"_ferro_rebuild_guard\" (\"value\", CONSTRAINT \
                 \"ferro: author.age has a value that cannot become integer\" CHECK (0))"
                    .to_string(),
                "INSERT INTO \"_ferro_rebuild_guard\" (\"value\") SELECT \"age\" FROM \
                 \"_ferro_new_author\" WHERE typeof(\"age\") NOT IN ('integer', 'null')"
                    .to_string(),
                "DROP TABLE \"_ferro_rebuild_guard\"".to_string(),
                "DELETE FROM sqlite_sequence WHERE name = '_ferro_new_author'".to_string(),
                "INSERT INTO sqlite_sequence (name, seq) SELECT '_ferro_new_author', seq \
                 FROM sqlite_sequence WHERE name = 'author'"
                    .to_string(),
                "DROP TABLE \"author\"".to_string(),
                "ALTER TABLE \"_ferro_new_author\" RENAME TO \"author\"".to_string(),
            ]
        );
        assert!(!statements.iter().any(|s| s.contains("CAST(")));
        let refused = render(
            "author",
            &author(vec![typed("datetime", None)]),
            &author(vec![typed("string", None)]),
            &[],
        )
        .expect_err("refused");
        assert!(
            refused
                .message
                .contains("author.x from varchar to DATETIME"),
            "{}",
            refused.message
        );
    }

    #[test]
    fn an_autoincrement_tables_rebuild_carries_its_sequence_and_a_plain_ones_does_not() {
        // `INTEGER PRIMARY KEY AUTOINCREMENT` never hands out an id again:
        // the new table takes the old one's high-water mark before the old
        // table (and its `sqlite_sequence` row) is dropped; the rename then
        // moves the row to the real name.
        let before = author(vec![nullable("age", "integer")]);
        let mut after = before.clone();
        after.columns[2] = nullable("age", "string");
        let statements = render("author", &after, &before, &[]).expect("render");
        let carry = [
            "DELETE FROM sqlite_sequence WHERE name = '_ferro_new_author'".to_string(),
            "INSERT INTO sqlite_sequence (name, seq) SELECT '_ferro_new_author', seq \
             FROM sqlite_sequence WHERE name = 'author'"
                .to_string(),
            "DROP TABLE \"author\"".to_string(),
        ];
        let drop = statements
            .iter()
            .position(|s| s == "DROP TABLE \"author\"")
            .expect("drop");
        assert_eq!(statements[drop - 2..=drop], carry);
        assert!(statements[0].contains("AUTOINCREMENT"), "{}", statements[0]);

        // A plain `INTEGER PRIMARY KEY` has no sequence row to carry.
        let plain = |mut model: SchemaModel| {
            model.columns[0].autoincrement = false;
            model
        };
        let statements = render("author", &plain(after), &plain(before), &[]).expect("render");
        assert!(
            !statements[0].contains("AUTOINCREMENT"),
            "{}",
            statements[0]
        );
        assert!(
            statements.iter().all(|s| !s.contains("sqlite_sequence")),
            "{statements:?}"
        );
    }

    #[test]
    fn a_rebuild_of_another_table_is_refused() {
        let err = render("post", &author(vec![]), &author(vec![]), &[]).expect_err("refused");
        assert!(
            err.message.contains("cannot rebuild table 'post'"),
            "{}",
            err.message
        );
    }

    #[test]
    fn the_rebuilt_tables_are_read_back_from_the_statements() {
        let statements = vec![
            "-- ferro: foreign-keys-off\n-- ferro: data-dependent\n\nCREATE TABLE IF NOT EXISTS \
             \"_ferro_new_author\" ( \"id\" integer )"
                .to_string(),
            "CREATE TABLE \"_ferro_new_post\" ( \"id\" integer )".to_string(),
            "CREATE TABLE IF NOT EXISTS \"tag\" ( \"id\" integer )".to_string(),
            "INSERT INTO \"_ferro_new_author\" SELECT 1".to_string(),
        ];
        assert_eq!(rebuilt_tables(&statements), ["author", "post"]);
        assert!(rebuilt_tables(&[]).is_empty());
    }
}
