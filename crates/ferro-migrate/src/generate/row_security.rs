//! Row security in a generated migration (ADR-0019, ADR-0033): which
//! row-security ops a file carries, and the one question the reconciliation
//! pass cannot answer — whether the table's row security is the migration's
//! own, to tear down on the way back.
//!
//! ```text
//! class Order(Model):                         0010_order_rls/01_schema.up.postgres.sql
//!     __ferro_rls__ = RowSecurity(       ──▶    ALTER TABLE "order" ENABLE ROW LEVEL SECURITY;
//!         RowPolicy(column="tenant_id",         ALTER TABLE "order" FORCE ROW LEVEL SECURITY;
//!                   setting="app.tenant"))      CREATE POLICY "rls_order_tenant_id" ON "order" …;
//!                                             0010_order_rls/01_schema.down.postgres.sql
//!                                               DROP POLICY "rls_order_tenant_id" ON "order";
//!                                               ALTER TABLE "order" NO FORCE ROW LEVEL SECURITY;
//!                                               ALTER TABLE "order" DISABLE ROW LEVEL SECURITY;
//! ```
//!
//! The pass keeps `ENABLE` and `FORCE` one-way because it cannot know who set
//! them, and tears them down (under `migrate_destructive`) only on the
//! evidence of a ferro-named policy. A file between two declared snapshots
//! does know: when the side it starts from declares row security on a table
//! and the side it ends on does not, ferro installed it
//! ([`introduced_by_migration`], read the other way round), so the file turns
//! both flags off even when no policy is left to witness it. Every statement
//! is still the pass's, through [`render_plan`] (AGENTS.md § I-1 items 15–16).

use super::GenerateError;
use crate::{Dialect, MigrationOp, MigrationPlan, render_plan};
use ferro_ddl_lowering::{
    render_disable_row_security, render_no_force_row_security, row_security_teardown_warning,
};
use ferro_schema_ir::{IrEnvelope, SchemaIrPayload, SchemaModel};
use std::collections::BTreeMap;

/// Whether the migration turning `before` into `after` introduced row
/// security on `table`: the table exists on both sides, `before` declares no
/// row security on it and `after` does. Its down then tears the flags down,
/// because the parent snapshot shows nobody else set them (ADR-0033). A table
/// the migration creates is not one: its down's `DROP TABLE` takes the
/// policies and the flags with it.
pub fn introduced_by_migration(
    table: &str,
    before: &IrEnvelope<SchemaIrPayload>,
    after: &IrEnvelope<SchemaIrPayload>,
) -> bool {
    let declares = |ir: &IrEnvelope<SchemaIrPayload>| {
        model(ir, table).map(|model| model.row_security.is_some())
    };
    declares(before) == Some(false) && declares(after) == Some(true)
}

fn model<'a>(ir: &'a IrEnvelope<SchemaIrPayload>, table: &str) -> Option<&'a SchemaModel> {
    ir.payload
        .models
        .iter()
        .find(|model| model.table_name == table)
}

/// `ops`, the plan of the file turning `from` into `to` on `dialect`, with the
/// flag teardown every table whose row security the file removes is owed
/// ([`introduced_by_migration`]`(table, to, from)`), where the planner's
/// policy evidence left it out.
pub fn with_teardown(
    ops: Vec<MigrationOp>,
    from: &IrEnvelope<SchemaIrPayload>,
    to: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
) -> Vec<MigrationOp> {
    if dialect != Dialect::Postgres {
        return ops;
    }
    let mut ops = ops;
    for declared in &from.payload.models {
        let table = declared.table_name.as_str();
        let Some(declaration) = declared.row_security.as_ref() else {
            continue;
        };
        if !introduced_by_migration(table, to, from) {
            continue;
        }
        let owed = [
            declaration.force.then(|| MigrationOp::NoForceRowSecurity {
                table: table.to_string(),
            }),
            Some(MigrationOp::DisableRowSecurity {
                table: table.to_string(),
            }),
        ];
        let missing: Vec<MigrationOp> = owed
            .into_iter()
            .flatten()
            .filter(|op| !ops.contains(op))
            .collect();
        if missing.is_empty() {
            continue;
        }
        let on_table = |op: &MigrationOp| op.table() == Some(table);
        // Where the planner puts a table's row security (#413): after its
        // other changes, ahead of its column drops.
        let at = ops
            .iter()
            .rposition(|op| on_table(op) && is_row_security(op))
            .map(|i| i + 1)
            .or_else(|| {
                ops.iter()
                    .position(|op| on_table(op) && matches!(op, MigrationOp::DropColumn { .. }))
            })
            .or_else(|| ops.iter().rposition(on_table).map(|i| i + 1))
            .unwrap_or(ops.len());
        ops.splice(at..at, missing);
    }
    ops
}

/// The teardown reports `ops` answer: the pass's warning naming each table's
/// dropped policies and cleared flags, which a generated file does on purpose
/// and in plain sight rather than as a surprise on connect.
pub fn answered_warnings(ops: &[MigrationOp]) -> Vec<String> {
    let mut torn: BTreeMap<&str, (Vec<String>, Vec<String>)> = BTreeMap::new();
    for op in ops {
        match op {
            MigrationOp::DropRowPolicy { table, name } => {
                torn.entry(table).or_default().0.push(name.clone());
            }
            MigrationOp::NoForceRowSecurity { table } => {
                let statement = render_no_force_row_security(table);
                torn.entry(table).or_default().1.push(statement);
            }
            MigrationOp::DisableRowSecurity { table } => {
                let statement = render_disable_row_security(table);
                torn.entry(table).or_default().1.push(statement);
            }
            _ => {}
        }
    }
    torn.into_iter()
        .filter_map(|(table, (policies, flags))| {
            row_security_teardown_warning(table, &policies, &flags)
        })
        .collect()
}

/// Whether `op` is a row-security op.
pub fn is_row_security(op: &MigrationOp) -> bool {
    matches!(
        op,
        MigrationOp::AddRowPolicy { .. }
            | MigrationOp::RebuildRowPolicy { .. }
            | MigrationOp::DropRowPolicy { .. }
            | MigrationOp::EnableRowSecurity { .. }
            | MigrationOp::ForceRowSecurity { .. }
            | MigrationOp::DisableRowSecurity { .. }
            | MigrationOp::NoForceRowSecurity { .. }
    )
}

/// The Postgres row-security statements of the file turning `before` into
/// `after` whose plan is `ops`: its row-security ops, completed by
/// [`with_teardown`], rendered by the pass's renderers in the planner's order.
///
/// # Errors
/// An op that cannot render ([`crate::EmissionError`] from [`render_plan`]).
pub fn render(
    ops: &[MigrationOp],
    before: &IrEnvelope<SchemaIrPayload>,
    after: &IrEnvelope<SchemaIrPayload>,
) -> Result<Vec<String>, GenerateError> {
    let plan = MigrationPlan {
        operations: with_teardown(ops.to_vec(), before, after, Dialect::Postgres)
            .into_iter()
            .filter(is_row_security)
            .collect(),
        ..MigrationPlan::default()
    };
    Ok(render_plan(&plan, before, after, Dialect::Postgres)?
        .into_iter()
        .flat_map(|rendered| rendered.statements)
        .collect())
}

#[cfg(test)]
mod tests {
    use super::super::tests::{ir, order, tenant_policy};
    use super::*;

    fn flags(ops: &[MigrationOp]) -> Vec<MigrationOp> {
        ops.iter()
            .filter(|op| is_row_security(op))
            .cloned()
            .collect()
    }

    #[test]
    fn introduced_reads_the_two_snapshots_both_ways() {
        let none = ir(vec![order(None)]);
        let declared = ir(vec![order(Some((true, vec![tenant_policy("app.t")])))]);
        let bare = ir(vec![order(Some((false, Vec::new())))]);
        // The parent had none and the target declares it: introduced.
        assert!(introduced_by_migration("order", &none, &declared));
        assert!(introduced_by_migration("order", &none, &bare));
        // The other way round, or both sides declaring it: not introduced.
        assert!(!introduced_by_migration("order", &declared, &none));
        assert!(!introduced_by_migration("order", &declared, &bare));
        assert!(!introduced_by_migration("order", &none, &none));
        // A table the migration creates or drops: the table carries it.
        let empty = ir(vec![]);
        assert!(!introduced_by_migration("order", &empty, &declared));
        assert!(!introduced_by_migration("order", &declared, &empty));
        assert!(!introduced_by_migration("ledger", &none, &declared));
    }

    #[test]
    fn a_file_removing_a_declaration_tears_the_flags_down_once() {
        let table = || "order".to_string();
        let bare = ir(vec![order(Some((true, Vec::new())))]);
        let none = ir(vec![order(None)]);
        let column = MigrationOp::DropColumn {
            table: table(),
            column: "note".into(),
        };
        // No policy witnesses the flags: the file adds their teardown, ahead
        // of the table's column drops.
        let completed = with_teardown(vec![column.clone()], &bare, &none, Dialect::Postgres);
        assert_eq!(
            completed,
            [
                MigrationOp::NoForceRowSecurity { table: table() },
                MigrationOp::DisableRowSecurity { table: table() },
                column.clone(),
            ]
        );
        // Already planned (a policy witnessed them): nothing added.
        let planned = vec![
            MigrationOp::DropRowPolicy {
                table: table(),
                name: "rls_order_tenant_id".into(),
            },
            MigrationOp::NoForceRowSecurity { table: table() },
            MigrationOp::DisableRowSecurity { table: table() },
        ];
        let declared = ir(vec![order(Some((true, vec![tenant_policy("app.t")])))]);
        assert_eq!(
            with_teardown(planned.clone(), &declared, &none, Dialect::Postgres),
            planned
        );
        // Unforced: only DISABLE. SQLite: nothing, ever.
        let unforced = ir(vec![order(Some((false, Vec::new())))]);
        assert_eq!(
            flags(&with_teardown(
                Vec::new(),
                &unforced,
                &none,
                Dialect::Postgres
            )),
            [MigrationOp::DisableRowSecurity { table: table() }]
        );
        assert!(with_teardown(Vec::new(), &bare, &none, Dialect::Sqlite).is_empty());
        // A file adding row security, or keeping it, owes no teardown.
        assert!(with_teardown(Vec::new(), &none, &bare, Dialect::Postgres).is_empty());
        assert!(with_teardown(Vec::new(), &bare, &bare, Dialect::Postgres).is_empty());
    }
}
