//! The SQLite table rebuild (ADR-0034, ADR-0046): every change SQLite's
//! `ALTER TABLE` cannot express, rendered as the table built again in its new
//! shape under a temporary name, its rows copied across, and swapped in.
//!
//! ```text
//! class Author(Model):          0004_author_age_text/01_schema.up.sqlite.sql
//!     age: str  # was int  ──▶  -- ferro: foreign-keys-off
//!                               -- ferro: data-dependent
//!                               CREATE TABLE IF NOT EXISTS "_ferro_new_author" (…, "age" varchar NOT NULL );
//!                               INSERT INTO "_ferro_new_author" ("id", …, "age") SELECT "id", …, CAST("age" AS varchar) FROM "author";
//!                               DROP TABLE "author";
//!                               ALTER TABLE "_ferro_new_author" RENAME TO "author";
//!                               CREATE UNIQUE INDEX IF NOT EXISTS "uq_author_name" ON "author" ("name");
//! ```
//!
//! [`needs_rebuild`] is the one table deciding, per op and per direction,
//! whether SQLite runs an op natively or only through a rebuild; [`render`]
//! writes the rebuild. The `CREATE TABLE` is the create pass's own
//! ([`crate::emit::render_create_table_as`]) for the shape the step leaves,
//! byte for byte apart from the table's name, and the indexes after the
//! rename are the create pass's own statements (AGENTS.md § I-1). The file
//! holds only these statements: the `foreign-keys-off` header hands the
//! pragma, the transaction and `PRAGMA foreign_key_check` to the runner.

use super::columns::{PlanContext, PlanDirection, goes_with_a_dropped_column, needs_values};
use crate::emit::{backfill_value_sql, render_create_table_as};
use crate::{Dialect, EmissionError, MigrationOp};
use ferro_ddl_lowering::{
    ResolvedStorage, quote_ident, resolve_column_storage, sqlite_declared_type,
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

fn has_foreign_key(model: Option<&SchemaModel>, column: &str) -> bool {
    model.is_some_and(|model| model.foreign_keys.iter().any(|fk| fk.column == column))
}

fn column<'a>(model: Option<&'a SchemaModel>, name: &str) -> Option<&'a SchemaColumn> {
    model?.columns.iter().find(|col| col.name == name)
}

/// Whether SQLite can run `op` only through a table rebuild, in the file
/// going `direction` that `ctx` describes. Always `false` on Postgres, which
/// never rebuilds.
///
/// | Op | Up | Down |
/// | :-- | :-- | :-- |
/// | add/drop a table, an enum type or label, an index | native | native |
/// | add an optional column, or a required one with a literal default | native | native |
/// | add a required column with no default | (backfill) | rebuild |
/// | add a required foreign-key column (SQLite's `REFERENCES` needs a NULL default) | rebuild | rebuild |
/// | drop a plain column | native | native |
/// | drop a foreign-key column | rebuild | rebuild |
/// | change a column's type or nullability | rebuild | rebuild |
/// | add, change or drop a check (but a dropped column's own) | rebuild | rebuild |
/// | add or retarget a foreign key | rebuild | rebuild |
/// | change the primary key | rebuild | rebuild |
/// | validate a constraint, rebuild an invalid index, row security | native (nothing on SQLite) | native |
///
/// A foreign-key column comes off natively only while it is the inline
/// `REFERENCES` column `ADD COLUMN` wrote; once any rebuild has written the
/// table (as `FOREIGN KEY (…)`, `CREATE TABLE`'s shape) SQLite refuses to
/// drop it in place, and a down cannot know which shape it finds, so the
/// drop is always a rebuild.
pub fn needs_rebuild(op: &MigrationOp, direction: PlanDirection, ctx: &PlanContext<'_>) -> bool {
    if ctx.dialect != Dialect::Sqlite {
        return false;
    }
    match op {
        MigrationOp::AddTable { .. }
        | MigrationOp::DropTable { .. }
        | MigrationOp::CreateEnumType { .. }
        | MigrationOp::DropEnumType { .. }
        | MigrationOp::AddEnumLabel { .. }
        | MigrationOp::AddIndex { .. }
        | MigrationOp::DropIndex { .. }
        | MigrationOp::RebuildIndex { .. }
        | MigrationOp::ValidateConstraint { .. }
        | MigrationOp::AddRowPolicy { .. }
        | MigrationOp::RebuildRowPolicy { .. }
        | MigrationOp::DropRowPolicy { .. }
        | MigrationOp::EnableRowSecurity { .. }
        | MigrationOp::ForceRowSecurity { .. }
        | MigrationOp::DisableRowSecurity { .. }
        | MigrationOp::NoForceRowSecurity { .. } => false,
        MigrationOp::AddColumn { column: name, .. } => {
            let Some(col) = column(ctx.after, name) else {
                return false;
            };
            if needs_values(col) {
                // Going up the rows need values first (a backfill); a down
                // putting the column back has no `SET NOT NULL` to reach it.
                direction == PlanDirection::Down
            } else {
                !col.nullable && has_foreign_key(ctx.after, name)
            }
        }
        MigrationOp::DropColumn { column: name, .. } => has_foreign_key(ctx.before, name),
        MigrationOp::AlterColumnType { .. }
        | MigrationOp::AlterColumnNullability { .. }
        | MigrationOp::ChangePrimaryKey { .. }
        | MigrationOp::AddCheck { .. }
        | MigrationOp::RebuildCheck { .. }
        | MigrationOp::AddForeignKey { .. }
        | MigrationOp::RebuildForeignKey { .. } => true,
        MigrationOp::DropCheck { .. } => !goes_with_a_dropped_column(op, ctx),
    }
}

/// The tables a file going `direction` from `old` to `new` rebuilds on
/// `dialect`: each table one of `ops` needs a rebuild for. Every other op on
/// such a table folds into its one rebuild (ADR-0046: one copy per table per
/// phase step).
pub fn tables_to_rebuild(
    ops: &[MigrationOp],
    old: &IrEnvelope<SchemaIrPayload>,
    new: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
    direction: PlanDirection,
) -> BTreeSet<String> {
    ops.iter()
        .filter(|op| {
            let ctx = PlanContext::of(op, old, new, dialect, direction);
            needs_rebuild(op, direction, &ctx)
        })
        .filter_map(|op| op.table().map(str::to_string))
        .collect()
}

/// [`render`] for `table` in a file turning `old` into `new`, the values
/// copied from `ops`: a `CAST` for each column an `AlterColumnType` of `ops`
/// changes, the backfill literal for each `NOT NULL` column new to the rows.
///
/// # Errors
/// `table` is missing from either side (a rebuild is of a table that exists
/// before and after the file), or its rendering fails.
pub fn render_table(
    table: &str,
    ops: &[MigrationOp],
    old: &IrEnvelope<SchemaIrPayload>,
    new: &IrEnvelope<SchemaIrPayload>,
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
    let mut casts = Vec::new();
    for col in &after.columns {
        let type_changed = ops.iter().any(|op| {
            matches!(op, MigrationOp::AlterColumnType { table: t, column }
                if t == table && column == &col.name)
        });
        if let Some(value) = copy_value(col, &before, type_changed)? {
            casts.push((col.name.clone(), value));
        }
    }
    render(table, &after, &before, &casts)
}

/// The value a rebuild copies into `col` of the new table, when it is not
/// the old column as it stands: a type change's `CAST`, or the literal
/// default a `NOT NULL` column new to the rows takes (as `ADD COLUMN`'s
/// backfill `DEFAULT` would give them).
///
/// # Errors
/// The column's storage cannot be resolved.
pub fn copy_value(
    col: &SchemaColumn,
    before: &SchemaModel,
    type_changed: bool,
) -> Result<Option<String>, EmissionError> {
    if before.columns.iter().any(|old| old.name == col.name) {
        if !type_changed {
            return Ok(None);
        }
        let storage = resolve_column_storage(col, Dialect::Sqlite)
            .map_err(|message| EmissionError { message })?;
        let ResolvedStorage::Scalar(canonical) = storage else {
            return Err(EmissionError {
                message: format!(
                    "column '{}' resolves to a native enum type on SQLite",
                    col.name
                ),
            });
        };
        return Ok(Some(format!(
            "CAST({} AS {})",
            quote_ident(&col.name),
            sqlite_declared_type(canonical)
        )));
    }
    backfill_value_sql(col, Dialect::Sqlite)
}

/// The statements that rebuild `table` from `shape_before` into
/// `shape_after` on SQLite: `CREATE TABLE "_ferro_new_<table>"` as the create
/// pass writes `shape_after`; `INSERT … SELECT` of every column both shapes
/// hold, and of each column `casts` names (`(column, sql_expr)`: the value
/// to copy, a `CAST` for a type change or a backfill literal for a new
/// `NOT NULL` column); `DROP TABLE`; the rename; then `shape_after`'s
/// ferro-owned indexes as the create pass builds them.
///
/// A column only `shape_after` holds and `casts` does not name gets no
/// value: `NULL`, which a `NOT NULL` column refuses on a populated table
/// (ADR-0033: a down restores schema, never data).
///
/// # Errors
/// `shape_after` does not render (an unresolvable column type), or names a
/// table other than `table`.
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
    for col in &shape_after.columns {
        let cast = casts.iter().find(|(name, _)| name == &col.name);
        let value = match cast {
            Some((_, expr)) => expr.clone(),
            None if shape_before.columns.iter().any(|old| old.name == col.name) => {
                quote_ident(&col.name)
            }
            None => continue,
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
        let Some(rest) = statement.trim_start().strip_prefix("CREATE TABLE ") else {
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
    use super::super::columns::PlanContext;
    use super::super::tests::{column, ir, model, pk};
    use super::*;
    use crate::render_create_table;
    use ferro_schema_ir::{SchemaCheck, SchemaForeignKey, SchemaIndex, SchemaUnique};

    const DIRECTIONS: [PlanDirection; 2] = [PlanDirection::Up, PlanDirection::Down];

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

    /// `needs_rebuild` of `op` in a file turning `before` into `after`.
    fn rebuilds(
        op: &MigrationOp,
        before: &SchemaModel,
        after: &SchemaModel,
        dialect: Dialect,
        direction: PlanDirection,
    ) -> bool {
        let (before, after) = (ir(vec![before.clone()]), ir(vec![after.clone()]));
        let ctx = PlanContext::of(op, &before, &after, dialect, direction);
        needs_rebuild(op, direction, &ctx)
    }

    fn t() -> String {
        "author".to_string()
    }

    /// Every op the planner emits, with the table shapes it is decided
    /// against and whether SQLite rebuilds it going (up, down).
    fn the_table() -> Vec<(MigrationOp, SchemaModel, SchemaModel, (bool, bool))> {
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
                (false, false),
            ),
            (
                MigrationOp::DropTable { table: t() },
                plain.clone(),
                plain.clone(),
                (false, false),
            ),
            (
                MigrationOp::CreateEnumType {
                    type_name: "status".into(),
                    labels: vec![],
                },
                plain.clone(),
                plain.clone(),
                (false, false),
            ),
            (
                MigrationOp::DropEnumType {
                    type_name: "status".into(),
                },
                plain.clone(),
                plain.clone(),
                (false, false),
            ),
            (
                MigrationOp::AddEnumLabel {
                    type_name: "status".into(),
                    label: "x".into(),
                },
                plain.clone(),
                plain.clone(),
                (false, false),
            ),
            // A1, A2.
            (add("bio"), plain.clone(), bio.clone(), (false, false)),
            (add("bio"), plain.clone(), defaulted.clone(), (false, false)),
            // A3 up is a backfill; the down of an A4 drop puts it back.
            (add("bio"), plain.clone(), req_bio.clone(), (false, true)),
            // A1 with a foreign key: inline REFERENCES.
            (
                add("team_id"),
                plain.clone(),
                opt_fk.clone(),
                (false, false),
            ),
            // A required foreign-key column: no REFERENCES on ADD COLUMN.
            (add("team_id"), plain.clone(), req_fk.clone(), (true, true)),
            // A4 plain, nullable or not.
            (drop("bio"), bio.clone(), plain.clone(), (false, false)),
            (drop("bio"), req_bio.clone(), plain.clone(), (false, false)),
            // C2, and the down of A1-with-FK.
            (drop("team_id"), opt_fk.clone(), plain.clone(), (true, true)),
            // A6.
            (
                MigrationOp::AlterColumnType {
                    table: t(),
                    column: "age".into(),
                },
                age_int.clone(),
                age_text.clone(),
                (true, true),
            ),
            // A7a, A7b.
            (
                MigrationOp::AlterColumnNullability {
                    table: t(),
                    column: "bio".into(),
                },
                req_bio.clone(),
                bio.clone(),
                (true, true),
            ),
            (
                MigrationOp::ChangePrimaryKey {
                    table: t(),
                    from: vec!["id".into()],
                    to: vec!["name".into()],
                },
                plain.clone(),
                plain.clone(),
                (true, true),
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
                (false, false),
            ),
            (
                MigrationOp::DropIndex {
                    table: t(),
                    name: "idx_author_bio".into(),
                },
                indexed.clone(),
                bio.clone(),
                (false, false),
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
                (false, false),
            ),
            // A10 add, change, drop.
            (
                MigrationOp::AddCheck {
                    table: t(),
                    name: "ck_author_bio".into(),
                },
                bio.clone(),
                checked_bio.clone(),
                (true, true),
            ),
            (
                MigrationOp::RebuildCheck {
                    table: t(),
                    name: "ck_author_bio".into(),
                },
                checked_bio.clone(),
                checked_bio.clone(),
                (true, true),
            ),
            (
                MigrationOp::DropCheck {
                    table: t(),
                    name: "ck_author_bio".into(),
                },
                checked_bio.clone(),
                bio.clone(),
                (true, true),
            ),
            // A dropped column's own check goes with its DROP COLUMN.
            (
                MigrationOp::DropCheck {
                    table: t(),
                    name: "ck_author_bio".into(),
                },
                checked_bio.clone(),
                plain.clone(),
                (false, false),
            ),
            // C3, and a foreign key added to an existing column.
            (
                MigrationOp::AddForeignKey {
                    table: t(),
                    column: "team_id".into(),
                },
                author(vec![nullable("team_id", "integer")]),
                opt_fk.clone(),
                (true, true),
            ),
            (
                MigrationOp::RebuildForeignKey {
                    table: t(),
                    column: "team_id".into(),
                    old_name: "fk_author_team_id_team".into(),
                },
                opt_fk.clone(),
                opt_fk.clone(),
                (true, true),
            ),
            (
                MigrationOp::ValidateConstraint {
                    table: t(),
                    name: "ck_author_bio".into(),
                },
                checked_bio.clone(),
                checked_bio.clone(),
                (false, false),
            ),
            (
                MigrationOp::AddRowPolicy {
                    table: t(),
                    name: "p".into(),
                },
                plain.clone(),
                plain.clone(),
                (false, false),
            ),
            (
                MigrationOp::RebuildRowPolicy {
                    table: t(),
                    name: "p".into(),
                },
                plain.clone(),
                plain.clone(),
                (false, false),
            ),
            (
                MigrationOp::DropRowPolicy {
                    table: t(),
                    name: "p".into(),
                },
                plain.clone(),
                plain.clone(),
                (false, false),
            ),
            (
                MigrationOp::EnableRowSecurity { table: t() },
                plain.clone(),
                plain.clone(),
                (false, false),
            ),
            (
                MigrationOp::ForceRowSecurity { table: t() },
                plain.clone(),
                plain.clone(),
                (false, false),
            ),
            (
                MigrationOp::DisableRowSecurity { table: t() },
                plain.clone(),
                plain.clone(),
                (false, false),
            ),
            (
                MigrationOp::NoForceRowSecurity { table: t() },
                plain.clone(),
                plain,
                (false, false),
            ),
        ]
    }

    #[test]
    fn the_native_or_rebuild_table_for_every_op_and_direction() {
        let table = the_table();
        // Every planner op kind is pinned.
        let kinds: std::collections::BTreeSet<String> = table
            .iter()
            .map(|(op, ..)| super::super::op_kind(op))
            .collect();
        assert_eq!(kinds.len(), 26, "{kinds:?}");
        for (op, before, after, (up, down)) in table {
            for (direction, expected) in DIRECTIONS.into_iter().zip([up, down]) {
                assert_eq!(
                    rebuilds(&op, &before, &after, Dialect::Sqlite, direction),
                    expected,
                    "{op:?} {direction:?}"
                );
                assert!(
                    !rebuilds(&op, &before, &after, Dialect::Postgres, direction),
                    "Postgres never rebuilds: {op:?} {direction:?}"
                );
            }
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
        let statements = render(
            "author",
            &after,
            &before,
            &[("age".into(), "CAST(\"age\" AS varchar)".into())],
        )
        .expect("render");
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
            statements[1..4],
            [
                "INSERT INTO \"_ferro_new_author\" (\"id\", \"name\", \"age\", \"email\") \
                 SELECT \"id\", \"name\", CAST(\"age\" AS varchar), \"email\" FROM \"author\""
                    .to_string(),
                "DROP TABLE \"author\"".to_string(),
                "ALTER TABLE \"_ferro_new_author\" RENAME TO \"author\"".to_string(),
            ]
        );
        assert_eq!(statements[4..], create.post_create_sqls[..]);
        assert_eq!(
            statements[4],
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
            copy_value(&required_fk, &before, false).expect("value"),
            Some("1".into())
        );
        assert_eq!(
            copy_value(&column("name", "string"), &before, false).expect("value"),
            None
        );
        assert_eq!(
            copy_value(&column("name", "integer"), &before, true).expect("value"),
            Some("CAST(\"name\" AS integer)".into())
        );
        assert_eq!(
            copy_value(&nullable("bio", "string"), &before, false).expect("value"),
            None
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
            "CREATE TABLE IF NOT EXISTS \"_ferro_new_author\" ( \"id\" integer )".to_string(),
            "CREATE TABLE \"_ferro_new_post\" ( \"id\" integer )".to_string(),
            "CREATE TABLE IF NOT EXISTS \"tag\" ( \"id\" integer )".to_string(),
            "INSERT INTO \"_ferro_new_author\" SELECT 1".to_string(),
        ];
        assert_eq!(rebuilt_tables(&statements), ["author", "post"]);
        assert!(rebuilt_tables(&[]).is_empty());
    }
}
