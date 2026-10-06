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
//! Every statement comes from [`render_plan`] over [`plan_from_ir`]`(after,
//! before)`: no statement is built here (AGENTS.md § I-1). The down restores
//! schema, never data, so a down that recreates a dropped table or column
//! carries `-- ferro: data-dependent`; a down never carries `destructive`
//! (ADR-0033: nearly every down of an add is a drop), and a generated down is
//! never `irreversible` — only a person declares that.

use super::{DESTRUCTIVE, step_text};
use crate::directory::Headers;
use crate::{
    Dialect, EmissionError, LiveFacts, MigrationOp, MigrationPlan, plan_from_ir, render_plan,
};
use ferro_schema_ir::{IrEnvelope, SchemaIrPayload};
use std::collections::BTreeSet;

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
        | MigrationOp::AddEnumLabel { type_name, .. } => Some(Subject::EnumType(type_name.clone())),
        other => other.table().map(|table| Subject::Table(table.to_string())),
    }
}

/// Whether running `op` brings back something the up removed, whose rows are
/// gone: a down that does this restores the schema but not the data.
fn recreates(op: &MigrationOp) -> bool {
    matches!(
        op,
        MigrationOp::AddTable { .. } | MigrationOp::AddColumn { .. }
    )
}

/// The statements `ops` render to on `dialect`, planned `old → new`.
fn statements(
    ops: Vec<MigrationOp>,
    old: &IrEnvelope<SchemaIrPayload>,
    new: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
) -> Result<Vec<String>, EmissionError> {
    let plan = MigrationPlan {
        operations: ops,
        ..MigrationPlan::default()
    };
    Ok(render_plan(&plan, old, new, dialect)?
        .into_iter()
        .flat_map(|rendered| rendered.statements)
        .collect())
}

/// One generated step on `dialect`, both directions: the up file renders
/// `step_ops` (planned `before → after`), and the down file renders the
/// inverse — [`plan_from_ir`]`(after, before)` with every drop planned,
/// restricted to the tables and enum types `step_ops` touch.
///
/// The down's headers: `data-dependent` when an inverse op recreates a
/// dropped table or column; `not-applicable` when the step has nothing to
/// reverse on `dialect`; never `destructive`, never `irreversible`.
///
/// # Errors
/// An op that cannot render (an [`EmissionError`] from [`render_plan`]).
pub fn render_down(
    step_ops: &[MigrationOp],
    before: &IrEnvelope<SchemaIrPayload>,
    after: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
) -> Result<Rendering, EmissionError> {
    let subjects: BTreeSet<Subject> = step_ops.iter().filter_map(subject).collect();
    let inverse: Vec<MigrationOp> =
        plan_from_ir(after, before, dialect, &LiveFacts::declared(), DESTRUCTIVE)
            .operations
            .into_iter()
            .filter(|op| subject(op).is_some_and(|s| subjects.contains(&s)))
            .collect();

    let up_statements = statements(step_ops.to_vec(), before, after, dialect)?;
    let headers = Headers {
        destructive: !up_statements.is_empty()
            && step_ops.iter().any(|op| {
                matches!(
                    op,
                    MigrationOp::DropTable { .. } | MigrationOp::DropEnumType { .. }
                )
            }),
        not_applicable: up_statements.is_empty(),
        ..Headers::default()
    };

    let data_dependent = inverse.iter().any(recreates);
    let down_statements = statements(inverse, after, before, dialect)?;
    let down_headers = Headers {
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
    use crate::PlanOptions;
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
        render_down(&up_ops(before, after, dialect), before, after, dialect).expect("render")
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
        let r = render_down(&ops, &before, &after, Dialect::Sqlite).expect("render");
        assert_eq!(r.down, "DROP TABLE \"tag\";\n");
    }

    #[test]
    fn a_step_with_nothing_on_a_dialect_is_not_applicable_both_ways() {
        let r = render_down(&[], &ir(vec![]), &ir(vec![]), Dialect::Sqlite).expect("render");
        assert_eq!(r.up, "-- ferro: not-applicable\n");
        assert_eq!(r.down, "-- ferro: not-applicable\n");
        assert!(r.headers.not_applicable && r.down_headers.not_applicable);
    }
}
