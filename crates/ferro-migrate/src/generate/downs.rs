//! The generated down (ADR-0033): one step's reverse is the one planner run
//! backwards, from the stage after the step to the stage before it,
//! restricted to the artifacts the step touches.
//!
//! ```text
//! 0002_drop_author/01_schema.up.postgres.sql     DROP TABLE "author"; DROP TYPE "status"
//! 0002_drop_author/01_schema.down.postgres.sql   -- ferro: data-dependent
//!                                                CREATE TYPE "status" …; CREATE TABLE "author" (…)
//! ```
//!
//! The down is [`crate::plan_down`]`(step_ops, after, before)`, the one down
//! every door uses (ADR-0050): the planner run back, keeping only the ops
//! whose artifact the step's up touched, every statement from the one
//! renderer — no statement is built here (AGENTS.md § I-1). The down restores
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

use super::backfill;
use super::columns;
use super::rebuild;
use super::renames;
use super::{GenerateError, refuse_unrendered, step_text};
use crate::directory::Headers;
use crate::plan::{self, Hint, renamed_snapshot};
use crate::render::render_ops;
use crate::{Dialect, Execution, MigrationOp, PlannedOp, RenderedOp, RowRisk, Side, plan_down};
use ferro_ddl_lowering::{ConstraintMode, IndexMode};
use ferro_schema_ir::{IrEnvelope, SchemaColumn, SchemaIrPayload};
use std::collections::{BTreeMap, BTreeSet};

use super::Rendering;

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

/// Whether `op`'s statements, its constraints added in `constraints` mode,
/// can fail on the rows the table holds (its verdict's [`RowRisk`]): a check
/// or foreign key added `NOT VALID` scans nothing — its validate step is the
/// data-dependent one.
fn may_fail_on_rows(op: &PlannedOp, constraints: ConstraintMode) -> bool {
    match op.verdict.fails_on_rows {
        RowRisk::None => false,
        RowRisk::Always => true,
        RowRisk::WhenValidated => constraints == ConstraintMode::Plain,
    }
}

/// Whether `ops` rewrite rows on `dialect`: a label rename over a column that
/// keeps the label as text in its rows ([`plan::relabels_rows`], as `ir`
/// declares the column) is an `UPDATE`, which the rows' constraints can reject.
fn rewrites_rows(ops: &[PlannedOp], ir: &IrEnvelope<SchemaIrPayload>, dialect: Dialect) -> bool {
    ops.iter().any(|planned| {
        let MigrationOp::RenameEnumLabel { columns, .. } = &planned.op else {
            return false;
        };
        columns.iter().any(|(table, column)| {
            find_column(ir, table, column).is_none_or(|col| plan::relabels_rows(col, dialect))
        })
    })
}

/// The mode a file adds its foreign keys and checks in: `NOT VALID` in an up
/// file on Postgres, where each one is on a table that already exists (a new
/// table's ride its `CREATE TABLE`) and a later step validates it
/// (ADR-0043); plain in a down, which restores its step's pre-state in that
/// step, and on SQLite, which has no unvalidated constraint.
fn constraint_mode(dialect: Dialect, up: bool) -> ConstraintMode {
    if dialect == Dialect::Postgres && up {
        ConstraintMode::NotValid
    } else {
        ConstraintMode::Plain
    }
}

/// `new` with every `NOT NULL` column that `ops` add demanding values of the
/// rows already there declared nullable, and the `SET NOT NULL` that brings
/// each back to `new`: the two halves of putting such a column back on
/// Postgres.
fn relaxed(
    ops: &[PlannedOp],
    new: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
) -> (IrEnvelope<SchemaIrPayload>, Vec<MigrationOp>) {
    let mut loose = Vec::new();
    let mut tighten = Vec::new();
    if dialect == Dialect::Postgres {
        for planned in ops.iter().filter(|planned| planned.verdict.demands_values) {
            let MigrationOp::AddColumn { table, column } = &planned.op else {
                continue;
            };
            loose.push((table.clone(), column.clone()));
            tighten.push(MigrationOp::AlterColumnNullability {
                table: table.clone(),
                column: column.clone(),
            });
        }
    }
    (backfill::relax_columns(new, &loose), tighten)
}

fn rendered(
    ops: Vec<MigrationOp>,
    old: &IrEnvelope<SchemaIrPayload>,
    new: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
    constraints: ConstraintMode,
) -> Result<Vec<RenderedOp>, GenerateError> {
    Ok(render_ops(
        &ops,
        old,
        new,
        dialect,
        constraints,
        IndexMode::Plain,
    )?)
}

/// Each of `ops`' statements on `dialect`, planned `old → new`, op by op. A
/// down (`restore`) adds a `NOT NULL` column that demands values nullable
/// and sets it `NOT NULL` right after its own statements; an up never meets
/// one (the phase table sends it to a backfill).
fn native_statements(
    ops: Vec<PlannedOp>,
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
    let ops: Vec<MigrationOp> = ops.into_iter().map(|planned| planned.op).collect();
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

/// The statements `ops` render to on `dialect` in a file from `old` to
/// `new`, and whether any table is rebuilt. On SQLite every op on a table one
/// of `ops` rebuilds ([`rebuild::tables_to_rebuild`]) folds into that table's
/// one [`rebuild::render_table`], placed where the table's first op stands;
/// every other op renders natively, in the planner's order (ADR-0046). A
/// down (`restore`) puts a demanding column back as [`native_statements`]
/// does.
fn statements(
    ops: Vec<PlannedOp>,
    old: &IrEnvelope<SchemaIrPayload>,
    new: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
    restore: bool,
    constraints: ConstraintMode,
) -> Result<(Vec<String>, bool), GenerateError> {
    let rebuilt = rebuild::tables_to_rebuild(&ops);
    // A table the step rebuilds relabels its rows in the rebuild's copy, where
    // they meet the table's new check; its label rename leaves it out.
    let mut relabels: BTreeMap<String, Vec<(String, String, String)>> = BTreeMap::new();
    let ops: Vec<PlannedOp> = ops
        .into_iter()
        .filter_map(|planned| match planned.op {
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
                (!updated.is_empty() || dialect == Dialect::Postgres).then_some(PlannedOp {
                    op: MigrationOp::RenameEnumLabel {
                        type_name,
                        old: from,
                        new: to,
                        columns: updated,
                    },
                    verdict: planned.verdict,
                })
            }
            _ => Some(planned),
        })
        .collect();
    let folded = |op: &MigrationOp| op.table().is_some_and(|table| rebuilt.contains(table));
    let native: Vec<PlannedOp> = ops.iter().filter(|p| !folded(&p.op)).cloned().collect();
    let mut native =
        native_statements(native, old, new, dialect, restore, constraints)?.into_iter();
    let mut out = Vec::new();
    let mut written = BTreeSet::new();
    for planned in &ops {
        match planned.op.table().filter(|table| rebuilt.contains(*table)) {
            Some(table) => {
                if written.insert(table) {
                    let relabelled = relabels.get(table).map(Vec::as_slice).unwrap_or(&[]);
                    out.extend(rebuild::render_table(table, old, new, relabelled)?);
                }
            }
            None => out.extend(native.next().ok_or_else(|| {
                GenerateError::Render(format!("{:?} rendered no statement group", planned.op))
            })?),
        }
    }
    Ok((out, !rebuilt.is_empty()))
}

/// `ops` with their verdicts between a step's two stages, `before` (read as
/// the planner leaves it, [`plan::planned_before`]) and `after`: what is true
/// of each op between the sides the step renders it between (ADR-0050). A
/// demanded column the expand adds nullable is a plain add there, which the
/// migration's plan, from the parent to the target, does not say.
pub(super) fn decided(
    ops: &[MigrationOp],
    before: &IrEnvelope<SchemaIrPayload>,
    after: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
) -> Vec<PlannedOp> {
    let old = Side::declared(plan::planned_before(before, after, dialect).into_owned());
    let new = Side::declared(after.clone());
    ops.iter()
        .map(|op| PlannedOp::of(op.clone(), &old, &new, dialect))
        .collect()
}

/// [`render_step`] as the step's two files.
///
/// # Errors
/// What [`render_step`] raises.
pub(super) fn render_down(
    step_ops: &[MigrationOp],
    before: &IrEnvelope<SchemaIrPayload>,
    after: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
    hints: &[Hint],
) -> Result<Rendering, GenerateError> {
    let step = render_step(step_ops, before, after, dialect, hints)?;
    Ok(Rendering {
        up: step_text(&step.headers, &step.up),
        down: step_text(&step.down_headers, &step.down),
        headers: step.headers,
        down_headers: step.down_headers,
    })
}

/// One step's statements and headers, both directions, before they are
/// written as files: what a step that holds more than the planner's ops (the
/// contract) composes with its own.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub(super) struct StepStatements {
    /// The up file's statements.
    pub up: Vec<String>,
    /// The up file's headers.
    pub headers: Headers,
    /// The down file's statements.
    pub down: Vec<String>,
    /// The down file's headers.
    pub down_headers: Headers,
}

/// One generated step on `dialect`, both directions: the up file renders
/// `step_ops` (the migration's plan's ops placed in the step, the declared
/// renames `hints` among them) between the stages `before` and `after`,
/// each with its verdict between them ([`decided`]), and the down file
/// renders [`plan_down`]`(step_ops, after, before)` — the planner run back
/// between the two declared stages, keeping only the ops whose artifact the
/// step's up touched. On SQLite the drop of a dropped column's own check is
/// left out: its `DROP COLUMN` carries it ([`columns::omitted`]).
///
/// On Postgres the up adds every foreign key and check `NOT VALID`
/// (ADR-0043); the down restores the step's pre-state with plain statements.
///
/// The up's headers: `destructive` when it drops data (a table, a column or
/// an enum type); `data-dependent` when its statements can fail on existing
/// rows. The down's: `data-dependent` when an op of it recreates a dropped
/// table or `NOT NULL` column, or can fail on the rows it finds;
/// `not-applicable` when the step has nothing to reverse on `dialect`; never
/// `destructive`, and never `irreversible`: between two declared stages
/// every op is expressible (ADR-0033).
///
/// # Errors
/// An op that cannot render (an [`crate::EmissionError`] from the
/// renderer), or that renders only a blocking report
/// ([`GenerateError::Unrenderable`]), or a down op with no way to run
/// between two declared stages.
pub(super) fn render_step(
    step_ops: &[MigrationOp],
    before: &IrEnvelope<SchemaIrPayload>,
    after: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
    hints: &[Hint],
) -> Result<StepStatements, GenerateError> {
    let step_ops: Vec<PlannedOp> = decided(step_ops, before, after, dialect)
        .into_iter()
        .filter(|planned| !columns::omitted(planned, dialect))
        .collect();
    let step_ops = step_ops.as_slice();
    let ops: Vec<MigrationOp> = step_ops.iter().map(|planned| planned.op.clone()).collect();
    // A step holding the migration's renames (ADR-0032) runs its table and
    // column renames first; everything else in it reads the table under its
    // new names.
    let hints: &[Hint] = if ops.iter().any(renames::is_rename) {
        hints
    } else {
        &[]
    };
    let renamed_before = renamed_snapshot(before, hints);
    let (up_structural, up_rest): (Vec<PlannedOp>, Vec<PlannedOp>) = step_ops
        .iter()
        .cloned()
        .partition(|planned| renames::is_structural(&planned.op));

    let up_mode = constraint_mode(dialect, true);
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
    let (rest, up_rebuilds) = statements(up_rest, &renamed_before, after, dialect, false, up_mode)?;
    up_statements.extend(rest);
    let headers = Headers {
        foreign_keys_off: up_rebuilds,
        destructive: !up_statements.is_empty()
            && step_ops.iter().any(|planned| planned.verdict.drops_data),
        data_dependent: !up_statements.is_empty()
            && (rewrites_rows(step_ops, &renamed_before, dialect)
                || step_ops.iter().any(|op| may_fail_on_rows(op, up_mode))),
        not_applicable: up_statements.is_empty(),
        ..Headers::default()
    };

    // The down: the planner run back, scoped to what the up touched, its
    // renames (the up's, swapped) first.
    let down = plan_down(
        &ops,
        &Side::declared(after.clone()),
        &Side::declared(before.clone()),
        dialect,
    );
    let down_ops: Vec<PlannedOp> = down
        .operations
        .iter()
        .filter(|planned| !columns::omitted(planned, dialect))
        .cloned()
        .collect();
    if let Some(planned) = down_ops.iter().find(|planned| {
        matches!(
            planned.verdict.execution,
            Execution::Refused(_) | Execution::Irreversible(_)
        )
    }) {
        return Err(GenerateError::Render(format!(
            "the down of {:?} has no way to run between two declared stages: {:?}",
            planned.op, planned.verdict.execution
        )));
    }
    let down_mode = constraint_mode(dialect, false);
    let data_dependent = rewrites_rows(&down_ops, down.before.ir(), dialect)
        || down_ops
            .iter()
            .any(|op| op.verdict.recreates || may_fail_on_rows(op, down_mode));
    let (down_structural, down_rest): (Vec<PlannedOp>, Vec<PlannedOp>) = down_ops
        .into_iter()
        .partition(|planned| renames::is_structural(&planned.op));
    let mut down_statements: Vec<String> = native_statements(
        down_structural,
        after,
        down.before.ir(),
        dialect,
        false,
        down_mode,
    )?
    .into_iter()
    .flatten()
    .collect();
    let (rest, down_rebuilds) = statements(
        down_rest,
        down.before.ir(),
        before,
        dialect,
        true,
        down_mode,
    )?;
    down_statements.extend(rest);
    let down_headers = Headers {
        foreign_keys_off: down_rebuilds,
        data_dependent: !down_statements.is_empty() && data_dependent,
        not_applicable: down_statements.is_empty(),
        ..Headers::default()
    };

    Ok(StepStatements {
        up: up_statements,
        headers,
        down: down_statements,
        down_headers,
    })
}

#[cfg(test)]
mod tests {
    use super::super::tests::{create_pass, file, ir, model, pk};
    use super::*;
    use crate::{PlanOptions, Side, plan_from_ir};
    use ferro_schema_ir::{RowPolicyCommand, RowPolicyExpr, SchemaRowPolicy, SchemaRowSecurity};

    fn up_ops(
        before: &IrEnvelope<SchemaIrPayload>,
        after: &IrEnvelope<SchemaIrPayload>,
        dialect: Dialect,
    ) -> Vec<MigrationOp> {
        plan_from_ir(
            &Side::declared(before.clone()),
            &Side::declared(after.clone()),
            dialect,
            PlanOptions { destructive: true },
        )
        .ops()
        .cloned()
        .collect()
    }

    fn render(
        before: &IrEnvelope<SchemaIrPayload>,
        after: &IrEnvelope<SchemaIrPayload>,
        dialect: Dialect,
    ) -> Rendering {
        render_down(&up_ops(before, after, dialect), before, after, dialect, &[]).expect("render")
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
}
