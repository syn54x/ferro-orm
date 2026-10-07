//! Declared renames in a generated migration (ADR-0032).
//!
//! ```python
//! class Author(Model):
//!     __ferro_renamed_from__ = "writer"
//!     full_name: str = Field(renamed_from="name")
//! ```
//!
//! ```text
//! 0002_rename_writer/01_schema.up.postgres.sql
//!   ALTER TABLE "writer" RENAME TO "author";
//!   ALTER TABLE "author" RENAME COLUMN "name" TO "full_name";
//!   ALTER INDEX "idx_writer_name" RENAME TO "idx_author_full_name";
//!   ALTER TABLE "book" RENAME CONSTRAINT "fk_book_writer_id_writer" TO "fk_book_writer_id_author";
//! ```
//!
//! The planner decides the renames ([`crate::plan::live_hints`],
//! [`crate::plan_from_ir`], which applies them); this module is the generator's
//! side of them: the two refusals as a [`GenerateError`], which renames run
//! natively before everything else in their step (a table's and a column's —
//! the rest of the step, a SQLite rebuild included, reads the table under its
//! new names), and the summary line a hintless drop and add earns.

use super::GenerateError;
use crate::MigrationOp;
use crate::plan::{Hint, live_hints};
use ferro_schema_ir::{IrEnvelope, SchemaIrPayload};

/// The live rename hints `target` declares against `parent`.
///
/// # Errors
/// [`GenerateError::Hint`] for a hint whose old name is still declared or
/// that another hint claims too; `new` writes nothing (exit 1).
pub fn live(
    parent: &IrEnvelope<SchemaIrPayload>,
    target: &IrEnvelope<SchemaIrPayload>,
) -> Result<Vec<Hint>, GenerateError> {
    live_hints(&parent.payload, &target.payload).map_err(GenerateError::Hint)
}

/// Whether `op` is a rename op.
pub fn is_rename(op: &MigrationOp) -> bool {
    matches!(
        op,
        MigrationOp::RenameTable { .. }
            | MigrationOp::RenameColumn { .. }
            | MigrationOp::RenameIndex { .. }
            | MigrationOp::RenameConstraint { .. }
            | MigrationOp::RenamePolicy { .. }
    )
}

/// Whether `op` renames a table or a column: native on every dialect, and run
/// first in its step, so every other statement of the step — a derived
/// name's, a SQLite rebuild — finds the table under its new names.
pub fn is_structural(op: &MigrationOp) -> bool {
    matches!(
        op,
        MigrationOp::RenameTable { .. } | MigrationOp::RenameColumn { .. }
    )
}

/// The lines `new` prints for a column dropped and another added on one table
/// with no hint between them (ADR-0032): the generator renders exactly that,
/// marked destructive, and never guesses a rename, so it names the
/// declaration that would. A model dropped beside one added gets no line: two
/// models are as often two models, and the destructive `DROP TABLE` already
/// says what it costs.
///
/// ```text
/// author: if "name" became "full_name", declare renamed_from="name"
/// ```
pub fn suggestions(
    ops: &[MigrationOp],
    parent: &IrEnvelope<SchemaIrPayload>,
    target: &IrEnvelope<SchemaIrPayload>,
) -> Vec<String> {
    let is_fk = |ir: &IrEnvelope<SchemaIrPayload>, table: &str, column: &str| {
        ir.payload
            .models
            .iter()
            .filter(|model| model.table_name == table)
            .flat_map(|model| &model.foreign_keys)
            .any(|fk| fk.column == column)
    };
    let mut lines = Vec::new();
    for op in ops {
        let MigrationOp::DropColumn { table, column: old } = op else {
            continue;
        };
        for added in ops {
            let MigrationOp::AddColumn {
                table: t,
                column: new,
            } = added
            else {
                continue;
            };
            if t != table {
                continue;
            }
            // A relation field's hint names the field, not its shadow
            // `<field>_id` column.
            let declaration = match (
                is_fk(parent, table, old) && is_fk(target, table, new),
                old.strip_suffix("_id"),
            ) {
                (true, Some(field)) => format!("ForeignKey(renamed_from=\"{field}\")"),
                _ => format!("renamed_from=\"{old}\""),
            };
            lines.push(format!(
                "{table}: if \"{old}\" became \"{new}\", declare {declaration}"
            ));
        }
    }
    lines
}
