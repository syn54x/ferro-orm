//! The generator plans once (ADR-0050): for each target dialect, one plan
//! from the parent snapshot to the target, each of its ops placed in the
//! step it lands in by its verdict ([`super::columns::phase`]), and the
//! schemas between the steps built from those placements.
//!
//! ```text
//! class Author(Model):          plan(parent → target), once per dialect
//!     slug: str   # new    ──▶  AddColumn  author.slug   demands values → the expand, nullable
//!     # name: str (dropped)     DropColumn author.name   drops data     → the contract
//!
//! stages:   before ──01_expand──▶ expanded ──index steps──▶ kept ──04_contract──▶ target
//!                   ADD "slug" (nullable)                          SET NOT NULL; DROP "name"
//! ```
//!
//! Each stage is the target with what the later steps do still undone: an
//! index an index step changes as the parent declares it, a demanded column
//! nullable, a removed enum label still declared, a table or column the
//! contract drops still there. A phase is assigned once, from the plan's
//! verdict; no stage is planned again, so no op can land in a step its phase
//! does not name.

use super::backfill::{self, Demand};
use super::columns::Phase;
use super::staging;
use super::{GenerateError, find_model, phase_of, plan, refuse_unsupported};
use crate::emit::{column_riders, standalone_indexes};
use crate::plan::declared_label_additions;
use crate::{Dialect, MigrationOp, Plan, Rider};
use ferro_ddl_lowering::schema_columns_storage_drift;
use ferro_schema_ir::{IrEnvelope, SchemaIrPayload};
use std::collections::BTreeSet;

/// The schemas between a migration's steps.
pub(super) struct Stages {
    /// The parent snapshot as the migration's declared renames leave it.
    pub before: IrEnvelope<SchemaIrPayload>,
    /// After the labels and schema (or expand) steps: every index an index
    /// step changes as the parent declares it, every demanded column
    /// nullable, every removed label still declared, and every table and
    /// column the contract drops still there (ADR-0025).
    pub expanded: IrEnvelope<SchemaIrPayload>,
    /// After the index steps: the target with every demanded column
    /// nullable, every removed label still declared, and what the contract
    /// drops still there. The contract starts here.
    pub kept: IrEnvelope<SchemaIrPayload>,
    /// [`Self::kept`] without what the contract drops: where the contract's
    /// drops lead, and where its `NOT NULL` and label changes start.
    pub loose: IrEnvelope<SchemaIrPayload>,
}

/// One dialect's plan, its ops placed in the steps they land in, each list
/// in plan order.
pub(super) struct Placed {
    /// The dialect.
    pub dialect: Dialect,
    /// The plan from the parent to the target.
    pub plan: Plan,
    /// The `labels` step's ops: each label added to an existing enum type.
    pub labels: Vec<MigrationOp>,
    /// The `schema` (or `expand`) step's ops, a demanded column's add among
    /// them, which the step renders nullable.
    pub schema: Vec<MigrationOp>,
    /// The ops the contract drops after the data steps (ADR-0025).
    pub held: Vec<MigrationOp>,
    /// The contract's removed labels (D2): each label removal, and each
    /// change of a column of its type the removal makes (the column's check
    /// rebuilt to the labels left, its storage narrowed).
    pub relabel: Vec<MigrationOp>,
}

/// A migration's ops, placed, and the stages between its steps.
pub(super) struct Layout {
    /// The stages.
    pub stages: Stages,
    /// One placement per target dialect, in the dialects' order.
    pub placed: Vec<Placed>,
    /// The index steps' ops, one per step, in their order (ADR-0044,
    /// ADR-0051).
    pub index_ops: Vec<MigrationOp>,
    /// Every column whose existing rows need a value, parents first.
    pub demands: Vec<Demand>,
    /// Whether the migration backfills (a demanded column, a removed label):
    /// its schema step is the expand and it has a contract.
    pub backfills: bool,
    /// `type.label` for every label the models add, on any dialect.
    pub labels_added: BTreeSet<String>,
}

impl Layout {
    /// Plan `parent` → `target` once per dialect and place every op:
    /// `before` is `parent` as the declared renames leave it, and
    /// `hand_data_step` whether a person adds a data step (whose contract
    /// then drops what the step may read).
    ///
    /// # Errors
    /// An op no door runs (its refusal), or a report no op answers.
    pub(super) fn plan(
        parent: &IrEnvelope<SchemaIrPayload>,
        before: IrEnvelope<SchemaIrPayload>,
        target: &IrEnvelope<SchemaIrPayload>,
        dialects: &[Dialect],
        hand_data_step: bool,
    ) -> Result<Self, GenerateError> {
        let mut plans = Vec::new();
        for &dialect in dialects {
            let planned = plan(parent, target, dialect);
            refuse_unsupported(&planned)?;
            plans.push(planned);
        }
        let index_ops = index_ops(&plans, &before, target)?;
        let shape = staging::schema_shape(&before, target, &index_ops);
        let every: Vec<_> = plans
            .iter()
            .flat_map(|planned| planned.operations.iter().cloned())
            .collect();
        let demands = backfill::collect(&every, &before, &shape);
        let ops: Vec<MigrationOp> = every.iter().map(|planned| planned.op.clone()).collect();
        let removals = backfill::label_removals(&ops);
        let backfills = !demands.is_empty() || !removals.is_empty();
        let data_steps = backfills || hand_data_step;

        let mut placed = Vec::new();
        for (&dialect, planned) in dialects.iter().zip(plans) {
            placed.push(place(dialect, planned, data_steps)?);
        }
        let held: Vec<MigrationOp> = placed.iter().flat_map(|p| p.held.clone()).collect();
        let expanded = backfill::with_drops_kept(
            &backfill::with_removed_labels(
                &backfill::relaxed(&shape, &demands),
                &before,
                &removals,
            ),
            &before,
            &held,
        );
        let loose =
            backfill::with_removed_labels(&backfill::relaxed(target, &demands), &before, &removals);
        let kept = backfill::with_drops_kept(&loose, &before, &held);
        // A removed label's change of a column that the same migration also
        // widens (a label added beside it) is the expand's first, so the
        // backfill can write the added label, and the contract's after.
        for placement in &mut placed {
            let widened: Vec<MigrationOp> = placement
                .relabel
                .iter()
                .filter(|op| widens(op, &before, &expanded, placement.dialect))
                .cloned()
                .collect();
            if !widened.is_empty() {
                placement.schema = placement
                    .plan
                    .ops()
                    .filter(|op| placement.schema.contains(op) || widened.contains(op))
                    .cloned()
                    .collect();
            }
        }
        let labels_added = declared_label_additions(&before, target)
            .into_iter()
            .map(|(type_name, label)| format!("{type_name}.{label}"))
            .collect();
        Ok(Self {
            stages: Stages {
                before,
                expanded,
                kept,
                loose,
            },
            placed,
            index_ops,
            demands,
            backfills,
            labels_added,
        })
    }

    /// The stages on either side of the index step at `at`: the target with
    /// that step's change and every later one undone, and with only the
    /// later ones undone.
    pub(super) fn index_stages(
        &self,
        at: usize,
        target: &IrEnvelope<SchemaIrPayload>,
    ) -> (IrEnvelope<SchemaIrPayload>, IrEnvelope<SchemaIrPayload>) {
        let before = &self.stages.before;
        (
            staging::schema_shape(before, target, &self.index_ops[at..]),
            staging::schema_shape(before, target, &self.index_ops[at + 1..]),
        )
    }

    /// Whether the models change nothing any dialect has a step for.
    pub(super) fn is_empty(&self) -> bool {
        self.labels_added.is_empty() && self.placed.iter().all(|p| p.plan.is_empty())
    }

    /// The phase steps before the data steps that the migration has, in
    /// order: `labels` when the models add a label (on every dialect, also
    /// where it has nothing to run), `schema` when any dialect has a schema
    /// op.
    pub(super) fn early_phases(&self) -> Vec<Phase> {
        let mut phases = Vec::new();
        if !self.labels_added.is_empty() || self.placed.iter().any(|p| !p.labels.is_empty()) {
            phases.push(Phase::Labels);
        }
        if self.placed.iter().any(|p| !p.schema.is_empty()) {
            phases.push(Phase::Schema);
        }
        phases
    }

    /// Whether the migration has a contract: it backfills, or a drop waits
    /// for its data steps.
    pub(super) fn contracts(&self) -> bool {
        self.backfills || self.placed.iter().any(|p| !p.held.is_empty())
    }
}

/// `planned`'s ops placed by their phase in a migration that has
/// (`data_steps`) or lacks a data step.
fn place(dialect: Dialect, planned: Plan, data_steps: bool) -> Result<Placed, GenerateError> {
    let (mut labels, mut schema, mut held, mut relabel) =
        (Vec::new(), Vec::new(), Vec::new(), Vec::new());
    for op in &planned.operations {
        match (phase_of(op, data_steps)?, &op.op) {
            (Phase::Labels, _) => labels.push(op.op.clone()),
            // A demanded column is added nullable by the expand; its
            // backfill fills it and the contract makes it `NOT NULL`.
            (Phase::Schema, _) | (Phase::Backfill, MigrationOp::AddColumn { .. }) => {
                schema.push(op.op.clone());
            }
            (Phase::Backfill, MigrationOp::RemoveEnumLabel { .. }) => relabel.push(op.op.clone()),
            // A removed label's change of a column that held it.
            (Phase::Contract, _) if op.verdict.goes_with == Some(Rider::RemovedLabel) => {
                relabel.push(op.op.clone());
            }
            (Phase::Contract, _) => held.push(op.op.clone()),
            // A column made `NOT NULL` is the contract's (its demand), and an
            // index change its own index step ([`index_ops`]).
            _ => {}
        }
    }
    Ok(Placed {
        dialect,
        plan: planned,
        labels,
        schema,
        held,
        relabel,
    })
}

/// Whether the expand already changes `op`'s artifact (a label removal's
/// rider) from `before` to `expanded`: the column's storage or its check's
/// labels differ, because the same migration adds a label to the type.
fn widens(
    op: &MigrationOp,
    before: &IrEnvelope<SchemaIrPayload>,
    expanded: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
) -> bool {
    let column = |ir, table: &str, column: &str| {
        find_model(ir, table).and_then(|model| model.columns.iter().find(|c| c.name == column))
    };
    let check = |ir, table: &str, name: &str| {
        find_model(ir, table).and_then(|model| model.checks.iter().find(|c| c.name == name))
    };
    match op {
        MigrationOp::AlterColumnType {
            table,
            column: name,
        } => match (column(before, table, name), column(expanded, table, name)) {
            (Some(old), Some(new)) => schema_columns_storage_drift(old, new, dialect),
            _ => false,
        },
        MigrationOp::RebuildCheck { table, name } => {
            match (check(before, table, name), check(expanded, table, name)) {
                (Some(old), Some(new)) => old.values != new.values,
                _ => false,
            }
        }
        _ => false,
    }
}

/// The index steps' ops (ADR-0044, ADR-0051): every index op the plans put
/// in the index phase — an index added, dropped or redefined on a table the
/// parent holds — and an `AddIndex` for every index an added column of such
/// a table carries ([`column_riders`]), which a table that already exists
/// builds in its own step too. Laid out table by table in the target's
/// order, each table's drops first (as the parent declares them), then its
/// builds (as the target does).
fn index_ops(
    plans: &[Plan],
    before: &IrEnvelope<SchemaIrPayload>,
    target: &IrEnvelope<SchemaIrPayload>,
) -> Result<Vec<MigrationOp>, GenerateError> {
    let mut builds: BTreeSet<(String, String)> = BTreeSet::new();
    let mut drops: BTreeSet<(String, String)> = BTreeSet::new();
    for planned in plans.iter().flat_map(|plan| &plan.operations) {
        match &planned.op {
            MigrationOp::AddColumn { table, column } if find_model(before, table).is_some() => {
                if let Some(model) = find_model(target, table) {
                    let riders = column_riders(model, column);
                    for name in riders.unique.iter().chain(&riders.index) {
                        builds.insert((table.clone(), name.clone()));
                    }
                }
            }
            MigrationOp::AddIndex { table, name, .. }
            | MigrationOp::RedefineIndex { table, name }
                if phase_of(planned, false)? == Phase::Index =>
            {
                builds.insert((table.clone(), name.clone()));
            }
            MigrationOp::DropIndex { table, name } if phase_of(planned, false)? == Phase::Index => {
                drops.insert((table.clone(), name.clone()));
            }
            _ => {}
        }
    }
    let mut ops = Vec::new();
    for after in &target.payload.models {
        let Some(parent) = find_model(before, &after.table_name) else {
            continue;
        };
        let table = &after.table_name;
        let key = |name: &str| (table.clone(), name.to_string());
        let old = standalone_indexes(parent);
        for (name, ..) in &old {
            if drops.contains(&key(name)) {
                ops.push(MigrationOp::DropIndex {
                    table: table.clone(),
                    name: name.clone(),
                });
            }
        }
        for (name, columns, unique) in standalone_indexes(after) {
            if !builds.contains(&key(&name)) {
                continue;
            }
            // The parent's index under the name is dropped by the build.
            ops.push(if old.iter().any(|(declared, ..)| declared == &name) {
                MigrationOp::RedefineIndex {
                    table: table.clone(),
                    name,
                }
            } else {
                MigrationOp::AddIndex {
                    table: table.clone(),
                    name,
                    columns,
                    unique,
                }
            });
        }
    }
    Ok(ops)
}
