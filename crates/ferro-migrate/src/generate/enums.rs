//! Enum changes in a generated migration (ADR-0011, ADR-0032, ADR-0033).
//!
//! ```python
//! class OrderStatus(StrEnum):
//!     __ferro_renamed_labels__ = {"cancelled": "canceled"}   # new label → old label
//!     PAID = "paid"
//!     CANCELLED = "cancelled"
//!     REFUNDED = "refunded"                                 # new
//! ```
//!
//! ```text
//! 0007_status/
//!   01_labels.up.postgres.sql     ALTER TYPE "orderstatus" ADD VALUE IF NOT EXISTS 'refunded';
//!   01_labels.down.postgres.sql   -- ferro: nothing-to-reverse Postgres cannot drop an enum label; 'refunded' stays
//!   01_labels.up.sqlite.sql       -- ferro: not-applicable
//!   02_schema.up.postgres.sql     ALTER TYPE "orderstatus" RENAME VALUE 'canceled' TO 'cancelled';
//!   02_schema.up.sqlite.sql       UPDATE "order" SET "status" = 'cancelled' WHERE "status" = 'canceled';
//! ```
//!
//! A label addition is its own `labels` step, first in the migration, so every
//! later step can write the label once it is committed (I-12's before-tables
//! slot). Its statement is the reconciliation pass's (`render_pg_enum_add_value`
//! through [`render_plan`]); its down reverses nothing, because Postgres cannot
//! drop an enum label, and says so. SQLite stores labels as text: nothing to do.
//!
//! A label rename and a type rename are rename ops of the `schema` step,
//! rendered and reversed there like every other rename
//! ([`super::downs::render_down`]). The `CREATE TYPE` / `DROP TYPE` of a type a
//! migration introduces or retires ride the `schema` step with the tables that
//! carry them, decided by `enum_type_provenance` in the planner.
//!
//! A label removed (D2, `MigrationOp::RemoveEnumLabel`) may still be held by
//! rows, so it is a demand ([`super::backfill`]): a backfill over the rows
//! holding it, then the contract — on Postgres the swap-type recipe
//! ([`render_swap_type`]), whose down puts the label back with the pass's
//! `ADD VALUE IF NOT EXISTS`; on SQLite, where labels are text in the rows,
//! nothing beyond the backfill (a check or a narrower column is the table's
//! rebuild). A down meeting the removal of a label its up added is answered
//! by the `labels` step's down, which says the label stays.

use super::{GenerateError, Rendering, refuse_unrendered, step_text};
use crate::directory::Headers;
use crate::{Dialect, MigrationOp, MigrationPlan, render_plan};
use ferro_ddl_lowering::{quote_ident, quote_label};
use ferro_schema_ir::{IrEnvelope, SchemaIrPayload};
use std::collections::BTreeMap;

/// Whether `op` belongs in the `labels` step.
pub fn is_label_addition(op: &MigrationOp) -> bool {
    matches!(op, MigrationOp::AddEnumLabel { .. })
}

/// The labels `ops` add, per type, in plan order.
fn added_labels(ops: &[MigrationOp]) -> BTreeMap<&str, Vec<String>> {
    let mut added: BTreeMap<&str, Vec<String>> = BTreeMap::new();
    for op in ops {
        if let MigrationOp::AddEnumLabel { type_name, label } = op {
            added.entry(type_name).or_default().push(label.clone());
        }
    }
    added
}

/// One column a [`render_swap_type`] moves to the new type.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SwapColumn {
    /// The table.
    pub table: String,
    /// The column.
    pub column: String,
    /// The column's default label, if it declares one: Postgres cannot cast
    /// a default to the new type, so it is dropped first and set again after.
    pub default: Option<String>,
}

/// The swap-type contract of a label removal on Postgres (D2, ADR-0032's
/// recipe), which has no `ALTER TYPE … DROP VALUE`:
///
/// ```sql
/// CREATE TYPE "orderstatus_new" AS ENUM ('paid', 'refunded');
/// ALTER TABLE "order" ALTER COLUMN "status" TYPE "orderstatus_new" USING "status"::text::"orderstatus_new";
/// DROP TYPE "orderstatus";
/// ALTER TYPE "orderstatus_new" RENAME TO "orderstatus";
/// ```
///
/// One `ALTER COLUMN … TYPE` per column of the type, in `columns`' order (a
/// type two tables share is one swap covering both), each between a
/// `DROP DEFAULT` and a `SET DEFAULT` when the column has a default. The cast
/// fails on a row still holding a removed label (`invalid input value for
/// enum`), which the runner counts with the recipe that re-runs the backfill.
pub fn render_swap_type(
    type_name: &str,
    labels_after: &[String],
    columns: &[SwapColumn],
) -> Vec<String> {
    let staged = format!("{type_name}_new");
    let labels: Vec<String> = labels_after.iter().map(|l| quote_label(l)).collect();
    let mut out = vec![format!(
        "CREATE TYPE {} AS ENUM ({})",
        quote_ident(&staged),
        labels.join(", ")
    )];
    for col in columns {
        let alter = format!(
            "ALTER TABLE {} ALTER COLUMN {}",
            quote_ident(&col.table),
            quote_ident(&col.column)
        );
        if col.default.is_some() {
            out.push(format!("{alter} DROP DEFAULT"));
        }
        out.push(format!(
            "{alter} TYPE {staged_q} USING {col_q}::text::{staged_q}",
            staged_q = quote_ident(&staged),
            col_q = quote_ident(&col.column),
        ));
        if let Some(label) = &col.default {
            out.push(format!("{alter} SET DEFAULT {}", quote_label(label)));
        }
    }
    out.push(format!("DROP TYPE {}", quote_ident(type_name)));
    out.push(format!(
        "ALTER TYPE {} RENAME TO {}",
        quote_ident(&staged),
        quote_ident(type_name)
    ));
    out
}

/// `'a'`, `'a' and 'b'`, `'a', 'b' and 'c'`.
fn quoted_list(labels: &[String]) -> String {
    let quoted: Vec<String> = labels.iter().map(|label| quote_label(label)).collect();
    match quoted.as_slice() {
        [] => String::new(),
        [only] => only.clone(),
        [init @ .., last] => format!("{} and {last}", init.join(", ")),
    }
}

/// The `labels` step on `dialect`: the pass's `ADD VALUE IF NOT EXISTS` for
/// each label `step_ops` adds, and a down that reverses nothing and names the
/// labels that stay. A dialect with no label to add (SQLite) is
/// `not-applicable` both ways.
///
/// # Errors
/// An op that cannot render, or that renders only a warning.
pub fn render_labels_step(
    step_ops: &[MigrationOp],
    before: &IrEnvelope<SchemaIrPayload>,
    after: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
) -> Result<Rendering, GenerateError> {
    debug_assert!(step_ops.iter().all(is_label_addition), "{step_ops:?}");
    let plan = MigrationPlan {
        operations: step_ops.to_vec(),
        ..MigrationPlan::default()
    };
    let rendered = render_plan(&plan, before, after, dialect)?;
    refuse_unrendered(&rendered, dialect)?;
    let statements: Vec<String> = rendered
        .into_iter()
        .flat_map(|rendered| rendered.statements)
        .collect();
    let headers = Headers {
        not_applicable: statements.is_empty(),
        ..Headers::default()
    };
    let labels: Vec<String> = added_labels(step_ops).into_values().flatten().collect();
    let stays = if labels.len() == 1 { "stays" } else { "stay" };
    let down_headers = if statements.is_empty() {
        Headers {
            not_applicable: true,
            ..Headers::default()
        }
    } else {
        Headers {
            nothing_to_reverse: Some(format!(
                "Postgres cannot drop an enum label; {} {stays}",
                quoted_list(&labels)
            )),
            ..Headers::default()
        }
    };
    Ok(Rendering {
        up: step_text(&headers, &statements),
        down: step_text(&down_headers, &[]),
        headers,
        down_headers,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_swap_creates_the_new_type_moves_each_column_then_drops_and_renames() {
        let columns = [
            SwapColumn {
                table: "order".into(),
                column: "status".into(),
                default: Some("paid".into()),
            },
            SwapColumn {
                table: "refund".into(),
                column: "state".into(),
                default: None,
            },
        ];
        assert_eq!(
            render_swap_type(
                "orderstatus",
                &["paid".to_string(), "it's".to_string()],
                &columns
            ),
            [
                "CREATE TYPE \"orderstatus_new\" AS ENUM ('paid', 'it''s')",
                "ALTER TABLE \"order\" ALTER COLUMN \"status\" DROP DEFAULT",
                "ALTER TABLE \"order\" ALTER COLUMN \"status\" TYPE \"orderstatus_new\" USING \
                 \"status\"::text::\"orderstatus_new\"",
                "ALTER TABLE \"order\" ALTER COLUMN \"status\" SET DEFAULT 'paid'",
                "ALTER TABLE \"refund\" ALTER COLUMN \"state\" TYPE \"orderstatus_new\" USING \
                 \"state\"::text::\"orderstatus_new\"",
                "DROP TYPE \"orderstatus\"",
                "ALTER TYPE \"orderstatus_new\" RENAME TO \"orderstatus\"",
            ]
        );
    }
}
