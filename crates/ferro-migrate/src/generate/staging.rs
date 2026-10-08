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
//! Every statement is a pass renderer's in a mode (AGENTS.md § I-1):
//! `render_index_sql` / `render_drop_index_sql` in [`IndexMode::Concurrent`],
//! `render_add_fk_sql` / `render_check_addition` in
//! [`ConstraintMode::NotValid`], and `render_validate_constraint`.

use super::columns::{PlanContext, PlanDirection, stages_constraints};
use super::{GenerateError, GeneratedStep, Rendering, find_model, step_text};
use crate::directory::{Headers, StepDialect, StepKind};
use crate::emit::{
    column_riders, find_foreign_key, fk_constraint_name, render_add_fk_sql, render_index_sql,
    standalone_indexes,
};
use crate::{Dialect, MigrationOp};
use ferro_ddl_lowering::{
    ConstraintMode, IndexMode, render_check_addition, render_drop_constraint,
    render_drop_index_sql, render_validate_constraint,
};
use ferro_schema_ir::{IrEnvelope, SchemaIrPayload, SchemaModel};
use std::collections::BTreeMap;

/// One index as a model declares it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct IndexDef {
    /// Its table.
    pub table: String,
    /// Its name, which is also its index step's name.
    pub name: String,
    /// Its columns, in order.
    pub columns: Vec<String>,
    /// Whether it is unique.
    pub unique: bool,
}

/// What one index step does.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum IndexOp {
    /// Builds `def`; when the parent declared an index of the same name
    /// another way (`replaces`), the build's first statement drops it.
    Build {
        /// The index the target declares.
        def: IndexDef,
        /// The parent's index under that name, when there was one.
        replaces: Option<IndexDef>,
    },
    /// Drops `def`, an index the target no longer declares.
    Drop(IndexDef),
}

impl IndexOp {
    /// The index the step names.
    pub fn name(&self) -> &str {
        match self {
            IndexOp::Build { def, .. } | IndexOp::Drop(def) => &def.name,
        }
    }
}

fn declared_indexes(model: &SchemaModel) -> Vec<IndexDef> {
    standalone_indexes(model)
        .into_iter()
        .map(|(name, columns, unique)| IndexDef {
            table: model.table_name.clone(),
            name,
            columns,
            unique,
        })
        .collect()
}

/// The index steps of the migration turning `parent` into `target`: every
/// index added to, dropped from or redefined on a table both declare, table by
/// table in the target's order, drops first. An index on a table the
/// migration creates or drops rides the table's statement, and one over a
/// column the migration drops goes with the column; neither is a step.
pub fn index_ops(
    parent: &IrEnvelope<SchemaIrPayload>,
    target: &IrEnvelope<SchemaIrPayload>,
) -> Vec<IndexOp> {
    let mut ops = Vec::new();
    for after in &target.payload.models {
        let Some(before) = find_model(parent, &after.table_name) else {
            continue;
        };
        let old = declared_indexes(before);
        let new = declared_indexes(after);
        let has_column = |name: &String| after.columns.iter().any(|col| &col.name == name);
        for def in &old {
            if !new.iter().any(|index| index.name == def.name) && def.columns.iter().all(has_column)
            {
                ops.push(IndexOp::Drop(def.clone()));
            }
        }
        for def in new {
            let replaces = old.iter().find(|index| index.name == def.name);
            if replaces != Some(&def) {
                ops.push(IndexOp::Build {
                    replaces: replaces.cloned(),
                    def,
                });
            }
        }
    }
    ops
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
    declared_indexes(model)
        .iter()
        .any(|index| index.name == name)
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
/// index steps: `target` with every one of `ops` undone — an index a step
/// builds left out, an index a step drops or redefines kept as `parent`
/// declared it.
pub fn schema_shape(
    parent: &IrEnvelope<SchemaIrPayload>,
    target: &IrEnvelope<SchemaIrPayload>,
    ops: &[IndexOp],
) -> IrEnvelope<SchemaIrPayload> {
    let mut shape = target.clone();
    for op in ops {
        let (table, name, restore) = match op {
            IndexOp::Build { def, replaces } => (&def.table, &def.name, replaces.is_some()),
            IndexOp::Drop(def) => (&def.table, &def.name, true),
        };
        let (Some(model), Some(before)) = (
            shape
                .payload
                .models
                .iter_mut()
                .find(|model| &model.table_name == table),
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

/// The two statements that build `def` on `dialect`: on Postgres a drop of
/// whatever an earlier failed build left under the name, then the concurrent
/// build; on SQLite the plain build.
fn build(def: &IndexDef, dialect: Dialect) -> Vec<String> {
    match dialect {
        Dialect::Postgres => vec![
            render_drop_index_sql(&def.name, IndexMode::Concurrent),
            render_index_sql(
                &def.table,
                &def.name,
                &def.columns,
                def.unique,
                dialect,
                IndexMode::Concurrent,
            ),
        ],
        Dialect::Sqlite => vec![render_index_sql(
            &def.table,
            &def.name,
            &def.columns,
            def.unique,
            dialect,
            IndexMode::Plain,
        )],
    }
}

/// The statements that build `def` over another definition under its name
/// (ADR-0051): on Postgres [`build`] already drops first; on SQLite the plain
/// build's `IF NOT EXISTS` would keep the old one, so it is dropped first.
fn replace(def: &IndexDef, dialect: Dialect) -> Vec<String> {
    match dialect {
        Dialect::Postgres => build(def, dialect),
        Dialect::Sqlite => drop(def, dialect)
            .into_iter()
            .chain(build(def, dialect))
            .collect(),
    }
}

fn drop(def: &IndexDef, dialect: Dialect) -> Vec<String> {
    let mode = match dialect {
        Dialect::Postgres => IndexMode::Concurrent,
        Dialect::Sqlite => IndexMode::Plain,
    };
    vec![render_drop_index_sql(&def.name, mode)]
}

fn rendering(
    dialect: Dialect,
    up: (Vec<String>, bool),
    down: (Vec<String>, bool),
) -> (StepDialect, Rendering) {
    let headers = |data_dependent| Headers {
        no_transaction: dialect == Dialect::Postgres,
        data_dependent,
        ..Headers::default()
    };
    let (up_headers, down_headers) = (headers(up.1), headers(down.1));
    (
        dialect.into(),
        Rendering {
            up: step_text(&up_headers, &up.0),
            down: step_text(&down_headers, &down.0),
            headers: up_headers,
            down_headers,
        },
    )
}

/// The index step for `op`, rendered for every dialect in `dialects`, named
/// after its index (ADR-0044). On Postgres it is a no-transaction step, exact
/// from its first statement: a build drops whatever an earlier failed build
/// left under the name (`DROP INDEX CONCURRENTLY IF EXISTS`), then builds
/// `CONCURRENTLY`; a drop is `DROP INDEX CONCURRENTLY IF EXISTS`. On SQLite it
/// holds the plain statement in a transaction. A unique's build is
/// data-dependent (a duplicate fails it). Its down is the reverse step: a
/// build's down drops the index (or rebuilds the one it replaced), a drop's
/// down builds it back. The ordinal is the caller's to assign.
pub fn index_step(op: &IndexOp, dialects: &[Dialect]) -> GeneratedStep {
    let renderings = dialects
        .iter()
        .map(|&dialect| match op {
            IndexOp::Build { def, replaces } => match replaces {
                Some(old) => rendering(
                    dialect,
                    (replace(def, dialect), def.unique),
                    (replace(old, dialect), old.unique),
                ),
                None => rendering(
                    dialect,
                    (build(def, dialect), def.unique),
                    (drop(def, dialect), false),
                ),
            },
            IndexOp::Drop(def) => rendering(
                dialect,
                (drop(def, dialect), false),
                (build(def, dialect), def.unique),
            ),
        })
        .collect();
    GeneratedStep {
        ordinal: 0,
        name: op.name().to_string(),
        kind: StepKind::Ddl,
        renderings,
        data: None,
        hand_model: None,
    }
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

/// Every foreign key and check `ops` stage when the file turning `before`
/// into `after` renders them on `dialect` ([`stages_constraints`]), in the
/// order the file adds them: an added or rebuilt foreign key or check, and an
/// added column's own check and foreign key. Empty on SQLite.
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
    for op in ops {
        let ctx = PlanContext::of(op, before, after, dialect, PlanDirection::Up);
        let Some(model) = ctx.after.filter(|_| stages_constraints(&ctx)) else {
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
