//! Row security in a generated migration (ADR-0019, ADR-0033).
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
//! The generator writes the planner's row-security ops into the table's schema
//! step, in the planner's order, and decides nothing about them: between two
//! declared snapshots [`crate::plan_from_ir`] itself rebuilds an edited raw
//! body and tears down row security the parent snapshot declared and the
//! target dropped, policies or none (the parent is the proof ferro installed
//! it). [`introduced_by_migration`] names that teardown from the migration's
//! side, for a caller that reverses a migration without planning it (the
//! Alembic bridge's `downgrade()`).

use ferro_schema_ir::{IrEnvelope, SchemaIrPayload, SchemaModel};

/// Whether the migration turning `before` into `after` introduced row
/// security on `table`: the table exists on both sides, `before` declares no
/// row security on it and `after` does. Its down then tears the flags down,
/// because the parent snapshot shows nobody else set them (ADR-0033) — the
/// planner's own rule between two snapshots, pinned against it. A table the
/// migration creates is not one: its down's `DROP TABLE` takes the policies
/// and the flags with it.
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

#[cfg(test)]
mod tests {
    use super::super::tests::{ir, order, tenant_policy};
    use super::*;
    use crate::{Dialect, LiveFacts, MigrationOp, PlanOptions, plan_from_ir};

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
    fn introduced_is_exactly_when_the_planners_down_disables_row_security() {
        let sides = [
            ir(vec![order(None)]),
            ir(vec![order(Some((true, vec![tenant_policy("app.t")])))]),
            ir(vec![order(Some((false, vec![tenant_policy("app.t")])))]),
            ir(vec![order(Some((true, Vec::new())))]),
            ir(vec![order(Some((false, Vec::new())))]),
        ];
        for before in &sides {
            for after in &sides {
                let down = plan_from_ir(
                    after,
                    before,
                    Dialect::Postgres,
                    &LiveFacts::declared(),
                    PlanOptions { destructive: true },
                )
                .expect("plan");
                let disables = down.operations.contains(&MigrationOp::DisableRowSecurity {
                    table: "order".into(),
                });
                assert_eq!(
                    disables,
                    introduced_by_migration("order", before, after),
                    "{before:?} → {after:?}"
                );
            }
        }
    }
}
