//! The generated down (ADR-0033): one step's reverse is the one planner run
//! backwards, from the migration's snapshot to its parent's, restricted to
//! what the step touches.
//!
//! ```text
//! 0002_drop_author/01_schema.up.postgres.sql     DROP TABLE "author"; DROP TYPE "status"
//! 0002_drop_author/01_schema.down.postgres.sql   -- ferro: data-dependent
//!                                                CREATE TYPE "status" …; CREATE TABLE "author" (…)
//! ```
//!
//! Every statement comes from [`render_plan`] over [`crate::plan_from_ir`]`(after,
//! before)`: no statement is built here (AGENTS.md § I-1). The down restores
//! schema, never data, so a down that recreates a dropped table or `NOT NULL`
//! column carries `-- ferro: data-dependent`, as does one whose statements
//! can fail on the rows it finds (a cast, `SET NOT NULL`, a unique); a down
//! never carries `destructive` (ADR-0033: nearly every down of an add is a
//! drop), and a generated down is never `irreversible` — only a person
//! declares that.
//!
//! A dropped `NOT NULL` column with no default comes back `NOT NULL` on
//! Postgres as the pass's two statements for it, added nullable then
//! `SET NOT NULL`: on an empty table the down reaches the parent snapshot,
//! on a populated one it fails (ADR-0033), never landing on a relaxed schema.
//!
//! ```text
//! 0003_drop_bio/01_schema.down.postgres.sql   -- ferro: data-dependent
//!                                             ALTER TABLE "author" ADD COLUMN "bio" varchar;
//!                                             ALTER TABLE "author" ALTER COLUMN "bio" SET NOT NULL;
//! ```

use super::columns::{self, Phase, PlanContext, PlanDirection};
use super::rebuild;
use super::renames;
use super::{GenerateError, refuse_unrendered, step_text};
use crate::directory::Headers;
use crate::plan::{
    self, Hint, rename_ops, renamed_snapshot, renamed_table, reverse_hints, storage_hints,
};
use crate::render::render_plan_in;
use crate::{Dialect, MigrationOp, MigrationPlan, RenderedOp};
use ferro_ddl_lowering::ConstraintMode;
use ferro_schema_ir::{IrEnvelope, SchemaColumn, SchemaIrPayload};
use std::collections::{BTreeMap, BTreeSet};

use super::Rendering;

/// What an op changes: its table, or the enum type it creates or drops.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
enum Subject {
    Table(String),
    EnumType(String),
}

fn subject(op: &MigrationOp) -> Option<Subject> {
    match op {
        MigrationOp::CreateEnumType { type_name, .. }
        | MigrationOp::DropEnumType { type_name }
        | MigrationOp::AddEnumLabel { type_name, .. }
        | MigrationOp::RenameEnumLabel { type_name, .. }
        | MigrationOp::RenameEnumType { new: type_name, .. } => {
            Some(Subject::EnumType(type_name.clone()))
        }
        other => other.table().map(|table| Subject::Table(table.to_string())),
    }
}

fn find_column<'a>(
    ir: &'a IrEnvelope<SchemaIrPayload>,
    table: &str,
    column: &str,
) -> Option<&'a SchemaColumn> {
    ir.payload
        .models
        .iter()
        .find(|model| model.table_name == table)?
        .columns
        .iter()
        .find(|col| col.name == column)
}

/// The column `op` adds, or whose type or nullability it changes, as `new`
/// declares it.
fn target_column<'a>(
    op: &MigrationOp,
    new: &'a IrEnvelope<SchemaIrPayload>,
) -> Option<&'a SchemaColumn> {
    match op {
        MigrationOp::AddColumn { table, column }
        | MigrationOp::AlterColumnType { table, column }
        | MigrationOp::AlterColumnNullability { table, column } => find_column(new, table, column),
        _ => None,
    }
}

/// Whether running `op` brings back something the up removed, whose rows are
/// gone: a table, or a `NOT NULL` column (a nullable one comes back exactly
/// as declared, holding nothing).
fn recreates(op: &MigrationOp, new: &IrEnvelope<SchemaIrPayload>) -> bool {
    match op {
        MigrationOp::AddTable { .. } => true,
        MigrationOp::AddColumn { .. } => target_column(op, new).is_some_and(|col| !col.nullable),
        _ => false,
    }
}

/// Whether `op`'s statements, its constraints added in `constraints` mode,
/// can fail on the rows the table holds: a cast, a `SET NOT NULL`, a unique
/// over existing values, a `NOT NULL` column added with no value to give
/// them, or a check or foreign key validated as it is added (one added
/// `NOT VALID` scans nothing; its validate step is the data-dependent one).
fn may_fail_on_rows(
    op: &MigrationOp,
    new: &IrEnvelope<SchemaIrPayload>,
    constraints: ConstraintMode,
) -> bool {
    let column = target_column(op, new);
    match op {
        MigrationOp::AlterColumnType { .. } => true,
        MigrationOp::AlterColumnNullability { .. } => column.is_some_and(|col| !col.nullable),
        MigrationOp::AddIndex { unique, .. } => *unique,
        // A check or a foreign key over rows already there (a SQLite rebuild
        // copies them under it).
        MigrationOp::AddCheck { .. }
        | MigrationOp::RebuildCheck { .. }
        | MigrationOp::AddForeignKey { .. }
        | MigrationOp::RebuildForeignKey { .. } => constraints == ConstraintMode::Plain,
        MigrationOp::AddColumn { .. } => {
            column.is_some_and(|col| columns::needs_values(col) || (!col.nullable && col.unique))
        }
        _ => false,
    }
}

/// Whether `ops` rewrite rows on `dialect`: a label rename over a column that
/// keeps the label as text in its rows ([`plan::relabels_rows`], as `ir`
/// declares the column) is an `UPDATE`, which the rows' constraints can reject.
fn rewrites_rows(ops: &[MigrationOp], ir: &IrEnvelope<SchemaIrPayload>, dialect: Dialect) -> bool {
    ops.iter().any(|op| {
        let MigrationOp::RenameEnumLabel { columns, .. } = op else {
            return false;
        };
        columns.iter().any(|(table, column)| {
            find_column(ir, table, column).is_none_or(|col| plan::relabels_rows(col, dialect))
        })
    })
}

/// Whether `op` drops data.
fn drops_data(op: &MigrationOp) -> bool {
    matches!(
        op,
        MigrationOp::DropTable { .. }
            | MigrationOp::DropEnumType { .. }
            | MigrationOp::DropColumn { .. }
    )
}

/// `new` with every `NOT NULL` column that `ops` adds with no value for
/// existing rows declared nullable, and the `SET NOT NULL` that brings each
/// back to `new`: the two halves of putting such a column back on Postgres.
fn relaxed(
    ops: &[MigrationOp],
    new: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
) -> (IrEnvelope<SchemaIrPayload>, Vec<MigrationOp>) {
    let mut relaxed = new.clone();
    let mut tighten = Vec::new();
    if dialect != Dialect::Postgres {
        return (relaxed, tighten);
    }
    for op in ops {
        let MigrationOp::AddColumn { table, column } = op else {
            continue;
        };
        let Some(col) = relaxed
            .payload
            .models
            .iter_mut()
            .filter(|model| &model.table_name == table)
            .flat_map(|model| model.columns.iter_mut())
            .find(|col| &col.name == column)
        else {
            continue;
        };
        if columns::needs_values(col) {
            col.nullable = true;
            tighten.push(MigrationOp::AlterColumnNullability {
                table: table.clone(),
                column: column.clone(),
            });
        }
    }
    (relaxed, tighten)
}

fn rendered(
    ops: Vec<MigrationOp>,
    old: &IrEnvelope<SchemaIrPayload>,
    new: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
    constraints: ConstraintMode,
) -> Result<Vec<RenderedOp>, GenerateError> {
    let plan = MigrationPlan {
        operations: ops,
        ..MigrationPlan::default()
    };
    Ok(render_plan_in(&plan, old, new, dialect, constraints)?)
}

/// Each of `ops`' statements on `dialect`, planned `old → new`, op by op. A
/// down (`restore`) adds a `NOT NULL` column with no default nullable and
/// sets it `NOT NULL` right after its own statements; an up never meets one
/// ([`columns::assign`] sends it to a backfill).
fn native_statements(
    ops: Vec<MigrationOp>,
    old: &IrEnvelope<SchemaIrPayload>,
    new: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
    restore: bool,
    constraints: ConstraintMode,
) -> Result<Vec<Vec<String>>, GenerateError> {
    let (relaxed, tighten) = if restore {
        relaxed(&ops, new, dialect)
    } else {
        (new.clone(), Vec::new())
    };
    let ops_rendered = rendered(ops, old, &relaxed, dialect, constraints)?;
    refuse_unrendered(&ops_rendered, dialect)?;
    let tighten_rendered = rendered(tighten, &relaxed, new, dialect, constraints)?;
    refuse_unrendered(&tighten_rendered, dialect)?;
    let mut out = Vec::new();
    for op in ops_rendered {
        let mut statements = op.statements;
        if let MigrationOp::AddColumn { table, column } = &op.op {
            statements.extend(
                tighten_rendered
                    .iter()
                    .filter(|t| {
                        matches!(&t.op, MigrationOp::AlterColumnNullability { table: tt, column: tc }
                            if tt == table && tc == column)
                    })
                    .flat_map(|t| t.statements.iter().cloned()),
            );
        }
        out.push(statements);
    }
    Ok(out)
}

/// The statements `ops` render to on `dialect` in a file going `direction`
/// from `old` to `new`, and whether any table is rebuilt. On SQLite every op
/// on a table that needs a rebuild folds into that table's one
/// [`rebuild::render_table`], placed where the table's first op stands;
/// every other op renders natively, in the planner's order (ADR-0046).
fn statements(
    ops: Vec<MigrationOp>,
    old: &IrEnvelope<SchemaIrPayload>,
    new: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
    direction: PlanDirection,
) -> Result<(Vec<String>, bool), GenerateError> {
    let rebuilt = rebuild::tables_to_rebuild(&ops, old, new, dialect, direction);
    // A table the step rebuilds relabels its rows in the rebuild's copy, where
    // they meet the table's new check; its label rename leaves it out.
    let mut relabels: BTreeMap<String, Vec<(String, String, String)>> = BTreeMap::new();
    let ops: Vec<MigrationOp> = ops
        .into_iter()
        .filter_map(|op| match op {
            MigrationOp::RenameEnumLabel {
                type_name,
                old: from,
                new: to,
                columns,
            } => {
                let (copied, updated): (Vec<_>, Vec<_>) = columns
                    .into_iter()
                    .partition(|(table, _)| rebuilt.contains(table));
                for (table, column) in copied {
                    relabels
                        .entry(table)
                        .or_default()
                        .push((column, from.clone(), to.clone()));
                }
                (!updated.is_empty() || dialect == Dialect::Postgres).then_some(
                    MigrationOp::RenameEnumLabel {
                        type_name,
                        old: from,
                        new: to,
                        columns: updated,
                    },
                )
            }
            other => Some(other),
        })
        .collect();
    let folded = |op: &MigrationOp| op.table().is_some_and(|table| rebuilt.contains(table));
    let native: Vec<MigrationOp> = ops.iter().filter(|op| !folded(op)).cloned().collect();
    let mut native = native_statements(
        native,
        old,
        new,
        dialect,
        direction == PlanDirection::Down,
        columns::constraint_mode(dialect, direction),
    )?
    .into_iter();
    let mut out = Vec::new();
    let mut written = BTreeSet::new();
    for op in &ops {
        match op.table().filter(|table| rebuilt.contains(*table)) {
            Some(table) => {
                if written.insert(table) {
                    let relabelled = relabels.get(table).map(Vec::as_slice).unwrap_or(&[]);
                    out.extend(rebuild::render_table(table, old, new, relabelled)?);
                }
            }
            None => out.extend(native.next().ok_or_else(|| {
                GenerateError::Render(format!("{op:?} rendered no statement group"))
            })?),
        }
    }
    Ok((out, !rebuilt.is_empty()))
}

/// One generated step on `dialect`, both directions: the up file renders
/// `step_ops` (planned `before → after`), and the down file renders the
/// inverse — [`crate::plan_from_ir`]`(after, before)` with every drop planned
/// and row security the step introduced torn down
/// ([`super::row_security::with_teardown`]), restricted to the tables and enum types `step_ops` touch and to the ops
/// [`columns::assign`] puts in the step's `phase`.
///
/// On Postgres the up adds every foreign key and check `NOT VALID`
/// ([`columns::constraint_mode`], ADR-0043); the down restores the step's
/// pre-state with plain statements.
///
/// The up's headers: `destructive` when it drops a table, a column or an
/// enum type; `data-dependent` when its statements can fail on existing rows.
/// The down's: `data-dependent` when an inverse op recreates a dropped table
/// or `NOT NULL` column, or can fail on the rows it finds; `not-applicable`
/// when the step has nothing to reverse on `dialect`; never `destructive`,
/// never `irreversible`.
///
/// # Errors
/// An op that cannot render (an [`crate::EmissionError`] from
/// [`render_plan`]), or that renders only a warning
/// ([`GenerateError::Unrenderable`]).
pub fn render_down(
    step_ops: &[MigrationOp],
    before: &IrEnvelope<SchemaIrPayload>,
    after: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
    phase: Phase,
    hints: &[Hint],
) -> Result<Rendering, GenerateError> {
    // A step holding the migration's renames (ADR-0032) runs its table and
    // column renames first; everything else in it reads the table under its
    // new names. Its down undoes them first the other way, then restores the
    // rest under the parent's names (ADR-0033).
    let hints: &[Hint] = if step_ops.iter().any(renames::is_rename) {
        hints
    } else {
        &[]
    };
    let renamed_before = renamed_snapshot(before, hints);
    let reverse = reverse_hints(hints);
    let renamed_after = renamed_snapshot(after, &reverse);
    // The side the down is planned from: on SQLite with its labels as the
    // step leaves them, so a column the label renames widened is narrowed back
    // ([`storage_hints`]).
    let planned_after = renamed_snapshot(after, &storage_hints(&reverse, dialect));
    let (up_structural, up_rest): (Vec<MigrationOp>, Vec<MigrationOp>) =
        step_ops.iter().cloned().partition(renames::is_structural);
    let (down_structural, down_derived): (Vec<MigrationOp>, Vec<MigrationOp>) =
        rename_ops(after, &renamed_after, dialect)
            .into_iter()
            .filter(|_| !hints.is_empty())
            .partition(renames::is_structural);

    // The step's tables by the names the down restores them under.
    let subjects: BTreeSet<Subject> = step_ops
        .iter()
        .filter_map(subject)
        .map(|s| match s {
            Subject::Table(table) => Subject::Table(renamed_table(&reverse, &table)),
            other => other,
        })
        .collect();
    let inverse: Vec<MigrationOp> = super::plan(&planned_after, before, dialect)
        .operations
        .into_iter()
        .filter(|op| subject(op).is_some_and(|s| subjects.contains(&s)))
        .filter(|op| {
            let ctx = PlanContext::of(op, &planned_after, before, dialect, PlanDirection::Down);
            !columns::carried_by_its_column_drop(op, &ctx)
                && columns::assign(op, &ctx).phase == phase
        })
        .collect();

    let up_mode = columns::constraint_mode(dialect, PlanDirection::Up);
    let mut up_statements: Vec<String> = native_statements(
        up_structural,
        before,
        &renamed_before,
        dialect,
        false,
        up_mode,
    )?
    .into_iter()
    .flatten()
    .collect();
    let (rest, up_rebuilds) =
        statements(up_rest, &renamed_before, after, dialect, PlanDirection::Up)?;
    up_statements.extend(rest);
    let headers = Headers {
        foreign_keys_off: up_rebuilds,
        destructive: !up_statements.is_empty() && step_ops.iter().any(drops_data),
        data_dependent: !up_statements.is_empty()
            && (rewrites_rows(step_ops, &renamed_before, dialect)
                || step_ops
                    .iter()
                    .any(|op| may_fail_on_rows(op, after, up_mode))),
        not_applicable: up_statements.is_empty(),
        ..Headers::default()
    };

    let down_mode = columns::constraint_mode(dialect, PlanDirection::Down);
    let data_dependent = rewrites_rows(&down_derived, &renamed_after, dialect)
        || inverse
            .iter()
            .any(|op| recreates(op, before) || may_fail_on_rows(op, before, down_mode));
    let mut down_statements: Vec<String> = native_statements(
        down_structural,
        after,
        &renamed_after,
        dialect,
        false,
        down_mode,
    )?
    .into_iter()
    .flatten()
    .collect();
    let mut down_ops = down_derived;
    down_ops.extend(inverse);
    let (rest, down_rebuilds) = statements(
        down_ops,
        &renamed_after,
        before,
        dialect,
        PlanDirection::Down,
    )?;
    down_statements.extend(rest);
    let down_headers = Headers {
        foreign_keys_off: down_rebuilds,
        data_dependent: !down_statements.is_empty() && data_dependent,
        not_applicable: down_statements.is_empty(),
        ..Headers::default()
    };

    Ok(Rendering {
        up: step_text(&headers, &up_statements),
        down: step_text(&down_headers, &down_statements),
        headers,
        down_headers,
    })
}

#[cfg(test)]
mod tests {
    use super::super::tests::{author, create_pass, file, ir, model, pk, post};
    use super::*;
    use crate::{LiveFacts, PlanOptions, plan_from_ir};
    use ferro_schema_ir::{RowPolicyCommand, RowPolicyExpr, SchemaRowPolicy, SchemaRowSecurity};

    fn up_ops(
        before: &IrEnvelope<SchemaIrPayload>,
        after: &IrEnvelope<SchemaIrPayload>,
        dialect: Dialect,
    ) -> Vec<MigrationOp> {
        plan_from_ir(
            before,
            after,
            dialect,
            &LiveFacts::declared(),
            PlanOptions { destructive: true },
        )
        .operations
    }

    fn render(
        before: &IrEnvelope<SchemaIrPayload>,
        after: &IrEnvelope<SchemaIrPayload>,
        dialect: Dialect,
    ) -> Rendering {
        render_down(
            &up_ops(before, after, dialect),
            before,
            after,
            dialect,
            Phase::Schema,
            &[],
        )
        .expect("render")
    }

    #[test]
    fn a_created_table_and_its_type_are_dropped_table_first_with_no_destructive_header() {
        let before = ir(vec![]);
        let after = ir(vec![author()]);
        let pg = render(&before, &after, Dialect::Postgres);
        assert_eq!(pg.down, "DROP TABLE \"author\";\n\nDROP TYPE \"status\";\n");
        assert_eq!(pg.down_headers, Headers::default());
        assert_eq!(pg.up, file(&create_pass(&author(), Dialect::Postgres), ""));
        assert_eq!(pg.headers, Headers::default());

        let sqlite = render(&before, &after, Dialect::Sqlite);
        assert_eq!(sqlite.down, "DROP TABLE \"author\";\n");
        assert_eq!(sqlite.down_headers, Headers::default());
    }

    #[test]
    fn a_dropped_table_is_recreated_from_the_parent_snapshot_marked_data_dependent() {
        let before = ir(vec![author(), post()]);
        let after = ir(vec![author()]);
        for dialect in [Dialect::Postgres, Dialect::Sqlite] {
            let r = render(&before, &after, dialect);
            assert_eq!(r.up, "-- ferro: destructive\n\nDROP TABLE \"post\";\n");
            assert_eq!(
                r.down,
                file(&create_pass(&post(), dialect), "-- ferro: data-dependent\n"),
                "{dialect:?}"
            );
            assert!(r.down_headers.data_dependent && !r.down_headers.destructive);
            assert_eq!(r.down_headers.irreversible, None);
        }
    }

    #[test]
    fn a_dropped_table_with_its_type_recreates_the_type_before_the_table() {
        let before = ir(vec![author()]);
        let after = ir(vec![]);
        let pg = render(&before, &after, Dialect::Postgres);
        assert_eq!(
            pg.up,
            "-- ferro: destructive\n\nDROP TABLE \"author\";\n\nDROP TYPE \"status\";\n"
        );
        assert_eq!(
            pg.down,
            file(
                &create_pass(&author(), Dialect::Postgres),
                "-- ferro: data-dependent\n"
            )
        );
    }

    #[test]
    fn row_security_on_a_new_table_goes_with_the_table_and_comes_back_with_it() {
        let guarded = ferro_schema_ir::SchemaModel {
            row_security: Some(SchemaRowSecurity {
                force: true,
                policies: vec![SchemaRowPolicy {
                    name: "rls_ledger_owner_id".into(),
                    command: RowPolicyCommand::All,
                    restrictive: false,
                    expr: RowPolicyExpr::Setting {
                        column: "owner_id".into(),
                        setting: "app.owner_id".into(),
                    },
                }],
            }),
            ..model(
                "Ledger",
                vec![pk(), super::super::tests::column("owner_id", "integer")],
            )
        };
        let empty = ir(vec![]);
        let with = ir(vec![guarded.clone()]);
        let created = render(&empty, &with, Dialect::Postgres);
        assert_eq!(created.down, "DROP TABLE \"ledger\";\n");
        let dropped = render(&with, &empty, Dialect::Postgres);
        assert_eq!(
            dropped.down,
            file(
                &create_pass(&guarded, Dialect::Postgres),
                "-- ferro: data-dependent\n"
            )
        );
        assert!(
            dropped
                .down
                .contains("CREATE POLICY \"rls_ledger_owner_id\"")
        );
    }

    #[test]
    fn the_down_is_restricted_to_what_the_step_touches() {
        let before = ir(vec![author()]);
        let after = ir(vec![author(), post(), model("Tag", vec![pk()])]);
        let ops: Vec<MigrationOp> = up_ops(&before, &after, Dialect::Sqlite)
            .into_iter()
            .filter(|op| op.table() == Some("tag"))
            .collect();
        let r = render_down(&ops, &before, &after, Dialect::Sqlite, Phase::Schema, &[])
            .expect("render");
        assert_eq!(r.down, "DROP TABLE \"tag\";\n");
    }

    #[test]
    fn a_step_with_nothing_on_a_dialect_is_not_applicable_both_ways() {
        let r = render_down(
            &[],
            &ir(vec![]),
            &ir(vec![]),
            Dialect::Sqlite,
            Phase::Schema,
            &[],
        )
        .expect("render");
        assert_eq!(r.up, "-- ferro: not-applicable\n");
        assert_eq!(r.down, "-- ferro: not-applicable\n");
        assert!(r.headers.not_applicable && r.down_headers.not_applicable);
    }
}
