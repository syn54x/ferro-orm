//! Online shapes for a table that already exists (ADR-0043, ADR-0044): its
//! foreign keys and checks staged `NOT VALID` and validated by a later step,
//! and each of its indexes built or dropped by its own index step.
//!
//! ```text
//! class Author(Model):                     0005_author_email_unique/
//!     email: str | None = Field(unique=True)  01_schema    ADD CONSTRAINT "ck_author_email_nonempty" … NOT VALID
//!     __ferro_checks__ = (                    02_uq_author_email
//!         Check("email_nonempty", …),                      -- ferro: no-transaction
//!     )                                                    DROP INDEX CONCURRENTLY IF EXISTS "uq_author_email"
//!                                                          CREATE UNIQUE INDEX CONCURRENTLY "uq_author_email" …
//!                                         03_validate  VALIDATE CONSTRAINT "ck_author_email_nonempty"
//! ```
//!
//! The migration's steps split at one intermediate snapshot
//! ([`schema_shape`]): the target with every index step's change undone.
//! Every step before the index steps turns the parent into that shape, so a
//! SQLite rebuild recreates the indexes as they stand after its step and an
//! index a later step builds is never built twice (ADR-0046); the index steps
//! turn it into the target.
//!
//! Every statement is a pass renderer's in a mode (AGENTS.md § I-1): an
//! index step is the planner's index op rendered in
//! [`IndexMode::Concurrent`], its down [`plan_down`] in the same mode;
//! `render_add_fk_sql` / `render_check_addition` in
//! [`ConstraintMode::NotValid`], and `render_validate_constraint`.

use super::{GenerateError, GeneratedStep, Rendering, find_model, step_text};
use crate::directory::{Headers, StepDialect, StepKind};
use crate::emit::{
    column_riders, find_foreign_key, fk_constraint_name, render_add_fk_sql, standalone_indexes,
};
use crate::render::render_ops;
use crate::{Dialect, MigrationOp, Side, declared_index, plan_down};
use ferro_ddl_lowering::{
    ConstraintMode, IndexMode, render_check_addition, render_drop_constraint,
    render_validate_constraint,
};
use ferro_schema_ir::{IrEnvelope, SchemaIrPayload, SchemaModel};
use std::collections::BTreeMap;

/// The table and the index an index step names: the planner's `AddIndex`,
/// `RedefineIndex` or `DropIndex`, or `None` for any other op.
pub(super) fn index_of(op: &MigrationOp) -> Option<(&str, &str)> {
    match op {
        MigrationOp::AddIndex { table, name, .. }
        | MigrationOp::RedefineIndex { table, name }
        | MigrationOp::DropIndex { table, name } => Some((table, name)),
        _ => None,
    }
}

/// Remove the index `name` from `model`: its `indexes` / `uniques` entry and
/// the column flag that declares it.
fn remove_index(model: &mut SchemaModel, name: &str) {
    model.indexes.retain(|index| index.name != name);
    model.uniques.retain(|unique| unique.name != name);
    let flags: Vec<(String, bool, bool)> = model
        .columns
        .iter()
        .map(|col| {
            let riders = column_riders(model, &col.name);
            (
                col.name.clone(),
                riders.unique.as_deref() == Some(name),
                riders.index.as_deref() == Some(name),
            )
        })
        .collect();
    for (column, unique, index) in flags {
        if let Some(col) = model.columns.iter_mut().find(|col| col.name == column) {
            col.unique &= !unique;
            col.index &= !index;
        }
    }
}

/// Whether `model` declares the index `name`, by an entry or a column flag.
pub(super) fn declares_index(model: &SchemaModel, name: &str) -> bool {
    standalone_indexes(model)
        .iter()
        .any(|(declared, ..)| declared == name)
}

/// Put `parent`'s declaration of the index `name` back into `model`: its
/// `indexes` / `uniques` entry and the column flag that declared it.
pub(super) fn restore_index(model: &mut SchemaModel, parent: &SchemaModel, name: &str) {
    if let Some(index) = parent.indexes.iter().find(|index| index.name == name) {
        model.indexes.push(index.clone());
    }
    if let Some(unique) = parent.uniques.iter().find(|unique| unique.name == name) {
        model.uniques.push(unique.clone());
    }
    for parent_col in &parent.columns {
        let riders = column_riders(parent, &parent_col.name);
        if let Some(col) = model
            .columns
            .iter_mut()
            .find(|col| col.name == parent_col.name)
        {
            col.unique |= riders.unique.as_deref() == Some(name);
            col.index |= riders.index.as_deref() == Some(name);
        }
    }
}

/// The schema as it stands between the migration's earlier steps and its
/// index steps: `target` with every one of the index steps' `ops` undone —
/// an index an `AddIndex` builds left out, an index a `DropIndex` or a
/// `RedefineIndex` changes kept as `parent` declared it. Every stage of a
/// migration is built this way, as the target with the later steps' ops
/// undone (ADR-0050 § "The generator plans once", as built).
pub fn schema_shape(
    parent: &IrEnvelope<SchemaIrPayload>,
    target: &IrEnvelope<SchemaIrPayload>,
    ops: &[MigrationOp],
) -> IrEnvelope<SchemaIrPayload> {
    let mut shape = target.clone();
    for op in ops {
        let Some((table, name)) = index_of(op) else {
            continue;
        };
        let restore = !matches!(op, MigrationOp::AddIndex { .. });
        let (Some(model), Some(before)) = (
            shape
                .payload
                .models
                .iter_mut()
                .find(|model| model.table_name == table),
            find_model(parent, table),
        ) else {
            continue;
        };
        remove_index(model, name);
        if restore {
            restore_index(model, before, name);
        }
    }
    shape
}

/// Whether `ops` build a unique index, as `new` declares it: a duplicate
/// fails the build, so the file is data-dependent.
fn builds_unique(ops: &[MigrationOp], new: &IrEnvelope<SchemaIrPayload>) -> bool {
    ops.iter().any(|op| match op {
        MigrationOp::AddIndex { unique, .. } => *unique,
        MigrationOp::RedefineIndex { table, name } => {
            declared_index(new, table, name).is_some_and(|(_, unique)| unique)
        }
        _ => false,
    })
}

/// The index step for `op`, rendered for every dialect in `dialects`, named
/// after its index (ADR-0044), between the stages `before` and `after` on
/// either side of it. Its up is the op rendered by the one renderer in
/// [`IndexMode::Concurrent`]: on Postgres a no-transaction step, exact from
/// its first statement (a build drops whatever an earlier failed build left
/// under the name, then builds `CONCURRENTLY`; a drop is `DROP INDEX
/// CONCURRENTLY IF EXISTS`); on SQLite the plain statement in a transaction.
/// Its down is [`plan_down`]`([op], after, before)` rendered in the same
/// mode. A unique's build is data-dependent (a duplicate fails it). The
/// ordinal is the caller's to assign.
///
/// # Errors
/// An op that cannot render.
pub(super) fn index_step(
    op: &MigrationOp,
    before: &IrEnvelope<SchemaIrPayload>,
    after: &IrEnvelope<SchemaIrPayload>,
    dialects: &[Dialect],
) -> Result<GeneratedStep, GenerateError> {
    let Some((_, name)) = index_of(op) else {
        return Err(GenerateError::Render(format!(
            "an index step runs an index op, not {op:?}"
        )));
    };
    let up_ops = vec![op.clone()];
    let mut renderings = BTreeMap::new();
    for &dialect in dialects {
        let up: Vec<String> = render_ops(
            &up_ops,
            before,
            after,
            dialect,
            ConstraintMode::Plain,
            IndexMode::Concurrent,
        )?
        .into_iter()
        .flat_map(|rendered| rendered.statements)
        .collect();
        let back = plan_down(
            &up_ops,
            &Side::declared(after.clone()),
            &Side::declared(before.clone()),
            dialect,
        );
        let all: Vec<usize> = (0..back.operations.len()).collect();
        let down: Vec<String> = back
            .render_in(ConstraintMode::Plain, IndexMode::Concurrent, &all)?
            .into_iter()
            .flat_map(|rendered| rendered.statements)
            .collect();
        let down_ops: Vec<MigrationOp> = back.ops().cloned().collect();
        let headers = |data_dependent| Headers {
            no_transaction: dialect == Dialect::Postgres,
            data_dependent,
            ..Headers::default()
        };
        let up_headers = headers(builds_unique(&up_ops, after));
        let down_headers = headers(builds_unique(&down_ops, before));
        renderings.insert(
            StepDialect::from(dialect),
            Rendering {
                up: step_text(&up_headers, &up),
                down: step_text(&down_headers, &down),
                headers: up_headers,
                down_headers,
            },
        );
    }
    Ok(GeneratedStep {
        ordinal: 0,
        name: name.to_string(),
        kind: StepKind::Ddl,
        renderings,
        data: None,
        hand_model: None,
    })
}

/// One foreign key or check a step added `NOT VALID`, which a later step
/// validates.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StagedConstraint {
    /// Its table.
    pub table: String,
    /// Its name.
    pub name: String,
    /// The `NOT VALID` add that installed it, as the step rendered it.
    pub add: Vec<String>,
}

/// Every foreign key and check `ops` stage when the up file turning `before`
/// into `after` renders them on `dialect`, in the order the file adds them:
/// an added or rebuilt foreign key or check, and an added column's own check
/// and foreign key. Every Postgres up file stages them on a table that
/// already exists (a new table's ride its `CREATE TABLE`) and validates them
/// in a later step (ADR-0043); SQLite has no unvalidated constraint (a
/// rebuild validates by copying), so it stages none.
///
/// # Errors
/// [`GenerateError::Render`] when an op names a constraint `after` does not
/// declare.
pub fn staged_constraints(
    ops: &[MigrationOp],
    before: &IrEnvelope<SchemaIrPayload>,
    after: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
) -> Result<Vec<StagedConstraint>, GenerateError> {
    let mut staged = Vec::new();
    if dialect != Dialect::Postgres {
        return Ok(staged);
    }
    for op in ops {
        let Some(table) = op.table() else {
            continue;
        };
        let (Some(_), Some(model)) = (find_model(before, table), find_model(after, table)) else {
            continue;
        };
        let table = &model.table_name;
        let check = |name: &str| -> Result<StagedConstraint, GenerateError> {
            let add = render_check_addition(table, model, name, dialect, ConstraintMode::NotValid)
                .and_then(|emission| emission.statement)
                .ok_or_else(|| {
                    GenerateError::Render(format!(
                        "check '{name}' on '{table}' is not declared, so it cannot be staged"
                    ))
                })?;
            Ok(StagedConstraint {
                table: table.clone(),
                name: name.to_string(),
                add: vec![add],
            })
        };
        let foreign_key = |column: &str| -> Result<StagedConstraint, GenerateError> {
            let fk = find_foreign_key(model, table, column)?;
            Ok(StagedConstraint {
                table: table.clone(),
                name: fk_constraint_name(table, fk),
                add: vec![render_add_fk_sql(table, fk, ConstraintMode::NotValid)],
            })
        };
        match op {
            MigrationOp::AddCheck { name, .. } | MigrationOp::RebuildCheck { name, .. } => {
                staged.push(check(name)?);
            }
            MigrationOp::AddForeignKey { column, .. }
            | MigrationOp::RebuildForeignKey { column, .. } => staged.push(foreign_key(column)?),
            MigrationOp::AddColumn { column, .. } => {
                let riders = column_riders(model, column);
                if let Some(own) = riders.check {
                    staged.push(check(&own.name)?);
                }
                if riders.foreign_key.is_some() {
                    staged.push(foreign_key(column)?);
                }
            }
            _ => {}
        }
    }
    Ok(staged)
}

/// The validate step for `constraints`, rendered for every dialect in
/// `dialects` (ADR-0043): on Postgres the data-dependent step that validates
/// each, whose down drops each and adds it back `NOT VALID` (there is no
/// un-validate, and the down reaches the state its parent step left); on
/// SQLite, which validates by rebuilding in the earlier step, the one-line
/// `-- ferro: not-applicable` file both ways. The caller generates it only
/// when a dialect stages a constraint. The ordinal is the caller's to assign.
pub fn validate_step(constraints: &[StagedConstraint], dialects: &[Dialect]) -> GeneratedStep {
    let mut renderings = BTreeMap::new();
    for &dialect in dialects {
        let (up, down) = match dialect {
            Dialect::Postgres => (
                constraints
                    .iter()
                    .map(|c| render_validate_constraint(&c.table, &c.name))
                    .collect(),
                constraints
                    .iter()
                    .flat_map(|c| {
                        std::iter::once(render_drop_constraint(&c.table, &c.name))
                            .chain(c.add.iter().cloned())
                    })
                    .collect(),
            ),
            Dialect::Sqlite => (Vec::new(), Vec::new()),
        };
        let headers = Headers {
            data_dependent: !up.is_empty(),
            not_applicable: up.is_empty(),
            ..Headers::default()
        };
        let down_headers = Headers {
            not_applicable: down.is_empty(),
            ..Headers::default()
        };
        renderings.insert(
            StepDialect::from(dialect),
            Rendering {
                up: step_text(&headers, &up),
                down: step_text(&down_headers, &down),
                headers,
                down_headers,
            },
        );
    }
    GeneratedStep {
        ordinal: 0,
        name: "validate".to_string(),
        kind: StepKind::Ddl,
        renderings,
        data: None,
        hand_model: None,
    }
}
