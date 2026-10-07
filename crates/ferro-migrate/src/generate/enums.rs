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

use super::{GenerateError, Rendering, refuse_unrendered, step_text};
use crate::directory::Headers;
use crate::{Dialect, MigrationOp, MigrationPlan, render_plan};
use ferro_ddl_lowering::{extra_enum_labels_warning, quote_label};
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

/// The warnings the down of a migration adding `ops`' labels raises and
/// answers itself: the planner run backwards reports each added label as one
/// the model no longer declares (warn-never-act, ADR-0011), and the `labels`
/// step's down already says the label stays.
pub fn answered_by_labels_step(ops: &[MigrationOp]) -> Vec<String> {
    added_labels(ops)
        .into_iter()
        .filter_map(|(type_name, labels)| extra_enum_labels_warning(type_name, &labels))
        .collect()
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
