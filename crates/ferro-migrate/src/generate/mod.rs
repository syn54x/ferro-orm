//! The migration generator (ADR-0023, ADR-0026, ADR-0027, ADR-0037): the
//! difference between the head schema snapshot and the declared modelset,
//! decided by the one planner and rendered once per target dialect into the
//! steps of a new migration — never read from a database.
//!
//! ```text
//! parent snapshot ──plan_from_ir──▶ up ops   ──render_plan──▶ 01_schema.up.<dialect>.sql
//! target modelset ──plan_from_ir──▶ down ops ──render_plan──▶ 01_schema.down.<dialect>.sql
//! ```
//!
//! The down is the same planner run backwards (target → parent), restricted
//! to what each step touches ([`downs::render_down`]), so a dropped model's
//! down recreates it from the parent snapshot exactly as a new model's up
//! creates it. Every statement comes from [`render_plan`]: the
//! generator decides which step an op lands in and which headers the file
//! carries, never a statement (AGENTS.md § I-1).
//!
//! It generates new and dropped models (their tables, the enum types they
//! introduce or retire, and everything a `CREATE TABLE` carries), the plain
//! `ALTER TABLE` edits of an existing table where the dialect has a native
//! statement, and on SQLite a table rebuild for every change `ALTER TABLE`
//! cannot express ([`rebuild`]), in the same step as its Postgres twin. On a
//! table that already exists ([`staging`]) every index change is its own
//! index step, after the others, and on Postgres every foreign key and check
//! is added `NOT VALID` and validated by a last `validate` step. A label added
//! to an enum type is a first `labels` step whose down reverses nothing, and a
//! label or type rename is a rename of the `schema` step ([`enums`]).
//! [`columns::assign`] decides each op's step; every other change is refused
//! naming the ticket that generates it.

pub mod columns;
pub mod downs;
pub mod enums;
pub mod rebuild;
pub mod renames;
pub mod row_security;
pub mod staging;

use crate::directory::{DirectoryError, Headers, MigrationsDir, StepDialect, StepKind};
use crate::plan::{HintError, renamed_snapshot};
use crate::snapshot::{Snapshot, SnapshotError};
use crate::{
    Dialect, EmissionError, LiveFacts, MigrationOp, MigrationPlan, PlanOptions, RenderedOp,
    plan_from_ir, render_plan,
};
use columns::{Needs, Phase, PlanContext, PlanDirection, Refusal, StepAssignment};
use ferro_ddl_lowering::extra_check_names_warning;
use ferro_schema_ir::{IrEnvelope, SchemaIrPayload};
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

/// One dialect's up and down file of a generated step, as written to disk.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize)]
pub struct Rendering {
    /// The up file's full text, headers included.
    pub up: String,
    /// The down file's full text, headers included.
    pub down: String,
    /// The up file's headers.
    pub headers: Headers,
    /// The down file's headers.
    pub down_headers: Headers,
}

/// One step of a generated migration.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize)]
pub struct GeneratedStep {
    /// `NN`, from 1.
    pub ordinal: u8,
    /// The name after `NN_`.
    pub name: String,
    /// What the step is.
    pub kind: StepKind,
    /// One rendering per target dialect.
    pub renderings: BTreeMap<StepDialect, Rendering>,
}

/// Everything a new migration directory holds, ready to write.
#[derive(Clone, Debug, PartialEq, serde::Serialize)]
pub struct GeneratedMigration {
    /// Its steps, in order.
    pub steps: Vec<GeneratedStep>,
    /// Its snapshot.
    #[serde(serialize_with = "crate::directory::serialize_snapshot")]
    pub snapshot: Snapshot,
    /// The snapshot's `ir.json` text, exactly as it is to be stored.
    pub snapshot_json: String,
    /// What changed, in words: the models added and dropped, the enum types
    /// introduced and retired.
    pub summary: String,
    /// Warnings rendering raised (a backend limitation a dialect skips, such as
    /// row security on SQLite), each once.
    pub warnings: Vec<String>,
}

/// Why `new` writes nothing.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum GenerateError {
    /// The planner decided an op this generator does not generate yet.
    NotGeneratedYet {
        /// The op kind (`AddColumn`, …).
        op: String,
        /// The table, or the enum type, it changes.
        subject: String,
        /// The ticket that generates it.
        ticket: u32,
    },
    /// The op needs values for existing rows (ticket #534).
    NeedsBackfill {
        /// The op kind (`AddColumn`, …).
        op: String,
        /// The table it changes.
        table: String,
        /// The column whose existing rows need a value.
        column: String,
    },
    /// The models change a table's primary key (ticket #536).
    PrimaryKeyChange {
        /// The table whose key changes.
        table: String,
    },
    /// The renderer has no statement for an op on a dialect, only a warning
    /// saying why (a cast the pass refuses): writing the file without it
    /// would be a silent omission.
    Unrenderable {
        /// The op kind.
        op: String,
        /// The table it changes.
        table: String,
        /// The dialect.
        dialect: StepDialect,
        /// The renderer's warning.
        warning: String,
    },
    /// The planner reported a change between the two snapshots that it turns
    /// into no op (an enum label removal, a drifting foreign key ferro does
    /// not own) — writing nothing for it would be a silent omission.
    Unplanned {
        /// The dialect the plan was for.
        dialect: StepDialect,
        /// The planner's report.
        warning: String,
    },
    /// No target dialect was given.
    NoDialects,
    /// A declared rename hint `new` refuses (ADR-0032): its old name is still
    /// declared, or two hints claim one old name.
    Hint(HintError),
    /// An op could not render.
    Render(String),
}

impl std::fmt::Display for GenerateError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            GenerateError::NotGeneratedYet {
                op,
                subject,
                ticket,
            } => write!(f, "not generated yet: {op} on {subject} (ticket #{ticket})"),
            GenerateError::NeedsBackfill { op, table, .. } => write!(
                f,
                "not generated yet: {op} on {table} needs a backfill (ticket #534)"
            ),
            GenerateError::PrimaryKeyChange { table } => write!(
                f,
                "not generated yet: a primary-key change on {table} (ticket #536): a table's \
                 primary key cannot change in place; declare a new model with the new key, \
                 copy the rows across, then drop the old model"
            ),
            GenerateError::Unrenderable {
                op,
                table,
                dialect,
                warning,
            } => write!(
                f,
                "not generated yet: {op} on {table} has no statement on {}: {warning}",
                dialect.suffix().unwrap_or("every dialect")
            ),
            GenerateError::Unplanned { dialect, warning } => write!(
                f,
                "not generated yet: the models change something the planner has no \
                 operation for on {}: {warning}",
                dialect.suffix().unwrap_or("every dialect")
            ),
            GenerateError::NoDialects => write!(
                f,
                "no target dialect: the database's config needs dialects = [...]"
            ),
            GenerateError::Hint(err) => write!(f, "rename hint refused: {err}"),
            GenerateError::Render(message) => f.write_str(message),
        }
    }
}

impl std::error::Error for GenerateError {}

impl From<EmissionError> for GenerateError {
    fn from(err: EmissionError) -> Self {
        GenerateError::Render(err.message)
    }
}

impl From<SnapshotError> for GenerateError {
    fn from(err: SnapshotError) -> Self {
        GenerateError::Render(format!("the generated snapshot {err}"))
    }
}

/// The op's kind as the plan JSON spells it (`AddTable`, …).
fn op_kind(op: &MigrationOp) -> String {
    serde_json::to_value(op)
        .ok()
        .and_then(|value| value.get("kind")?.as_str().map(str::to_string))
        .unwrap_or_else(|| format!("{op:?}"))
}

/// What `op` changes, for a refusal: its table, or the enum type.
fn op_subject(op: &MigrationOp) -> String {
    match op {
        MigrationOp::AddEnumLabel { type_name, .. }
        | MigrationOp::CreateEnumType { type_name, .. }
        | MigrationOp::DropEnumType { type_name }
        | MigrationOp::RenameEnumLabel { type_name, .. }
        | MigrationOp::RenameEnumType { new: type_name, .. } => type_name.clone(),
        other => other.table().unwrap_or_default().to_string(),
    }
}

/// The column an op changes, when it changes one.
fn op_column(op: &MigrationOp) -> String {
    match op {
        MigrationOp::AddColumn { column, .. }
        | MigrationOp::DropColumn { column, .. }
        | MigrationOp::AlterColumnType { column, .. }
        | MigrationOp::AlterColumnNullability { column, .. }
        | MigrationOp::AddForeignKey { column, .. }
        | MigrationOp::RebuildForeignKey { column, .. } => column.clone(),
        _ => String::new(),
    }
}

/// Which step `op` belongs in, in the file that turns `before` into `after`
/// on `dialect` ([`columns::assign`]), or the refusal naming the ticket that
/// generates it.
fn phase_of(
    op: &MigrationOp,
    before: &IrEnvelope<SchemaIrPayload>,
    after: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
    direction: PlanDirection,
) -> Result<Phase, GenerateError> {
    let ctx = PlanContext::of(op, before, after, dialect, direction);
    let StepAssignment { phase, needs } = columns::assign(op, &ctx);
    let table = op_subject(op);
    match needs {
        // A rebuild sits in the phase step its Postgres twin's change does
        // (ADR-0046).
        Needs::Native | Needs::Rebuild => Ok(phase),
        Needs::Backfill => Err(GenerateError::NeedsBackfill {
            op: op_kind(op),
            table,
            column: op_column(op),
        }),
        Needs::Refused(Refusal::Ticket(ticket)) => Err(GenerateError::NotGeneratedYet {
            op: op_kind(op),
            subject: table,
            ticket,
        }),
        Needs::Refused(Refusal::PrimaryKeyChange) => Err(GenerateError::PrimaryKeyChange { table }),
        Needs::Refused(Refusal::LiveOnly) => Err(GenerateError::Render(format!(
            "{} on {table} is planned only against a live database, never between two \
             schema snapshots",
            op_kind(op)
        ))),
    }
}

/// The ops of `plan` the file renders: every op but a SQLite check drop the
/// same file's `DROP COLUMN` carries ([`columns::carried_by_its_column_drop`]).
fn rendered_ops(
    plan: &MigrationPlan,
    before: &IrEnvelope<SchemaIrPayload>,
    after: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
    direction: PlanDirection,
) -> Vec<MigrationOp> {
    plan.operations
        .iter()
        .filter(|op| {
            let ctx = PlanContext::of(op, before, after, dialect, direction);
            !columns::carried_by_its_column_drop(op, &ctx)
        })
        .cloned()
        .collect()
}

/// Refuse an op the renderer answered with a warning and no statement (a
/// cast the pass refuses): the file would otherwise silently leave it out.
/// A new table's warnings (row security on SQLite) are reported, not refused:
/// the table itself is created.
fn refuse_unrendered(rendered: &[RenderedOp], dialect: Dialect) -> Result<(), GenerateError> {
    for op in rendered {
        if matches!(op.op, MigrationOp::AddTable { .. }) {
            continue;
        }
        if let Some(warning) = op.warnings.first() {
            return Err(GenerateError::Unrenderable {
                op: op_kind(&op.op),
                table: op_subject(&op.op),
                dialect: dialect.into(),
                warning: warning.clone(),
            });
        }
    }
    Ok(())
}

/// The modelset with no models, in `like`'s IR version: the parent of `0001`.
pub fn empty_modelset(like: &IrEnvelope<SchemaIrPayload>) -> IrEnvelope<SchemaIrPayload> {
    IrEnvelope {
        ir_kind: like.ir_kind.clone(),
        ir_version: like.ir_version,
        payload: SchemaIrPayload {
            dialect_agnostic: like.payload.dialect_agnostic,
            models: Vec::new(),
        },
    }
}

const DESTRUCTIVE: PlanOptions = PlanOptions { destructive: true };

/// Plan `old → new` on `dialect` as two declared snapshots: every drop is
/// planned (a dropped model is always rendered, marked destructive; review is
/// the gate), and row security `old` declares and `new` does not is torn
/// down ([`row_security::with_teardown`]).
fn plan(
    old: &IrEnvelope<SchemaIrPayload>,
    new: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
) -> MigrationPlan {
    let mut plan = plan_from_ir(old, new, dialect, &LiveFacts::declared(), DESTRUCTIVE);
    plan.operations = row_security::with_teardown(plan.operations, old, new, dialect);
    plan
}

/// Every warning `plan` raises that planning `standing → standing` does not
/// and that nothing answers: the reports this change caused, not the ones the
/// models always raise. A leftover CHECK's report is answered by the plan's
/// drop of it, a row-security teardown's by the teardown itself
/// ([`row_security::answered_warnings`]); `answered` names the rest (a down's
/// report of a label the up added, which the `labels` step's down already
/// says stays).
fn change_warnings(
    plan: &MigrationPlan,
    standing: &MigrationPlan,
    answered_elsewhere: &[String],
) -> Vec<String> {
    let mut dropped_checks: BTreeMap<&str, Vec<String>> = BTreeMap::new();
    for op in &plan.operations {
        if let MigrationOp::DropCheck { table, name } = op {
            dropped_checks.entry(table).or_default().push(name.clone());
        }
    }
    let answered: BTreeSet<String> = dropped_checks
        .iter()
        .filter_map(|(table, names)| extra_check_names_warning(table, names))
        .chain(row_security::answered_warnings(&plan.operations))
        .chain(answered_elsewhere.iter().cloned())
        .collect();
    let already: BTreeSet<&String> = standing
        .warnings
        .iter()
        .chain(&standing.always_warnings)
        .collect();
    plan.warnings
        .iter()
        .chain(&plan.always_warnings)
        .filter(|warning| !already.contains(warning) && !answered.contains(*warning))
        .cloned()
        .collect()
}

/// Refuse anything in `plan` (the file turning `before` into `after`) this
/// generator does not generate: an op with no phase, or a warning the change
/// caused that no op answers.
fn refuse_unsupported(
    plan: &MigrationPlan,
    standing: &MigrationPlan,
    before: &IrEnvelope<SchemaIrPayload>,
    after: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
    direction: PlanDirection,
    answered: &[String],
) -> Result<(), GenerateError> {
    for op in &plan.operations {
        phase_of(op, before, after, dialect, direction)?;
    }
    if let Some(warning) = change_warnings(plan, standing, answered).into_iter().next() {
        return Err(GenerateError::Unplanned {
            dialect: dialect.into(),
            warning,
        });
    }
    Ok(())
}

/// The text of one step file: its headers, then each statement terminated by
/// `;`, a blank line between them. A file with no statements is the one-line
/// `-- ferro: not-applicable`.
fn step_text(headers: &Headers, statements: &[String]) -> String {
    let mut out = headers.render();
    for statement in statements {
        if !out.is_empty() {
            out.push('\n');
        }
        out.push_str(statement);
        out.push_str(";\n");
    }
    out
}

/// Every warning rendering `ops` raises on `dialect` (a backend limitation a
/// dialect skips, such as row security on SQLite), each once, into
/// `warnings`; an op the renderer leaves out with a warning is refused. An op
/// on a table SQLite rebuilds is not rendered alone: its rebuild carries it.
fn render_warnings(
    ops: &[MigrationOp],
    old: &IrEnvelope<SchemaIrPayload>,
    new: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
    warnings: &mut Vec<String>,
) -> Result<(), GenerateError> {
    let rebuilt = rebuild::tables_to_rebuild(ops, old, new, dialect, PlanDirection::Up);
    let plan = MigrationPlan {
        operations: ops
            .iter()
            .filter(|op| !op.table().is_some_and(|table| rebuilt.contains(table)))
            .cloned()
            .collect(),
        ..MigrationPlan::default()
    };
    let rendered = render_plan(&plan, old, new, dialect)?;
    refuse_unrendered(&rendered, dialect)?;
    for warning in rendered.into_iter().flat_map(|rendered| rendered.warnings) {
        if !warnings.contains(&warning) {
            warnings.push(warning);
        }
    }
    Ok(())
}

/// The short name of a model (`Author` for `myapp.models.Author`).
fn short_model_name(model_name: &str) -> &str {
    model_name.rsplit('.').next().unwrap_or(model_name)
}

/// What the up ops change, in words, over every dialect.
fn summarize(
    ups: &[Vec<MigrationOp>],
    parent: &IrEnvelope<SchemaIrPayload>,
    target: &IrEnvelope<SchemaIrPayload>,
) -> String {
    let model_of = |ir: &IrEnvelope<SchemaIrPayload>, table: &str| {
        ir.payload
            .models
            .iter()
            .find(|model| model.table_name == table)
            .map(|model| short_model_name(&model.model_name).to_string())
            .unwrap_or_else(|| table.to_string())
    };
    let mut added = BTreeSet::new();
    let mut dropped = BTreeSet::new();
    let mut types_added = BTreeSet::new();
    let mut types_dropped = BTreeSet::new();
    let mut changed = BTreeSet::new();
    let mut types_renamed = BTreeSet::new();
    let mut labels_added = BTreeSet::new();
    let mut labels_renamed = BTreeSet::new();
    for op in ups.iter().flatten() {
        match op {
            MigrationOp::AddTable { table } => {
                added.insert(model_of(target, table));
            }
            MigrationOp::DropTable { table } => {
                dropped.insert(model_of(parent, table));
            }
            MigrationOp::CreateEnumType { type_name, .. } => {
                types_added.insert(type_name.clone());
            }
            MigrationOp::DropEnumType { type_name } => {
                types_dropped.insert(type_name.clone());
            }
            MigrationOp::RenameEnumType { old, new } => {
                types_renamed.insert(format!("{old} → {new}"));
            }
            MigrationOp::AddEnumLabel { type_name, label } => {
                labels_added.insert(format!("{type_name}.{label}"));
            }
            MigrationOp::RenameEnumLabel {
                type_name,
                old,
                new,
                ..
            } => {
                labels_renamed.insert(format!("{type_name}.{old} → {new}"));
            }
            other => {
                if let Some(table) = other.table() {
                    changed.insert(model_of(target, table));
                }
            }
        }
    }
    [
        ("new models", added),
        ("changed models", changed),
        ("dropped models", dropped),
        ("new enum types", types_added),
        ("dropped enum types", types_dropped),
        ("renamed enum types", types_renamed),
        ("new enum labels", labels_added),
        ("renamed enum labels", labels_renamed),
    ]
    .into_iter()
    .filter(|(_, names)| !names.is_empty())
    .map(|(label, names)| {
        format!(
            "{label}: {}",
            names.into_iter().collect::<Vec<_>>().join(", ")
        )
    })
    .collect::<Vec<_>>()
    .join("; ")
}

/// The migration that turns `parent` (the head snapshot; `None` before the
/// first migration, which is generated against the empty modelset) into
/// `target`, rendered for every dialect in `dialects`.
///
/// Returns `Ok(None)` when no dialect has anything to do: a difference
/// outside the DDL-bearing projection (a Python default, a back-reference, a
/// method) is not a schema change (ADR-0027).
///
/// # Errors
/// [`GenerateError::NotGeneratedYet`],
/// [`GenerateError::NeedsBackfill`] and [`GenerateError::PrimaryKeyChange`]
/// for a change this generator does not generate yet,
/// [`GenerateError::Unrenderable`] for an op the renderer leaves out with a
/// warning, [`GenerateError::Unplanned`] for a change the planner reports but
/// has no op for, [`GenerateError::Render`] when an op cannot render.
pub fn generate(
    parent: Option<&Snapshot>,
    target: &IrEnvelope<SchemaIrPayload>,
    dialects: &[Dialect],
) -> Result<Option<GeneratedMigration>, GenerateError> {
    if dialects.is_empty() {
        return Err(GenerateError::NoDialects);
    }
    let empty = empty_modelset(target);
    let parent_ir = parent.map(|snapshot| &snapshot.ir).unwrap_or(&empty);
    // Declared renames (ADR-0032): the planner puts them first; every other
    // op reads its table from `before`, the parent as the renames leave it.
    // A refused hint stops `new` here, before anything is written.
    let hints = renames::live(parent_ir, target)?;
    let renamed_parent = renamed_snapshot(parent_ir, &hints);
    let before = &renamed_parent;

    let mut changes = Vec::new();
    let mut suggestions = Vec::new();
    for &dialect in dialects {
        let change = plan(parent_ir, target, dialect);
        refuse_unsupported(
            &change,
            &plan(target, target, dialect),
            before,
            target,
            dialect,
            PlanDirection::Up,
            &[],
        )?;
        refuse_unsupported(
            &plan(target, before, dialect),
            &plan(before, before, dialect),
            target,
            before,
            dialect,
            PlanDirection::Down,
            &enums::answered_by_labels_step(&change.operations),
        )?;
        for line in renames::suggestions(&change.operations, before, target) {
            if !suggestions.contains(&line) {
                suggestions.push(line);
            }
        }
        changes.push(change.operations);
    }

    // The index steps come after every other step but the validate step,
    // and turn `shape` into the target; every earlier step turns the parent
    // into `shape` (ADR-0044, ADR-0046).
    let index_ops = staging::index_ops(before, target);
    let shape = staging::schema_shape(before, target, &index_ops);
    let mut ups = Vec::new();
    let mut downs = Vec::new();
    for &dialect in dialects {
        let up = plan(parent_ir, &shape, dialect);
        ups.push(rendered_ops(
            &up,
            before,
            &shape,
            dialect,
            PlanDirection::Up,
        ));
        downs.push(plan(&shape, before, dialect));
    }
    if ups.iter().all(Vec::is_empty)
        && downs.iter().all(MigrationPlan::is_empty)
        && index_ops.is_empty()
    {
        return Ok(None);
    }

    let mut phases = BTreeSet::new();
    for (&dialect, (up, down)) in dialects.iter().zip(ups.iter().zip(&downs)) {
        for op in up {
            phases.insert(phase_of(op, before, &shape, dialect, PlanDirection::Up)?);
        }
        for op in &down.operations {
            phases.insert(phase_of(op, &shape, before, dialect, PlanDirection::Down)?);
        }
    }
    if phases.contains(&Phase::Index) {
        return Err(GenerateError::Render(
            "an index change on an existing table reached a phase step; it is its own index \
             step"
                .to_string(),
        ));
    }
    let mut warnings = Vec::new();
    let mut staged = Vec::new();
    for (&dialect, up) in dialects.iter().zip(&ups) {
        render_warnings(up, before, &shape, dialect, &mut warnings)?;
        for constraint in staging::staged_constraints(up, before, &shape, dialect)? {
            if !staged.contains(&constraint) {
                staged.push(constraint);
            }
        }
    }

    let mut steps = Vec::new();
    let push_phase = |phase: Phase, steps: &mut Vec<GeneratedStep>| {
        let mut renderings = BTreeMap::new();
        for (&dialect, up) in dialects.iter().zip(&ups) {
            let mut step_ops = Vec::new();
            for op in up {
                if phase_of(op, before, &shape, dialect, PlanDirection::Up)? == phase {
                    step_ops.push(op.clone());
                }
            }
            let rendering = if phase == Phase::Labels {
                enums::render_labels_step(&step_ops, before, &shape, dialect)?
            } else {
                downs::render_down(&step_ops, parent_ir, &shape, dialect, phase, &hints)?
            };
            renderings.insert(StepDialect::from(dialect), rendering);
        }
        steps.push(GeneratedStep {
            ordinal: 0,
            name: phase.step_name().to_string(),
            kind: StepKind::Ddl,
            renderings,
        });
        Ok::<(), GenerateError>(())
    };
    for &phase in phases.iter().filter(|&&phase| phase < Phase::Index) {
        push_phase(phase, &mut steps)?;
    }
    steps.extend(index_ops.iter().map(|op| staging::index_step(op, dialects)));
    for &phase in phases.iter().filter(|&&phase| phase > Phase::Index) {
        push_phase(phase, &mut steps)?;
    }
    // A step every configured dialect would render not-applicable is not
    // generated: only Postgres stages a constraint (ADR-0043).
    if !staged.is_empty() {
        steps.push(staging::validate_step(&staged, dialects));
    }
    for (ordinal, step) in (1u8..).zip(&mut steps) {
        step.ordinal = ordinal;
    }

    let bytes = Snapshot::store(target, parent.map(|snapshot| snapshot.checksum))?;
    let snapshot = Snapshot::load(&bytes)?;
    let snapshot_json = String::from_utf8(bytes).map_err(|err| {
        GenerateError::Render(format!("the generated snapshot is not UTF-8: {err}"))
    })?;
    Ok(Some(GeneratedMigration {
        steps,
        snapshot,
        snapshot_json,
        summary: std::iter::once(summarize(&changes, parent_ir, target))
            .chain(suggestions)
            .filter(|line| !line.is_empty())
            .collect::<Vec<_>>()
            .join("\n"),
        warnings,
    }))
}

/// One thing `ferro migrate check` found wrong.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize)]
pub struct Problem {
    /// A stable identifier: a [`DirectoryError::kind`], or `ungenerated`.
    pub kind: String,
    /// What is wrong and how to fix it.
    pub message: String,
}

/// What `ferro migrate check` reports.
#[derive(Clone, Debug, Default, PartialEq, Eq, serde::Serialize)]
pub struct CheckReport {
    /// `true` when there is no problem.
    pub ok: bool,
    /// The head migration's directory name, when there is one.
    pub head: Option<String>,
    /// Every problem found.
    pub problems: Vec<Problem>,
}

fn problem(err: &DirectoryError) -> Problem {
    Problem {
        kind: err.kind().to_string(),
        message: err.to_string(),
    }
}

/// The offline check (`ferro migrate check`): read the migrations directory
/// at `path` and report every problem — a malformed directory or broken
/// chain, a DDL step missing a rendering a target dialect needs, and a model
/// change no migration records yet. Reads files only.
pub fn check_migrations(
    path: &Path,
    target: &IrEnvelope<SchemaIrPayload>,
    dialects: &[Dialect],
) -> CheckReport {
    let mut report = CheckReport::default();
    match MigrationsDir::read(path) {
        Err(err) => report.problems.push(problem(&err)),
        Ok(dir) => {
            report.head = dir.head().map(|head| head.dir_name());
            report
                .problems
                .extend(dir.missing_renderings(dialects).iter().map(problem));
            let since = match &report.head {
                Some(head) => format!("the models changed since {head}"),
                None => "there is no migration yet".to_string(),
            };
            let head_snapshot = dir.head().map(|head| &head.snapshot);
            match generate(head_snapshot, target, dialects) {
                Ok(None) => {}
                Ok(Some(migration)) => report.problems.push(Problem {
                    kind: "ungenerated".to_string(),
                    message: format!(
                        "{since} and no migration records it ({}); run `ferro migrate new \
                         <name>` and commit the migration it writes",
                        migration.summary
                    ),
                }),
                Err(err) => report.problems.push(Problem {
                    kind: "ungenerated".to_string(),
                    message: format!(
                        "{since} and no migration records it, and `ferro migrate new` cannot \
                         generate it: {err}"
                    ),
                }),
            }
        }
    }
    report.ok = report.problems.is_empty();
    report
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::render_create_table;
    use ferro_schema_ir::{
        RowPolicyCommand, RowPolicyExpr, SchemaColumn, SchemaForeignKey, SchemaIndex, SchemaModel,
        SchemaRowPolicy, SchemaRowSecurity,
    };

    const BOTH: [Dialect; 2] = [Dialect::Postgres, Dialect::Sqlite];

    pub(super) fn ir(models: Vec<SchemaModel>) -> IrEnvelope<SchemaIrPayload> {
        IrEnvelope {
            ir_kind: "schema".into(),
            ir_version: 1,
            payload: SchemaIrPayload {
                dialect_agnostic: true,
                models,
            },
        }
    }

    pub(super) fn column(name: &str, logical_type: &str) -> SchemaColumn {
        SchemaColumn {
            renamed_from: None,
            name: name.into(),
            logical_type: logical_type.into(),
            db_type: None,
            db_type_explicit: None,
            nullable: false,
            primary_key: false,
            autoincrement: false,
            unique: false,
            index: false,
            default: None,
            format: None,
            enum_values: None,
            enum_type_name: None,
            postgres_native_enum: false,
            enum_renamed_labels: Default::default(),
        }
    }

    pub(super) fn pk() -> SchemaColumn {
        SchemaColumn {
            primary_key: true,
            autoincrement: true,
            ..column("id", "integer")
        }
    }

    fn status(labels: &[&str]) -> SchemaColumn {
        SchemaColumn {
            enum_values: Some(labels.iter().map(|l| serde_json::json!(l)).collect()),
            enum_type_name: Some("status".into()),
            ..column("status", "string")
        }
    }

    pub(super) fn model(name: &str, columns: Vec<SchemaColumn>) -> SchemaModel {
        SchemaModel {
            renamed_from: None,
            model_name: format!("myapp.models.{name}"),
            table_name: name.to_lowercase(),
            columns,
            foreign_keys: Vec::new(),
            indexes: Vec::new(),
            uniques: Vec::new(),
            checks: Vec::new(),
            table_checks: Vec::new(),
            row_security: None,
        }
    }

    pub(super) fn author() -> SchemaModel {
        model(
            "Author",
            vec![pk(), column("name", "string"), status(&["draft", "live"])],
        )
    }

    pub(super) fn post() -> SchemaModel {
        SchemaModel {
            foreign_keys: vec![SchemaForeignKey {
                renamed_from: None,
                column: "author_id".into(),
                to_table: "author".into(),
                to_column: "id".into(),
                on_delete: Some("CASCADE".into()),
                name: Some("fk_post_author_id_author".into()),
            }],
            indexes: vec![SchemaIndex {
                name: "idx_post_title".into(),
                columns: vec!["title".into()],
                unique: false,
            }],
            ..model(
                "Post",
                vec![
                    pk(),
                    column("author_id", "integer"),
                    SchemaColumn {
                        index: true,
                        ..column("title", "string")
                    },
                ],
            )
        }
    }

    fn snapshot_of(ir: &IrEnvelope<SchemaIrPayload>, parent: Option<&Snapshot>) -> Snapshot {
        Snapshot::load(&Snapshot::store(ir, parent.map(|p| p.checksum)).expect("store"))
            .expect("load")
    }

    /// Every statement the create pass executes for `model`, in its order.
    pub(super) fn create_pass(model: &SchemaModel, dialect: Dialect) -> Vec<String> {
        let emission = render_create_table(model, dialect).expect("create");
        let mut out = emission.pre_create_sqls;
        out.push(emission.create_sql);
        out.extend(emission.post_create_sqls);
        out
    }

    pub(super) fn file(statements: &[String], headers: &str) -> String {
        let mut out = headers.to_string();
        for statement in statements {
            if !out.is_empty() {
                out.push('\n');
            }
            out.push_str(statement);
            out.push_str(";\n");
        }
        out
    }

    fn rendering(migration: &GeneratedMigration, dialect: StepDialect) -> &Rendering {
        assert_eq!(migration.steps.len(), 1);
        assert_eq!(migration.steps[0].name, "schema");
        assert_eq!(migration.steps[0].ordinal, 1);
        &migration.steps[0].renderings[&dialect]
    }

    #[test]
    fn the_first_migration_creates_the_model_exactly_as_the_create_pass_does() {
        let target = ir(vec![author()]);
        let migration = generate(None, &target, &BOTH)
            .expect("ok")
            .expect("a change");
        assert_eq!(
            migration.steps[0]
                .renderings
                .keys()
                .copied()
                .collect::<Vec<_>>(),
            [StepDialect::Postgres, StepDialect::Sqlite]
        );
        for dialect in BOTH {
            let r = rendering(&migration, dialect.into());
            assert_eq!(
                r.up,
                file(&create_pass(&author(), dialect), ""),
                "{dialect:?}"
            );
            assert_eq!(r.headers, Headers::default());
            assert_eq!(
                r.down_headers,
                Headers::default(),
                "a down is never destructive"
            );
        }
        let pg = rendering(&migration, StepDialect::Postgres);
        assert_eq!(pg.down, "DROP TABLE \"author\";\n\nDROP TYPE \"status\";\n");
        assert_eq!(
            rendering(&migration, StepDialect::Sqlite).down,
            "DROP TABLE \"author\";\n"
        );
        assert_eq!(migration.snapshot.parent_checksum, None);
        assert_eq!(migration.snapshot.ir, target);
        assert_eq!(
            migration.snapshot_json.as_bytes(),
            Snapshot::store(&target, None).expect("store").as_slice()
        );
        assert_eq!(
            migration.summary,
            "new models: Author; new enum types: status"
        );
        assert!(migration.warnings.is_empty());
    }

    #[test]
    fn a_child_model_is_chained_to_its_parent_snapshot_and_dropped_alone() {
        let first = snapshot_of(&ir(vec![author()]), None);
        let target = ir(vec![author(), post()]);
        let migration = generate(Some(&first), &target, &[Dialect::Postgres])
            .expect("ok")
            .expect("a change");
        assert_eq!(migration.snapshot.parent_checksum, Some(first.checksum));
        assert_eq!(
            migration.steps[0]
                .renderings
                .keys()
                .copied()
                .collect::<Vec<_>>(),
            [StepDialect::Postgres],
            "only the configured dialects are rendered"
        );
        let pg = rendering(&migration, StepDialect::Postgres);
        assert_eq!(pg.up, file(&create_pass(&post(), Dialect::Postgres), ""));
        assert_eq!(pg.down, "DROP TABLE \"post\";\n");
        assert_eq!(migration.summary, "new models: Post");
    }

    #[test]
    fn dropping_models_drops_children_first_and_the_down_recreates_them() {
        let parent = snapshot_of(&ir(vec![author(), post()]), None);
        let target = ir(vec![model("Tag", vec![pk()])]);
        let migration = generate(Some(&parent), &target, &BOTH)
            .expect("ok")
            .expect("a change");
        let pg = rendering(&migration, StepDialect::Postgres);
        assert!(pg.headers.destructive);
        let mut up = create_pass(&model("Tag", vec![pk()]), Dialect::Postgres);
        up.extend([
            "DROP TABLE \"post\"".to_string(),
            "DROP TABLE \"author\"".to_string(),
            "DROP TYPE \"status\"".to_string(),
        ]);
        assert_eq!(pg.up, file(&up, "-- ferro: destructive\n"));
        // The down recreates the parent snapshot's tables, parents first, and
        // drops the table the up created: data-dependent, never destructive.
        let mut down = create_pass(&author(), Dialect::Postgres);
        down.extend(create_pass(&post(), Dialect::Postgres));
        down.push("DROP TABLE \"tag\"".to_string());
        assert_eq!(pg.down, file(&down, "-- ferro: data-dependent\n"));
        assert_eq!(
            migration.summary,
            "new models: Tag; dropped models: Author, Post; dropped enum types: status"
        );
    }

    #[test]
    fn an_edit_outside_the_ddl_bearing_projection_is_no_schema_change() {
        let parent = snapshot_of(&ir(vec![author()]), None);
        let mut edited = author();
        edited.columns[1].default = Some(serde_json::json!("anonymous"));
        edited.model_name = "myapp.renamed_module.Author".into();
        assert_eq!(generate(Some(&parent), &ir(vec![edited]), &BOTH), Ok(None));
    }

    /// What the reconciliation pass executes to turn `before` into `after`
    /// on `dialect`, statement by statement.
    fn pass(
        before: &IrEnvelope<SchemaIrPayload>,
        after: &IrEnvelope<SchemaIrPayload>,
        dialect: Dialect,
    ) -> Vec<String> {
        let pass = plan_from_ir(before, after, dialect, &LiveFacts::declared(), DESTRUCTIVE);
        render_plan(&pass, before, after, dialect)
            .expect("render")
            .into_iter()
            .flat_map(|rendered| rendered.statements)
            .collect()
    }

    fn with_columns(extra: Vec<SchemaColumn>) -> SchemaModel {
        let mut model = author();
        model.columns.extend(extra);
        model
    }

    fn optional(name: &str, logical_type: &str) -> SchemaColumn {
        SchemaColumn {
            nullable: true,
            ..column(name, logical_type)
        }
    }

    /// The one-step migration from `before` to `after` on `dialects`.
    fn edit(
        before: Vec<SchemaModel>,
        after: Vec<SchemaModel>,
        dialects: &[Dialect],
    ) -> GeneratedMigration {
        let parent = snapshot_of(&ir(before), None);
        generate(Some(&parent), &ir(after), dialects)
            .expect("ok")
            .expect("a change")
    }

    fn refusal(before: Vec<SchemaModel>, after: Vec<SchemaModel>, dialects: &[Dialect]) -> String {
        let parent = snapshot_of(&ir(before), None);
        generate(Some(&parent), &ir(after), dialects)
            .expect_err("refused")
            .to_string()
    }

    #[test]
    fn an_optional_column_is_the_passs_add_column_and_its_down_drops_it() {
        let after = with_columns(vec![optional("bio", "string")]);
        let migration = edit(vec![author()], vec![after.clone()], &BOTH);
        for dialect in BOTH {
            let r = rendering(&migration, dialect.into());
            let up = pass(&ir(vec![author()]), &ir(vec![after.clone()]), dialect);
            assert_eq!(up, ["ALTER TABLE \"author\" ADD COLUMN \"bio\" varchar"]);
            assert_eq!(r.up, file(&up, ""), "{dialect:?}");
            assert_eq!(r.headers, Headers::default());
            assert_eq!(r.down, "ALTER TABLE \"author\" DROP COLUMN \"bio\";\n");
            assert_eq!(
                r.down_headers,
                Headers::default(),
                "a down is never destructive"
            );
        }
    }

    #[test]
    fn a_required_column_with_a_literal_default_backfills_it_as_the_pass_does() {
        let tier = SchemaColumn {
            default: Some(serde_json::json!("free")),
            ..column("tier", "string")
        };
        let after = with_columns(vec![tier]);
        let migration = edit(vec![author()], vec![after.clone()], &BOTH);
        for dialect in BOTH {
            let r = rendering(&migration, dialect.into());
            let up = pass(&ir(vec![author()]), &ir(vec![after.clone()]), dialect);
            assert!(up[0].contains("NOT NULL DEFAULT 'free'"), "{up:?}");
            assert_eq!(r.up, file(&up, ""), "{dialect:?}");
            assert_eq!(r.down, "ALTER TABLE \"author\" DROP COLUMN \"tier\";\n");
        }
        assert_eq!(
            rendering(&migration, StepDialect::Postgres).up,
            "ALTER TABLE \"author\" ADD COLUMN \"tier\" varchar NOT NULL DEFAULT 'free';\n\n\
             ALTER TABLE \"author\" ALTER COLUMN \"tier\" DROP DEFAULT;\n"
        );
    }

    #[test]
    fn dropping_an_optional_column_is_destructive_and_its_down_adds_it_back() {
        let before = with_columns(vec![optional("bio", "string")]);
        let migration = edit(vec![before], vec![author()], &BOTH);
        for dialect in BOTH {
            let r = rendering(&migration, dialect.into());
            assert_eq!(
                r.up,
                "-- ferro: destructive\n\nALTER TABLE \"author\" DROP COLUMN \"bio\";\n"
            );
            assert_eq!(
                r.down,
                "ALTER TABLE \"author\" ADD COLUMN \"bio\" varchar;\n"
            );
            assert_eq!(r.down_headers, Headers::default());
        }
    }

    #[test]
    fn a_dropped_not_null_column_comes_back_not_null_marked_data_dependent() {
        let before = with_columns(vec![column("bio", "string")]);
        let migration = edit(vec![before.clone()], vec![author()], &[Dialect::Postgres]);
        let pg = rendering(&migration, StepDialect::Postgres);
        assert!(pg.headers.destructive);
        assert_eq!(
            pg.down,
            "-- ferro: data-dependent\n\n\
             ALTER TABLE \"author\" ADD COLUMN \"bio\" varchar;\n\n\
             ALTER TABLE \"author\" ALTER COLUMN \"bio\" SET NOT NULL;\n"
        );
        assert!(pg.down_headers.data_dependent && !pg.down_headers.destructive);
        // SQLite has no SET NOT NULL and refuses a NOT NULL ADD COLUMN with
        // no default: the down is a rebuild, its copy giving the column no
        // value, so a populated table fails it (ADR-0033).
        let migration = edit(vec![before.clone()], vec![author()], &BOTH);
        let sqlite = rendering(&migration, StepDialect::Sqlite);
        assert_eq!(
            sqlite.up,
            "-- ferro: destructive\n\nALTER TABLE \"author\" DROP COLUMN \"bio\";\n"
        );
        let mut down = vec![
            created_as_new(&before),
            "INSERT INTO \"_ferro_new_author\" (\"id\", \"name\", \"status\") \
             SELECT \"id\", \"name\", \"status\" FROM \"author\""
                .to_string(),
            "DROP TABLE \"author\"".to_string(),
            "ALTER TABLE \"_ferro_new_author\" RENAME TO \"author\"".to_string(),
        ];
        down.extend(create_pass_indexes(&before));
        assert_eq!(
            sqlite.down,
            file(
                &down,
                "-- ferro: foreign-keys-off\n-- ferro: data-dependent\n"
            )
        );
    }

    #[test]
    fn a_dropped_columns_index_and_check_go_with_it() {
        let mut before = with_columns(vec![SchemaColumn {
            index: true,
            ..optional("bio", "string")
        }]);
        before.indexes.push(ferro_schema_ir::SchemaIndex {
            name: "idx_author_bio".into(),
            columns: vec!["bio".into()],
            unique: false,
        });
        before.checks.push(ferro_schema_ir::SchemaCheck {
            name: "ck_author_bio".into(),
            column: "bio".into(),
            values: vec!["'a'".into()],
        });
        let migration = edit(vec![before.clone()], vec![author()], &BOTH);
        let sqlite = rendering(&migration, StepDialect::Sqlite);
        assert_eq!(
            sqlite.up,
            "-- ferro: destructive\n\nDROP INDEX IF EXISTS \"idx_author_bio\";\n\n\
             ALTER TABLE \"author\" DROP COLUMN \"bio\";\n"
        );
        assert!(migration.warnings.is_empty(), "{:?}", migration.warnings);
        let pg = rendering(&migration, StepDialect::Postgres);
        assert_eq!(
            pg.up,
            file(
                &pass(&ir(vec![before]), &ir(vec![author()]), Dialect::Postgres),
                "-- ferro: destructive\n"
            )
        );
        assert!(
            pg.down
                .contains("CREATE INDEX IF NOT EXISTS \"idx_author_bio\"")
        );
    }

    #[test]
    fn a_postgres_type_change_casts_both_ways_marked_data_dependent() {
        let before = with_columns(vec![column("age", "integer")]);
        let after = with_columns(vec![column("age", "string")]);
        let migration = edit(vec![before], vec![after], &[Dialect::Postgres]);
        let pg = rendering(&migration, StepDialect::Postgres);
        assert_eq!(
            pg.up,
            "-- ferro: data-dependent\n\n\
             ALTER TABLE \"author\" ALTER COLUMN \"age\" TYPE varchar USING \"age\"::varchar;\n"
        );
        assert_eq!(
            pg.down,
            "-- ferro: data-dependent\n\n\
             ALTER TABLE \"author\" ALTER COLUMN \"age\" TYPE integer USING \"age\"::integer;\n"
        );
    }

    #[test]
    fn relaxing_not_null_on_postgres_and_its_down_sets_it_again() {
        let before = with_columns(vec![column("bio", "string")]);
        let after = with_columns(vec![optional("bio", "string")]);
        let migration = edit(vec![before], vec![after], &[Dialect::Postgres]);
        let pg = rendering(&migration, StepDialect::Postgres);
        assert_eq!(
            pg.up,
            "ALTER TABLE \"author\" ALTER COLUMN \"bio\" DROP NOT NULL;\n"
        );
        assert_eq!(
            pg.down,
            "-- ferro: data-dependent\n\n\
             ALTER TABLE \"author\" ALTER COLUMN \"bio\" SET NOT NULL;\n"
        );
    }

    /// The step names of `migration`, `NN_name`, in order.
    fn step_names(migration: &GeneratedMigration) -> Vec<String> {
        migration
            .steps
            .iter()
            .map(|step| format!("{:02}_{}", step.ordinal, step.name))
            .collect()
    }

    /// The rendering of the step named `NN_name` on `dialect`.
    fn step<'a>(migration: &'a GeneratedMigration, name: &str, dialect: Dialect) -> &'a Rendering {
        let step = migration
            .steps
            .iter()
            .find(|step| format!("{:02}_{}", step.ordinal, step.name) == name)
            .unwrap_or_else(|| panic!("no step {name} in {:?}", step_names(migration)));
        &step.renderings[&StepDialect::from(dialect)]
    }

    fn unique_name(model: &SchemaModel) -> SchemaModel {
        let mut after = model.clone();
        after.columns[1].unique = true;
        after.uniques.push(ferro_schema_ir::SchemaUnique {
            name: "uq_author_name".into(),
            columns: vec!["name".into()],
        });
        after
    }

    const NOT_APPLICABLE: &str = "-- ferro: not-applicable\n";

    #[test]
    fn a_unique_on_an_existing_table_is_its_own_index_step_on_every_dialect() {
        let after = unique_name(&author());
        let migration = edit(vec![author()], vec![after.clone()], &BOTH);
        assert_eq!(step_names(&migration), ["01_uq_author_name"]);
        assert_eq!(migration.summary, "changed models: Author");
        // Postgres: built concurrently outside a transaction, exact from the
        // top; a duplicate fails it, so it is the data-dependent step.
        let pg = step(&migration, "01_uq_author_name", Dialect::Postgres);
        assert_eq!(
            pg.up,
            "-- ferro: no-transaction\n-- ferro: data-dependent\n\n\
             DROP INDEX CONCURRENTLY IF EXISTS \"uq_author_name\";\n\n\
             CREATE UNIQUE INDEX CONCURRENTLY \"uq_author_name\" ON \"author\" (\"name\");\n"
        );
        assert!(pg.headers.no_transaction && pg.headers.data_dependent);
        assert_eq!(
            pg.down,
            "-- ferro: no-transaction\n\n\
             DROP INDEX CONCURRENTLY IF EXISTS \"uq_author_name\";\n"
        );
        // SQLite: the plain statement, in a transaction, under the same step.
        let sqlite = step(&migration, "01_uq_author_name", Dialect::Sqlite);
        assert_eq!(
            sqlite.up,
            "-- ferro: data-dependent\n\n\
             CREATE UNIQUE INDEX IF NOT EXISTS \"uq_author_name\" ON \"author\" (\"name\");\n"
        );
        assert_eq!(sqlite.down, "DROP INDEX IF EXISTS \"uq_author_name\";\n");
        // A SQLite-only project gets the same step.
        let migration = edit(vec![author()], vec![after], &[Dialect::Sqlite]);
        assert_eq!(step_names(&migration), ["01_uq_author_name"]);
    }

    #[test]
    fn dropping_an_index_on_an_existing_table_is_the_reverse_index_step() {
        let before = unique_name(&author());
        let migration = edit(vec![before], vec![author()], &BOTH);
        assert_eq!(step_names(&migration), ["01_uq_author_name"]);
        let pg = step(&migration, "01_uq_author_name", Dialect::Postgres);
        assert_eq!(
            pg.up,
            "-- ferro: no-transaction\n\n\
             DROP INDEX CONCURRENTLY IF EXISTS \"uq_author_name\";\n"
        );
        assert!(!pg.headers.destructive, "an index holds no data");
        assert_eq!(
            pg.down,
            "-- ferro: no-transaction\n-- ferro: data-dependent\n\n\
             DROP INDEX CONCURRENTLY IF EXISTS \"uq_author_name\";\n\n\
             CREATE UNIQUE INDEX CONCURRENTLY \"uq_author_name\" ON \"author\" (\"name\");\n"
        );
        let sqlite = step(&migration, "01_uq_author_name", Dialect::Sqlite);
        assert_eq!(sqlite.up, "DROP INDEX IF EXISTS \"uq_author_name\";\n");
        assert_eq!(
            sqlite.down,
            "-- ferro: data-dependent\n\n\
             CREATE UNIQUE INDEX IF NOT EXISTS \"uq_author_name\" ON \"author\" (\"name\");\n"
        );
    }

    #[test]
    fn a_redefined_index_is_built_over_the_old_one_and_its_down_builds_the_old_one_back() {
        let index = |columns: &[&str]| ferro_schema_ir::SchemaIndex {
            name: "idx_author_lookup".into(),
            columns: columns.iter().map(|c| c.to_string()).collect(),
            unique: false,
        };
        let mut before = author();
        before.indexes.push(index(&["name"]));
        let mut after = author();
        after.indexes.push(index(&["name", "status"]));
        let migration = edit(vec![before], vec![after], &[Dialect::Postgres]);
        assert_eq!(step_names(&migration), ["01_idx_author_lookup"]);
        let pg = step(&migration, "01_idx_author_lookup", Dialect::Postgres);
        assert_eq!(
            pg.up,
            "-- ferro: no-transaction\n\n\
             DROP INDEX CONCURRENTLY IF EXISTS \"idx_author_lookup\";\n\n\
             CREATE INDEX CONCURRENTLY \"idx_author_lookup\" ON \"author\" (\"name\", \"status\");\n"
        );
        assert_eq!(
            pg.down,
            "-- ferro: no-transaction\n\n\
             DROP INDEX CONCURRENTLY IF EXISTS \"idx_author_lookup\";\n\n\
             CREATE INDEX CONCURRENTLY \"idx_author_lookup\" ON \"author\" (\"name\");\n"
        );
    }

    /// `author` with an optional `email`, unique when `unique`, and the table
    /// check `ck_author_email_nonempty` when `checked`.
    fn author_email(unique: bool, checked: bool) -> SchemaModel {
        let mut model = with_columns(vec![SchemaColumn {
            unique,
            ..optional("email", "string")
        }]);
        if unique {
            model.uniques.push(ferro_schema_ir::SchemaUnique {
                name: "uq_author_email".into(),
                columns: vec!["email".into()],
            });
        }
        if checked {
            model.table_checks.push(ferro_schema_ir::SchemaTableCheck {
                name: "ck_author_email_nonempty".into(),
                predicate: ferro_schema_ir::CheckExpr::IsNotNull {
                    column: "email".into(),
                },
            });
        }
        model
    }

    #[test]
    fn a_check_on_an_existing_postgres_table_is_added_not_valid_and_validated_in_its_own_step() {
        let (before, after) = (author_email(false, false), author_email(false, true));
        let migration = edit(vec![before.clone()], vec![after.clone()], &BOTH);
        assert_eq!(step_names(&migration), ["01_schema", "02_validate"]);
        let add = "ALTER TABLE \"author\" ADD CONSTRAINT \"ck_author_email_nonempty\" \
                   CHECK (\"email\" IS NOT NULL)";
        let pg = step(&migration, "01_schema", Dialect::Postgres);
        // NOT VALID takes its lock for no scan: nothing in it fails on rows.
        assert_eq!(pg.up, format!("{add} NOT VALID;\n"));
        assert_eq!(pg.headers, Headers::default());
        assert_eq!(
            pg.down,
            "ALTER TABLE \"author\" DROP CONSTRAINT \"ck_author_email_nonempty\";\n"
        );
        let validate = step(&migration, "02_validate", Dialect::Postgres);
        assert_eq!(
            validate.up,
            "-- ferro: data-dependent\n\n\
             ALTER TABLE \"author\" VALIDATE CONSTRAINT \"ck_author_email_nonempty\";\n"
        );
        // There is no un-validate: the down drops it and puts it back
        // NOT VALID, the state its parent step left (ADR-0043).
        assert_eq!(
            validate.down,
            format!(
                "ALTER TABLE \"author\" DROP CONSTRAINT \"ck_author_email_nonempty\";\n\n\
                 {add} NOT VALID;\n"
            )
        );
        // SQLite rebuilds in the schema step, and its validate step is the
        // one-line not-applicable file both ways.
        let sqlite = step(&migration, "01_schema", Dialect::Sqlite);
        assert_eq!(
            sqlite.up,
            file(&rebuild_of(&before, &after, &[]), &sqlite.headers.render())
        );
        let sqlite_validate = step(&migration, "02_validate", Dialect::Sqlite);
        assert_eq!(sqlite_validate.up, NOT_APPLICABLE);
        assert_eq!(sqlite_validate.down, NOT_APPLICABLE);
        // A SQLite-only project gets no validate step at all.
        let migration = edit(vec![before], vec![after], &[Dialect::Sqlite]);
        assert_eq!(step_names(&migration), ["01_schema"]);
    }

    #[test]
    fn the_ticket_transcript_schema_then_index_step_then_validate() {
        let (before, after) = (author_email(false, false), author_email(true, true));
        let migration = edit(vec![before], vec![after], &BOTH);
        assert_eq!(
            step_names(&migration),
            ["01_schema", "02_uq_author_email", "03_validate"]
        );
        assert_eq!(
            step(&migration, "02_uq_author_email", Dialect::Postgres).up,
            "-- ferro: no-transaction\n-- ferro: data-dependent\n\n\
             DROP INDEX CONCURRENTLY IF EXISTS \"uq_author_email\";\n\n\
             CREATE UNIQUE INDEX CONCURRENTLY \"uq_author_email\" ON \"author\" (\"email\");\n"
        );
        assert!(
            !step(&migration, "01_schema", Dialect::Postgres)
                .up
                .contains("uq_author_email")
        );
    }

    #[test]
    fn a_new_columns_index_unique_check_and_foreign_key_are_staged_on_an_existing_postgres_table() {
        let team = model("Team", vec![pk()]);
        let mut after = with_columns(vec![SchemaColumn {
            unique: true,
            ..optional("team_id", "integer")
        }]);
        after.uniques.push(ferro_schema_ir::SchemaUnique {
            name: "uq_author_team_id".into(),
            columns: vec!["team_id".into()],
        });
        after.foreign_keys.push(SchemaForeignKey {
            renamed_from: None,
            column: "team_id".into(),
            to_table: "team".into(),
            to_column: "id".into(),
            on_delete: Some("SET NULL".into()),
            name: Some("fk_author_team_id_team".into()),
        });
        let migration = edit(
            vec![team.clone(), author()],
            vec![team, after.clone()],
            &BOTH,
        );
        assert_eq!(
            step_names(&migration),
            ["01_schema", "02_uq_author_team_id", "03_validate"]
        );
        let fk = "ALTER TABLE \"author\" ADD CONSTRAINT \"fk_author_team_id_team\" FOREIGN KEY \
                  (\"team_id\") REFERENCES \"team\" (\"id\") ON DELETE SET NULL";
        let pg = step(&migration, "01_schema", Dialect::Postgres);
        assert_eq!(
            pg.up,
            format!("ALTER TABLE \"author\" ADD COLUMN \"team_id\" integer;\n\n{fk} NOT VALID;\n")
        );
        // The unique is the index step's; the schema step's down never
        // meets it.
        assert_eq!(pg.down, "ALTER TABLE \"author\" DROP COLUMN \"team_id\";\n");
        assert_eq!(
            step(&migration, "03_validate", Dialect::Postgres).up,
            "-- ferro: data-dependent\n\n\
             ALTER TABLE \"author\" VALIDATE CONSTRAINT \"fk_author_team_id_team\";\n"
        );
        // SQLite: the inline REFERENCES, the plain unique in its index step,
        // and no validation to do.
        let sqlite = step(&migration, "01_schema", Dialect::Sqlite);
        assert!(sqlite.up.contains("REFERENCES \"team\""), "{}", sqlite.up);
        assert!(!sqlite.up.contains("uq_author_team_id"), "{}", sqlite.up);
        assert_eq!(
            step(&migration, "02_uq_author_team_id", Dialect::Sqlite).up,
            "-- ferro: data-dependent\n\n\
             CREATE UNIQUE INDEX IF NOT EXISTS \"uq_author_team_id\" ON \"author\" (\"team_id\");\n"
        );
        assert_eq!(
            step(&migration, "03_validate", Dialect::Sqlite).up,
            NOT_APPLICABLE
        );
    }

    #[test]
    fn a_retargeted_foreign_key_on_postgres_drops_adds_not_valid_and_validates() {
        let team = model("Team", vec![pk()]);
        let club = model("Club", vec![pk()]);
        let fk = |to: &str| SchemaForeignKey {
            renamed_from: None,
            column: "team_id".into(),
            to_table: to.into(),
            to_column: "id".into(),
            on_delete: Some("CASCADE".into()),
            name: Some(format!("fk_author_team_id_{to}")),
        };
        let mut on_team = with_columns(vec![optional("team_id", "integer")]);
        on_team.foreign_keys.push(fk("team"));
        let mut on_club = on_team.clone();
        on_club.foreign_keys = vec![fk("club")];
        let migration = edit(
            vec![team.clone(), club.clone(), on_team],
            vec![team, club, on_club],
            &[Dialect::Postgres],
        );
        assert_eq!(step_names(&migration), ["01_schema", "02_validate"]);
        let pg = step(&migration, "01_schema", Dialect::Postgres);
        assert_eq!(
            pg.up,
            "ALTER TABLE \"author\" DROP CONSTRAINT \"fk_author_team_id_team\";\n\n\
             ALTER TABLE \"author\" ADD CONSTRAINT \"fk_author_team_id_club\" FOREIGN KEY \
             (\"team_id\") REFERENCES \"club\" (\"id\") ON DELETE CASCADE NOT VALID;\n"
        );
        assert_eq!(
            step(&migration, "02_validate", Dialect::Postgres).up,
            "-- ferro: data-dependent\n\n\
             ALTER TABLE \"author\" VALIDATE CONSTRAINT \"fk_author_team_id_club\";\n"
        );
    }

    #[test]
    fn several_models_edited_at_once_share_one_schema_step_parents_first() {
        let tag = || model("Tag", vec![pk()]);
        let mut post_after = post();
        post_after.columns.push(optional("subtitle", "string"));
        let author_after = with_columns(vec![optional("bio", "string")]);
        let migration = edit(
            vec![author(), post(), tag()],
            vec![author_after, post_after, tag()],
            &BOTH,
        );
        for dialect in BOTH {
            assert_eq!(
                rendering(&migration, dialect.into()).up,
                "ALTER TABLE \"author\" ADD COLUMN \"bio\" varchar;\n\n\
                 ALTER TABLE \"post\" ADD COLUMN \"subtitle\" varchar;\n"
            );
            assert_eq!(
                rendering(&migration, dialect.into()).down,
                "ALTER TABLE \"author\" DROP COLUMN \"bio\";\n\n\
                 ALTER TABLE \"post\" DROP COLUMN \"subtitle\";\n"
            );
        }
    }

    /// What the create pass writes for `model` on SQLite, its `CREATE TABLE`
    /// naming `_ferro_new_<table>`: a rebuild's first statement (ADR-0034).
    fn created_as_new(model: &SchemaModel) -> String {
        let emission = render_create_table(model, Dialect::Sqlite).expect("create");
        let table = format!("\"{}\"", model.table_name);
        let renamed = format!("\"_ferro_new_{}\"", model.table_name);
        emission.create_sql.replacen(&table, &renamed, 1)
    }

    /// The indexes the create pass builds for `model` after its table.
    fn create_pass_indexes(model: &SchemaModel) -> Vec<String> {
        render_create_table(model, Dialect::Sqlite)
            .expect("create")
            .post_create_sqls
    }

    /// The check a rebuild runs on a retyped column of `author`.
    fn guarded(column: &str, target: &str, class: &str) -> Vec<String> {
        vec![
            format!(
                "CREATE TEMP TABLE \"_ferro_rebuild_guard\" (\"value\", CONSTRAINT \
                 \"ferro: author.{column} has a value that cannot become {target}\" CHECK (0))"
            ),
            format!(
                "INSERT INTO \"_ferro_rebuild_guard\" (\"value\") SELECT \"{column}\" FROM \
                 \"_ferro_new_author\" WHERE typeof(\"{column}\") NOT IN ({class}, 'null')"
            ),
            "DROP TABLE \"_ferro_rebuild_guard\"".to_string(),
        ]
    }

    #[test]
    fn a_sqlite_type_change_is_a_table_rebuild_copying_as_it_stands_and_checked_both_ways() {
        let mut before = with_columns(vec![column("age", "integer")]);
        before.columns[1].unique = true;
        before.uniques.push(ferro_schema_ir::SchemaUnique {
            name: "uq_author_name".into(),
            columns: vec!["name".into()],
        });
        let mut after = before.clone();
        after.columns[3] = column("age", "string");
        let migration = edit(vec![before.clone()], vec![after.clone()], &BOTH);
        let sqlite = rendering(&migration, StepDialect::Sqlite);
        let mut up = vec![
            created_as_new(&after),
            "INSERT INTO \"_ferro_new_author\" (\"id\", \"name\", \"status\", \"age\") \
             SELECT \"id\", \"name\", \"status\", \"age\" FROM \"author\""
                .to_string(),
        ];
        up.extend(guarded("age", "varchar", "'text'"));
        up.extend([
            "DROP TABLE \"author\"".to_string(),
            "ALTER TABLE \"_ferro_new_author\" RENAME TO \"author\"".to_string(),
        ]);
        up.extend(create_pass_indexes(&after));
        assert_eq!(
            sqlite.up,
            file(
                &up,
                "-- ferro: foreign-keys-off\n-- ferro: data-dependent\n"
            )
        );
        assert!(sqlite.headers.foreign_keys_off && sqlite.headers.data_dependent);
        let mut down = vec![
            created_as_new(&before),
            "INSERT INTO \"_ferro_new_author\" (\"id\", \"name\", \"status\", \"age\") \
             SELECT \"id\", \"name\", \"status\", \"age\" FROM \"author\""
                .to_string(),
        ];
        down.extend(guarded("age", "integer", "'integer'"));
        down.extend([
            "DROP TABLE \"author\"".to_string(),
            "ALTER TABLE \"_ferro_new_author\" RENAME TO \"author\"".to_string(),
        ]);
        down.extend(create_pass_indexes(&before));
        assert_eq!(
            sqlite.down,
            file(
                &down,
                "-- ferro: foreign-keys-off\n-- ferro: data-dependent\n"
            )
        );
        assert!(!sqlite.down_headers.destructive);
        // Postgres: byte-unchanged from #524.
        let pg = rendering(&migration, StepDialect::Postgres);
        assert_eq!(
            pg.up,
            "-- ferro: data-dependent\n\n\
             ALTER TABLE \"author\" ALTER COLUMN \"age\" TYPE varchar USING \"age\"::varchar;\n"
        );
        assert!(!pg.headers.foreign_keys_off);
    }

    /// The rebuild statements of `table` from `before` into `after` with
    /// `values` copied: what the generator folds a table's changes into.
    fn rebuild_of(
        before: &SchemaModel,
        after: &SchemaModel,
        values: &[(&str, &str)],
    ) -> Vec<String> {
        let casts: Vec<(String, String)> = values
            .iter()
            .map(|(column, value)| (column.to_string(), value.to_string()))
            .collect();
        rebuild::render(&after.table_name, after, before, &casts).expect("rebuild")
    }

    #[test]
    fn two_tables_rebuilt_in_one_phase_share_one_step_and_one_header() {
        let age = |logical: &str| optional("age", logical);
        let author_before = with_columns(vec![age("integer")]);
        let author_after = with_columns(vec![age("string")]);
        let mut post_before = post();
        post_before.columns.push(age("integer"));
        let mut post_after = post();
        post_after.columns.push(age("string"));
        let migration = edit(
            vec![author_before.clone(), post_before.clone()],
            vec![author_after.clone(), post_after.clone()],
            &[Dialect::Sqlite],
        );
        let sqlite = rendering(&migration, StepDialect::Sqlite);
        let cast: [(&str, &str); 0] = [];
        let mut up = rebuild_of(&author_before, &author_after, &cast);
        up.extend(rebuild_of(&post_before, &post_after, &cast));
        assert_eq!(
            sqlite.up,
            file(
                &up,
                "-- ferro: foreign-keys-off\n-- ferro: data-dependent\n"
            )
        );
    }

    #[test]
    fn a_type_change_and_a_new_index_on_one_table_copy_it_once_and_build_the_index_after() {
        let before = with_columns(vec![
            optional("age", "integer"),
            optional("email", "string"),
        ]);
        let shape = with_columns(vec![optional("age", "string"), optional("email", "string")]);
        let mut after = shape.clone();
        after.indexes.push(ferro_schema_ir::SchemaIndex {
            name: "idx_author_email".into(),
            columns: vec!["email".into()],
            unique: false,
        });
        let migration = edit(
            vec![before.clone()],
            vec![after.clone()],
            &[Dialect::Sqlite],
        );
        assert_eq!(step_names(&migration), ["01_schema", "02_idx_author_email"]);
        // The rebuild recreates the table as it stands after its step; the
        // index step builds the index once, over the copied rows (ADR-0046).
        let sqlite = step(&migration, "01_schema", Dialect::Sqlite);
        assert_eq!(
            sqlite.up,
            file(
                &rebuild_of(&before, &shape, &[]),
                "-- ferro: foreign-keys-off\n-- ferro: data-dependent\n"
            )
        );
        assert_eq!(sqlite.up.matches("CREATE TABLE").count(), 1);
        assert!(!sqlite.up.contains("idx_author_email"));
        assert_eq!(
            sqlite.down,
            file(
                &rebuild_of(&shape, &before, &[]),
                "-- ferro: foreign-keys-off\n-- ferro: data-dependent\n"
            )
        );
        let index = step(&migration, "02_idx_author_email", Dialect::Sqlite);
        assert_eq!(
            index.up,
            "CREATE INDEX IF NOT EXISTS \"idx_author_email\" ON \"author\" (\"email\");\n"
        );
        assert_eq!(index.down, "DROP INDEX IF EXISTS \"idx_author_email\";\n");
    }

    #[test]
    fn a_check_added_changed_or_dropped_on_sqlite_is_a_rebuild_carrying_it_inline() {
        let plain = with_columns(vec![optional("tier", "string")]);
        let checked = |values: &[&str]| {
            let mut model = plain.clone();
            model.checks.push(ferro_schema_ir::SchemaCheck {
                name: "ck_author_tier".into(),
                column: "tier".into(),
                values: values.iter().map(|v| v.to_string()).collect(),
            });
            model
        };
        let free = checked(&["'free'"]);
        let pro = checked(&["'free'", "'pro'"]);
        for (before, after) in [(&plain, &free), (&free, &pro), (&free, &plain)] {
            // Postgres stages an added or changed check NOT VALID and
            // validates it in its own step; a drop needs no validation.
            let both = edit(vec![before.clone()], vec![after.clone()], &BOTH);
            if after.checks.is_empty() {
                assert_eq!(step_names(&both), ["01_schema"]);
            } else {
                assert_eq!(step_names(&both), ["01_schema", "02_validate"]);
                assert!(
                    step(&both, "01_schema", Dialect::Postgres)
                        .up
                        .contains("CHECK (\"tier\" IN ('free'")
                );
                assert!(
                    step(&both, "01_schema", Dialect::Postgres)
                        .up
                        .contains("NOT VALID")
                );
            }
            let migration = edit(
                vec![before.clone()],
                vec![after.clone()],
                &[Dialect::Sqlite],
            );
            let sqlite = rendering(&migration, StepDialect::Sqlite);
            assert!(sqlite.headers.foreign_keys_off);
            assert_eq!(
                sqlite.up,
                file(&rebuild_of(before, after, &[]), &sqlite.headers.render())
            );
            assert_eq!(
                sqlite.up.contains("CONSTRAINT \"ck_author_tier\""),
                !after.checks.is_empty()
            );
            assert_eq!(
                sqlite.down,
                file(
                    &rebuild_of(after, before, &[]),
                    &sqlite.down_headers.render()
                )
            );
            // Adding or changing a check can fail on the rows; dropping one
            // cannot.
            assert_eq!(sqlite.headers.data_dependent, !after.checks.is_empty());
        }
    }

    #[test]
    fn a_foreign_key_column_dropped_or_retargeted_on_sqlite_is_a_rebuild() {
        let team = model("Team", vec![pk()]);
        let club = model("Club", vec![pk()]);
        let fk = |to: &str| SchemaForeignKey {
            renamed_from: None,
            column: "team_id".into(),
            to_table: to.into(),
            to_column: "id".into(),
            on_delete: Some("CASCADE".into()),
            name: Some(format!("fk_author_team_id_{to}")),
        };
        let mut on_team = with_columns(vec![optional("team_id", "integer")]);
        on_team.foreign_keys.push(fk("team"));
        let mut on_club = on_team.clone();
        on_club.foreign_keys = vec![fk("club")];
        // C3: retarget.
        let migration = edit(
            vec![team.clone(), club.clone(), on_team.clone()],
            vec![team.clone(), club.clone(), on_club.clone()],
            &[Dialect::Sqlite],
        );
        let sqlite = rendering(&migration, StepDialect::Sqlite);
        assert_eq!(
            sqlite.up,
            file(
                &rebuild_of(&on_team, &on_club, &[]),
                &sqlite.headers.render()
            )
        );
        assert!(sqlite.up.contains("REFERENCES \"club\""), "{}", sqlite.up);
        assert!(
            sqlite.down.contains("REFERENCES \"team\""),
            "{}",
            sqlite.down
        );
        // C2: drop the column, and its down puts it back by a rebuild too.
        let plain = author();
        let migration = edit(
            vec![team.clone(), on_team.clone()],
            vec![team.clone(), plain.clone()],
            &[Dialect::Sqlite],
        );
        let sqlite = rendering(&migration, StepDialect::Sqlite);
        assert!(sqlite.headers.destructive && sqlite.headers.foreign_keys_off);
        assert_eq!(
            sqlite.up,
            file(&rebuild_of(&on_team, &plain, &[]), &sqlite.headers.render())
        );
        // Its down puts the column back natively, an inline REFERENCES.
        assert_eq!(
            sqlite.down,
            "ALTER TABLE \"author\" ADD COLUMN \"team_id\" integer REFERENCES \"team\"(\"id\") \
             ON DELETE CASCADE;\n"
        );
        // A1 with a foreign key: native up, rebuilt down.
        let migration = edit(
            vec![team.clone(), plain.clone()],
            vec![team, on_team.clone()],
            &[Dialect::Sqlite],
        );
        let sqlite = rendering(&migration, StepDialect::Sqlite);
        assert!(!sqlite.headers.foreign_keys_off);
        assert!(
            sqlite
                .up
                .starts_with("ALTER TABLE \"author\" ADD COLUMN \"team_id\"")
        );
        assert_eq!(
            sqlite.down,
            file(
                &rebuild_of(&on_team, &plain, &[]),
                "-- ferro: foreign-keys-off\n"
            )
        );
    }

    #[test]
    fn a_column_a_table_check_names_is_dropped_with_its_check_in_one_rebuild() {
        let mut before = with_columns(vec![optional("age", "integer")]);
        before.table_checks.push(ferro_schema_ir::SchemaTableCheck {
            name: "ck_author_age_positive".into(),
            predicate: ferro_schema_ir::CheckExpr::IsNotNull {
                column: "age".into(),
            },
        });
        let migration = edit(vec![before.clone()], vec![author()], &[Dialect::Sqlite]);
        let sqlite = rendering(&migration, StepDialect::Sqlite);
        assert!(sqlite.headers.foreign_keys_off && sqlite.headers.destructive);
        assert_eq!(
            sqlite.up,
            file(
                &rebuild_of(&before, &author(), &[]),
                &sqlite.headers.render()
            )
        );
        assert_eq!(
            sqlite.down,
            file(
                &rebuild_of(&author(), &before, &[]),
                &sqlite.down_headers.render()
            )
        );
    }

    #[test]
    fn a_required_foreign_key_column_with_a_default_copies_the_default_into_its_rows() {
        let team = model("Team", vec![pk()]);
        let mut after = with_columns(vec![SchemaColumn {
            default: Some(serde_json::json!(1)),
            ..column("team_id", "integer")
        }]);
        after.foreign_keys.push(SchemaForeignKey {
            renamed_from: None,
            column: "team_id".into(),
            to_table: "team".into(),
            to_column: "id".into(),
            on_delete: Some("CASCADE".into()),
            name: Some("fk_author_team_id_team".into()),
        });
        let migration = edit(
            vec![team.clone(), author()],
            vec![team, after.clone()],
            &[Dialect::Sqlite],
        );
        let sqlite = rendering(&migration, StepDialect::Sqlite);
        assert_eq!(
            sqlite.up,
            file(
                &rebuild_of(&author(), &after, &[("team_id", "1")]),
                &sqlite.headers.render()
            )
        );
        assert!(sqlite.headers.foreign_keys_off);
    }

    #[test]
    fn shapes_this_generator_cannot_render_yet_are_refused_naming_their_ticket() {
        assert_eq!(
            refusal(
                vec![author()],
                vec![with_columns(vec![column("slug", "string")])],
                &BOTH
            ),
            "not generated yet: AddColumn on author needs a backfill (ticket #534)"
        );
        assert_eq!(
            refusal(
                vec![with_columns(vec![optional("bio", "string")])],
                vec![with_columns(vec![column("bio", "string")])],
                &[Dialect::Postgres]
            ),
            "not generated yet: AlterColumnNullability on author needs a backfill (ticket #534)"
        );
    }

    #[test]
    fn a_primary_key_change_is_refused_with_the_recipe() {
        let mut moved = author();
        moved.columns[0].primary_key = false;
        moved.columns[0].autoincrement = false;
        moved.columns[1].primary_key = true;
        for dialects in [&[Dialect::Postgres][..], &[Dialect::Sqlite][..]] {
            assert_eq!(
                refusal(vec![author()], vec![moved.clone()], dialects),
                "not generated yet: a primary-key change on author (ticket #536): a table's \
                 primary key cannot change in place; declare a new model with the new key, \
                 copy the rows across, then drop the old model"
            );
        }
    }

    /// `author` with its `status` column declaring `labels`, renamed from
    /// the `(new, old)` pairs of `hints`, under `type_name`.
    fn relabelled(type_name: &str, labels: &[&str], hints: &[(&str, &str)]) -> SchemaModel {
        let mut model = author();
        model.columns[2] = SchemaColumn {
            enum_type_name: Some(type_name.into()),
            enum_renamed_labels: (!hints.is_empty()).then(|| {
                ferro_schema_ir::SchemaRenamedLabels {
                    enum_class: "Status".into(),
                    labels: hints
                        .iter()
                        .map(|(new, old)| (new.to_string(), old.to_string()))
                        .collect(),
                }
            }),
            ..status(labels)
        };
        model
    }

    #[test]
    fn an_added_label_is_its_own_first_step_with_nothing_to_reverse() {
        let added = relabelled("status", &["draft", "live", "gone"], &[]);
        let migration = edit(vec![author()], vec![added.clone()], &BOTH);
        assert_eq!(step_names(&migration), ["01_labels"]);
        assert_eq!(migration.summary, "new enum labels: status.gone");
        let pg = step(&migration, "01_labels", Dialect::Postgres);
        // The pass's statement, through the one renderer (I-1 item 11).
        let up = pass(&ir(vec![author()]), &ir(vec![added]), Dialect::Postgres);
        assert_eq!(up, ["ALTER TYPE \"status\" ADD VALUE IF NOT EXISTS 'gone'"]);
        assert_eq!(pg.up, file(&up, ""));
        assert_eq!(pg.headers, Headers::default());
        assert_eq!(
            pg.down,
            "-- ferro: nothing-to-reverse Postgres cannot drop an enum label; 'gone' stays\n"
        );
        assert_eq!(
            pg.down_headers.nothing_to_reverse.as_deref(),
            Some("Postgres cannot drop an enum label; 'gone' stays")
        );
        let sqlite = step(&migration, "01_labels", Dialect::Sqlite);
        assert_eq!(sqlite.up, NOT_APPLICABLE);
        assert_eq!(sqlite.down, NOT_APPLICABLE);
    }

    #[test]
    fn a_label_longer_than_every_other_widens_the_sqlite_column_in_the_schema_step() {
        // SQLite stores an enum as varchar(<longest label>): a longer label
        // is that column's type change, a rebuild after the labels step.
        let added = relabelled("status", &["draft", "live", "archived"], &[]);
        let migration = edit(vec![author()], vec![added], &BOTH);
        assert_eq!(step_names(&migration), ["01_labels", "02_schema"]);
        assert_eq!(
            step(&migration, "02_schema", Dialect::Postgres).up,
            NOT_APPLICABLE
        );
        let sqlite = step(&migration, "02_schema", Dialect::Sqlite);
        assert!(sqlite.up.contains("\"status\" varchar(8)"), "{}", sqlite.up);
        assert!(
            sqlite.down.contains("\"status\" varchar(5)"),
            "{}",
            sqlite.down
        );
    }

    #[test]
    fn an_added_label_and_a_new_column_of_its_type_are_labels_then_schema() {
        let mut after = relabelled("status", &["draft", "live", "gone", "past"], &[]);
        after.columns.push(SchemaColumn {
            nullable: true,
            name: "previous".into(),
            ..after.columns[2].clone()
        });
        let migration = edit(vec![author()], vec![after.clone()], &BOTH);
        assert_eq!(step_names(&migration), ["01_labels", "02_schema"]);
        let labels = step(&migration, "01_labels", Dialect::Postgres);
        assert_eq!(
            labels.up,
            "ALTER TYPE \"status\" ADD VALUE IF NOT EXISTS 'gone';\n\n\
             ALTER TYPE \"status\" ADD VALUE IF NOT EXISTS 'past';\n"
        );
        assert!(
            labels.down.ends_with("'gone' and 'past' stay\n"),
            "{}",
            labels.down
        );
        // The column's statements are the pass's, after its label additions.
        let schema = step(&migration, "02_schema", Dialect::Postgres);
        let pass_up = pass(&ir(vec![author()]), &ir(vec![after]), Dialect::Postgres);
        assert_eq!(schema.up, file(&pass_up[2..], ""));
        assert!(
            schema
                .up
                .ends_with("ALTER TABLE \"author\" ADD COLUMN \"previous\" status;\n"),
            "{}",
            schema.up
        );
        assert_eq!(
            schema.down,
            "ALTER TABLE \"author\" DROP COLUMN \"previous\";\n"
        );
        assert!(
            step(&migration, "02_schema", Dialect::Sqlite)
                .up
                .contains("ADD COLUMN \"previous\"")
        );
    }

    #[test]
    fn a_renamed_label_is_rename_value_on_postgres_and_an_update_on_sqlite_both_ways() {
        let after = relabelled("status", &["draft", "open"], &[("open", "live")]);
        let migration = edit(vec![author()], vec![after.clone()], &BOTH);
        assert_eq!(step_names(&migration), ["01_schema"]);
        assert_eq!(migration.summary, "renamed enum labels: status.live → open");
        let pg = step(&migration, "01_schema", Dialect::Postgres);
        assert_eq!(
            pg.up,
            "ALTER TYPE \"status\" RENAME VALUE 'live' TO 'open';\n"
        );
        assert_eq!(
            pg.down,
            "ALTER TYPE \"status\" RENAME VALUE 'open' TO 'live';\n"
        );
        let sqlite = step(&migration, "01_schema", Dialect::Sqlite);
        assert_eq!(
            sqlite.up,
            "-- ferro: data-dependent\n\n\
             UPDATE \"author\" SET \"status\" = 'open' WHERE \"status\" = 'live';\n"
        );
        assert_eq!(
            sqlite.down,
            "-- ferro: data-dependent\n\n\
             UPDATE \"author\" SET \"status\" = 'live' WHERE \"status\" = 'open';\n"
        );
        // After its migration the hint is inert: no schema change.
        let parent = snapshot_of(&ir(vec![after.clone()]), None);
        assert_eq!(generate(Some(&parent), &ir(vec![after]), &BOTH), Ok(None));

        // A longer spelling widens SQLite's column: the table is rebuilt at
        // the new width, its rows relabelled as the rebuild copies them, and
        // back the same way on the down.
        let wider = relabelled("status", &["draft", "published"], &[("published", "live")]);
        let migration = edit(vec![author()], vec![wider], &[Dialect::Sqlite]);
        let sqlite = step(&migration, "01_schema", Dialect::Sqlite);
        assert!(!sqlite.up.contains("UPDATE"), "{}", sqlite.up);
        assert!(sqlite.headers.data_dependent && sqlite.headers.foreign_keys_off);
        assert!(sqlite.up.contains("\"status\" varchar(9)"), "{}", sqlite.up);
        assert!(
            sqlite.up.contains(
                "SELECT \"id\", \"name\", CASE \"status\" WHEN 'live' THEN 'published' \
                 ELSE \"status\" END FROM \"author\""
            ),
            "{}",
            sqlite.up
        );
        assert!(
            sqlite
                .down
                .contains("CASE \"status\" WHEN 'published' THEN 'live' ELSE \"status\" END"),
            "{}",
            sqlite.down
        );
        assert!(
            sqlite.down.contains("\"status\" varchar(5)"),
            "{}",
            sqlite.down
        );
    }

    /// `relabelled`, its `status` stored as text, with a `db_check` when
    /// `checked`.
    fn text_stored(labels: &[&str], hints: &[(&str, &str)], checked: bool) -> SchemaModel {
        let mut model = relabelled("status", labels, hints);
        model.columns[2].db_type = Some("text".into());
        model.columns[2].db_type_explicit = Some(true);
        if checked {
            model.checks.push(ferro_schema_ir::SchemaCheck {
                name: "ck_author_status".into(),
                column: "status".into(),
                values: labels.iter().map(|l| format!("'{l}'")).collect(),
            });
        }
        model
    }

    #[test]
    fn a_renamed_label_on_a_text_stored_enum_updates_its_rows_on_both_dialects() {
        let update = |from: &str, to: &str| {
            format!("UPDATE \"author\" SET \"status\" = '{to}' WHERE \"status\" = '{from}'")
        };
        let before = text_stored(&["draft", "live"], &[], false);
        let after = text_stored(&["draft", "open"], &[("open", "live")], false);
        let migration = edit(vec![before], vec![after.clone()], &BOTH);
        assert_eq!(step_names(&migration), ["01_schema"]);
        for dialect in BOTH {
            let r = step(&migration, "01_schema", dialect);
            assert_eq!(
                r.up,
                format!("-- ferro: data-dependent\n\n{};\n", update("live", "open")),
                "{dialect:?}"
            );
            assert_eq!(
                r.down,
                format!("-- ferro: data-dependent\n\n{};\n", update("open", "live")),
                "{dialect:?}"
            );
        }
        let parent = snapshot_of(&ir(vec![after.clone()]), None);
        assert_eq!(generate(Some(&parent), &ir(vec![after]), &BOTH), Ok(None));
    }

    #[test]
    fn a_checked_text_stored_enum_relabels_between_its_check_s_drop_and_add() {
        let before = text_stored(&["draft", "live"], &[], true);
        let after = text_stored(&["draft", "open"], &[("open", "live")], true);
        let migration = edit(vec![before], vec![after.clone()], &BOTH);
        assert_eq!(step_names(&migration), ["01_schema"]);
        // Postgres: the old check allows only the old label, so it goes
        // first, and the new one is added (validated) over the new labels.
        let pg = step(&migration, "01_schema", Dialect::Postgres);
        assert_eq!(
            pg.up,
            "-- ferro: data-dependent\n\n\
             ALTER TABLE \"author\" DROP CONSTRAINT \"ck_author_status\";\n\n\
             UPDATE \"author\" SET \"status\" = 'open' WHERE \"status\" = 'live';\n\n\
             ALTER TABLE \"author\" ADD CONSTRAINT \"ck_author_status\" \
             CHECK (\"status\" IN ('draft', 'open'));\n"
        );
        assert_eq!(
            pg.down,
            "-- ferro: data-dependent\n\n\
             ALTER TABLE \"author\" DROP CONSTRAINT \"ck_author_status\";\n\n\
             UPDATE \"author\" SET \"status\" = 'live' WHERE \"status\" = 'open';\n\n\
             ALTER TABLE \"author\" ADD CONSTRAINT \"ck_author_status\" \
             CHECK (\"status\" IN ('draft', 'live'));\n"
        );
        // SQLite: the check lives in CREATE TABLE, so the table is rebuilt
        // and its rows relabelled as they are copied under the new check.
        let sqlite = step(&migration, "01_schema", Dialect::Sqlite);
        assert!(!sqlite.up.contains("UPDATE"), "{}", sqlite.up);
        assert!(
            sqlite
                .up
                .contains("CASE \"status\" WHEN 'live' THEN 'open' ELSE \"status\" END"),
            "{}",
            sqlite.up
        );
        assert!(
            sqlite
                .down
                .contains("CASE \"status\" WHEN 'open' THEN 'live' ELSE \"status\" END"),
            "{}",
            sqlite.down
        );
        let parent = snapshot_of(&ir(vec![after.clone()]), None);
        assert_eq!(generate(Some(&parent), &ir(vec![after]), &BOTH), Ok(None));
    }

    #[test]
    fn a_label_renamed_from_one_still_declared_is_refused_naming_it() {
        let still = relabelled(
            "status",
            &["draft", "live", "published"],
            &[("published", "live")],
        );
        assert_eq!(
            refusal(vec![author()], vec![still], &BOTH),
            "rename hint refused: enum Status (type \"status\") declares \
             __ferro_renamed_labels__ {\"published\": \"live\"}, but Status still declares the \
             label \"live\": a label cannot be renamed from one the enum keeps; delete the hint \
             or the old member"
        );
    }

    #[test]
    fn a_type_every_column_of_which_moved_is_renamed_on_postgres_and_nothing_on_sqlite() {
        let after = relabelled("authorstatus", &["draft", "live"], &[]);
        let migration = edit(vec![author()], vec![after], &BOTH);
        assert_eq!(step_names(&migration), ["01_schema"]);
        assert_eq!(
            migration.summary,
            "renamed enum types: status → authorstatus"
        );
        let pg = step(&migration, "01_schema", Dialect::Postgres);
        assert_eq!(pg.up, "ALTER TYPE \"status\" RENAME TO \"authorstatus\";\n");
        assert_eq!(
            pg.down,
            "ALTER TYPE \"authorstatus\" RENAME TO \"status\";\n"
        );
        let sqlite = step(&migration, "01_schema", Dialect::Sqlite);
        assert_eq!(sqlite.up, NOT_APPLICABLE);
        assert_eq!(sqlite.down, NOT_APPLICABLE);
    }

    #[test]
    fn a_new_table_reusing_a_type_neither_creates_nor_drops_it() {
        let editor = model("Editor", vec![pk(), status(&["draft", "live"])]);
        let migration = edit(vec![author()], vec![author(), editor], &[Dialect::Postgres]);
        let pg = rendering(&migration, StepDialect::Postgres);
        assert!(
            pg.up.starts_with("CREATE TABLE IF NOT EXISTS \"editor\""),
            "{}",
            pg.up
        );
        assert!(!pg.up.contains("TYPE"), "{}", pg.up);
        assert_eq!(pg.down, "DROP TABLE \"editor\";\n");
        // A new type is created first and dropped last (B1).
        let mut fresh = author();
        fresh.columns[2].enum_type_name = Some("authorstatus".into());
        let created = generate(None, &ir(vec![fresh]), &[Dialect::Postgres])
            .expect("ok")
            .expect("a change");
        let pg = rendering(&created, StepDialect::Postgres);
        assert!(pg.up.starts_with("DO $$ BEGIN IF NOT EXISTS"), "{}", pg.up);
        assert!(pg.up.contains("CREATE TYPE \"authorstatus\""), "{}", pg.up);
        assert!(
            pg.down.ends_with("DROP TYPE \"authorstatus\";\n"),
            "{}",
            pg.down
        );
    }

    #[test]
    fn a_removed_label_or_a_partial_type_move_is_refused() {
        let removed = relabelled("status", &["draft"], &[]);
        let err = refusal(vec![author()], vec![removed], &BOTH);
        assert!(err.starts_with("not generated yet:"), "{err}");
        // One of two columns of the type moving to a new type is a type
        // change, the swap-type recipe's (ticket #536), never a rename.
        let mut before = author();
        before.columns.push(SchemaColumn {
            name: "previous".into(),
            ..before.columns[2].clone()
        });
        let mut after = before.clone();
        after.columns[3].enum_type_name = Some("authorstatus".into());
        assert_eq!(
            refusal(vec![before], vec![after], &[Dialect::Postgres]),
            "not generated yet: AlterColumnType on author (ticket #536)"
        );
    }

    #[test]
    fn row_security_on_a_new_table_renders_on_postgres_and_warns_on_sqlite() {
        let guarded = SchemaModel {
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
            ..model("Ledger", vec![pk(), column("owner_id", "integer")])
        };
        let migration = generate(None, &ir(vec![guarded.clone()]), &BOTH)
            .expect("ok")
            .expect("a change");
        let pg = rendering(&migration, StepDialect::Postgres);
        assert_eq!(pg.up, file(&create_pass(&guarded, Dialect::Postgres), ""));
        assert!(pg.up.contains("ENABLE ROW LEVEL SECURITY"), "{}", pg.up);
        assert!(pg.up.contains("FORCE ROW LEVEL SECURITY"), "{}", pg.up);
        assert!(
            pg.up.contains("CREATE POLICY \"rls_ledger_owner_id\""),
            "{}",
            pg.up
        );
        let sqlite = rendering(&migration, StepDialect::Sqlite);
        assert!(
            !sqlite.headers.not_applicable,
            "SQLite still creates the table"
        );
        assert!(!sqlite.up.contains("POLICY"), "{}", sqlite.up);
        assert_eq!(migration.warnings.len(), 1, "{:?}", migration.warnings);

        // A later migration over the same table is no schema change on either
        // dialect: the SQLite row-security warning is standing, not a change.
        let parent = snapshot_of(&ir(vec![guarded.clone()]), None);
        assert_eq!(generate(Some(&parent), &ir(vec![guarded]), &BOTH), Ok(None));
    }

    /// A tenant policy on `order`, reading `setting`.
    pub(super) fn tenant_policy(setting: &str) -> SchemaRowPolicy {
        SchemaRowPolicy {
            name: "rls_order_tenant_id".into(),
            command: RowPolicyCommand::All,
            restrictive: false,
            expr: RowPolicyExpr::Setting {
                column: "tenant_id".into(),
                setting: setting.into(),
            },
        }
    }

    /// `order` with `policies` under a row-security declaration, or none.
    pub(super) fn order(declared: Option<(bool, Vec<SchemaRowPolicy>)>) -> SchemaModel {
        SchemaModel {
            row_security: declared.map(|(force, policies)| SchemaRowSecurity { force, policies }),
            ..model(
                "Order",
                vec![pk(), column("tenant_id", "uuid"), column("owner", "string")],
            )
        }
    }

    fn owner_policy() -> SchemaRowPolicy {
        SchemaRowPolicy {
            name: "rls_order_owner".into(),
            command: RowPolicyCommand::Select,
            restrictive: true,
            expr: RowPolicyExpr::Setting {
                column: "owner".into(),
                setting: "app.owner".into(),
            },
        }
    }

    fn enable() -> String {
        ferro_ddl_lowering::render_enable_row_security("order")
    }
    fn force() -> String {
        ferro_ddl_lowering::render_force_row_security("order")
    }
    fn no_force() -> String {
        ferro_ddl_lowering::render_no_force_row_security("order")
    }
    fn disable() -> String {
        ferro_ddl_lowering::render_disable_row_security("order")
    }
    fn create_policy(model: &SchemaModel, policy: &SchemaRowPolicy) -> String {
        ferro_ddl_lowering::render_create_row_policy(model, policy).expect("policy")
    }
    fn drop_policy(name: &str) -> String {
        ferro_ddl_lowering::render_drop_row_policy("order", name)
    }

    /// The one schema step of `before → after` on both dialects: the Postgres
    /// up and down equal `up` and `down`, the up is the pass's own statements
    /// for the same change, and SQLite has nothing to do either way.
    fn assert_row_security_step(
        before: SchemaModel,
        after: SchemaModel,
        up: &[String],
        down: &[String],
    ) {
        let migration = edit(vec![before.clone()], vec![after.clone()], &BOTH);
        assert_eq!(step_names(&migration), ["01_schema"]);
        let pg = step(&migration, "01_schema", Dialect::Postgres);
        assert_eq!(pg.up, file(up, ""));
        assert_eq!(pg.down, file(down, ""));
        assert_eq!(pg.headers, Headers::default());
        assert_eq!(pg.down_headers, Headers::default());
        let sqlite = step(&migration, "01_schema", Dialect::Sqlite);
        assert_eq!(sqlite.up, NOT_APPLICABLE);
        assert_eq!(sqlite.down, NOT_APPLICABLE);
        // Every statement is the pass's for the same declaration (I-1 15–16).
        let (from, to) = (ir(vec![before.clone()]), ir(vec![after.clone()]));
        let mut pass_up = pass(&from, &to, Dialect::Postgres);
        pass_up.extend(teardown_pass_cannot_witness(&before, &after));
        assert_eq!(pass_up, up);
        assert_eq!(
            row_security::render(&plan(&from, &to, Dialect::Postgres).operations, &from, &to)
                .expect("render"),
            up
        );
        // Applied, the target is no schema change; nor is the parent after
        // its down.
        let parent = snapshot_of(&to, None);
        assert_eq!(generate(Some(&parent), &to, &BOTH), Ok(None));
        // A SQLite-only project has no step to write at all.
        assert_eq!(
            generate(Some(&snapshot_of(&from, None)), &to, &[Dialect::Sqlite]),
            Ok(None)
        );
    }

    /// The flag teardown a file removing a policy-less declaration owes and the
    /// pass, which reads ownership off ferro-named policies, does not plan.
    fn teardown_pass_cannot_witness(before: &SchemaModel, after: &SchemaModel) -> Vec<String> {
        match (&before.row_security, &after.row_security) {
            (Some(declared), None) if declared.policies.is_empty() => {
                let mut out = Vec::new();
                if declared.force {
                    out.push(no_force());
                }
                out.push(disable());
                out
            }
            _ => Vec::new(),
        }
    }

    #[test]
    fn e1_row_security_added_to_a_table_enables_forces_creates_and_its_down_tears_it_down() {
        let after = order(Some((true, vec![tenant_policy("app.tenant")])));
        let up = [
            enable(),
            force(),
            create_policy(&after, &tenant_policy("app.tenant")),
        ];
        assert!(up[2].contains(
            "USING (\"tenant_id\" = NULLIF(current_setting('app.tenant', true), '')::uuid)"
        ));
        assert_row_security_step(
            order(None),
            after,
            &up,
            &[drop_policy("rls_order_tenant_id"), no_force(), disable()],
        );
        // Without FORCE there is no FORCE to clear.
        let unforced = order(Some((false, vec![tenant_policy("app.tenant")])));
        assert_row_security_step(
            order(None),
            unforced.clone(),
            &[
                enable(),
                create_policy(&unforced, &tenant_policy("app.tenant")),
            ],
            &[drop_policy("rls_order_tenant_id"), disable()],
        );
    }

    #[test]
    fn e2_a_changed_policy_body_is_dropped_and_recreated_and_its_down_restores_the_old_body() {
        let before = order(Some((true, vec![tenant_policy("app.tenant")])));
        let after = order(Some((true, vec![tenant_policy("app.tenant_id")])));
        assert_row_security_step(
            before.clone(),
            after.clone(),
            &[
                drop_policy("rls_order_tenant_id"),
                create_policy(&after, &tenant_policy("app.tenant_id")),
            ],
            &[
                drop_policy("rls_order_tenant_id"),
                create_policy(&before, &tenant_policy("app.tenant")),
            ],
        );
    }

    #[test]
    fn e3_row_security_removed_is_dropped_and_torn_down_and_its_down_recreates_it() {
        let before = order(Some((true, vec![tenant_policy("app.tenant")])));
        assert_row_security_step(
            before.clone(),
            order(None),
            &[drop_policy("rls_order_tenant_id"), no_force(), disable()],
            &[
                enable(),
                force(),
                create_policy(&before, &tenant_policy("app.tenant")),
            ],
        );
    }

    #[test]
    fn a_second_policy_on_a_table_with_row_security_is_its_create_policy_alone() {
        let before = order(Some((true, vec![tenant_policy("app.tenant")])));
        let after = order(Some((
            true,
            vec![tenant_policy("app.tenant"), owner_policy()],
        )));
        assert_row_security_step(
            before,
            after.clone(),
            &[create_policy(&after, &owner_policy())],
            &[drop_policy("rls_order_owner")],
        );
    }

    #[test]
    fn a_declaration_with_no_policy_is_still_torn_down_by_the_migration_that_introduced_it() {
        // No ferro-named policy witnesses the flags, which the pass would
        // leave alone; the parent snapshot shows the migration set them.
        let bare = order(Some((true, Vec::new())));
        assert_row_security_step(
            order(None),
            bare.clone(),
            &[enable(), force()],
            &[no_force(), disable()],
        );
        assert_row_security_step(
            bare,
            order(None),
            &[no_force(), disable()],
            &[enable(), force()],
        );
    }

    #[test]
    fn an_edited_raw_policy_body_is_refused_not_written() {
        // The planner reads a raw body that differs as unverifiable (ADR-0019)
        // and plans no op for it: `new` refuses rather than write a migration
        // that leaves the old body standing.
        let raw = |body: &str| SchemaRowPolicy {
            name: "rls_order_raw".into(),
            command: RowPolicyCommand::Select,
            restrictive: false,
            expr: RowPolicyExpr::Raw {
                using: Some(body.into()),
                with_check: None,
            },
        };
        let before = order(Some((true, vec![raw("owner = 'a'")])));
        let after = order(Some((true, vec![raw("owner = 'b'")])));
        let err = refusal(vec![before], vec![after], &BOTH);
        assert!(err.starts_with("not generated yet:"), "{err}");
        assert!(err.contains("'rls_order_raw'"), "{err}");
    }

    #[test]
    fn row_security_lands_after_the_tables_column_changes_and_its_down_before_their_reverse() {
        let mut after = order(Some((true, vec![tenant_policy("app.tenant")])));
        after.columns.push(optional("note", "string"));
        let migration = edit(vec![order(None)], vec![after.clone()], &[Dialect::Postgres]);
        let pg = step(&migration, "01_schema", Dialect::Postgres);
        assert_eq!(
            pg.up,
            file(
                &[
                    "ALTER TABLE \"order\" ADD COLUMN \"note\" varchar".to_string(),
                    enable(),
                    force(),
                    create_policy(&after, &tenant_policy("app.tenant")),
                ],
                ""
            )
        );
        // Its down tears the row security down before dropping the column.
        assert_eq!(
            pg.down,
            file(
                &[
                    drop_policy("rls_order_tenant_id"),
                    no_force(),
                    disable(),
                    "ALTER TABLE \"order\" DROP COLUMN \"note\"".to_string(),
                ],
                ""
            )
        );
    }

    #[test]
    fn b1_a_new_table_with_row_security_creates_it_after_the_table_and_drops_only_the_table() {
        let after = order(Some((true, vec![tenant_policy("app.tenant")])));
        let migration = edit(vec![], vec![after.clone()], &BOTH);
        let pg = step(&migration, "01_schema", Dialect::Postgres);
        let create = create_pass(&after, Dialect::Postgres);
        assert_eq!(pg.up, file(&create, ""));
        let table_at = create
            .iter()
            .position(|s| s.starts_with("CREATE TABLE"))
            .expect("table");
        assert_eq!(
            create[table_at + 1..],
            [
                enable(),
                force(),
                create_policy(&after, &tenant_policy("app.tenant"))
            ]
        );
        assert_eq!(pg.up.matches("CREATE POLICY").count(), 1, "{}", pg.up);
        assert_eq!(pg.down, "DROP TABLE \"order\";\n");
        let sqlite = step(&migration, "01_schema", Dialect::Sqlite);
        assert!(!sqlite.up.contains("POLICY"), "{}", sqlite.up);
        assert_eq!(sqlite.down, "DROP TABLE \"order\";\n");
    }

    #[test]
    fn check_reports_an_ungenerated_change_naming_the_models() {
        let missing =
            std::env::temp_dir().join(format!("ferro-migrate-check-{}-absent", std::process::id()));
        let report = check_migrations(&missing, &ir(vec![author()]), &BOTH);
        assert!(!report.ok);
        assert_eq!(report.head, None);
        assert_eq!(report.problems.len(), 1);
        assert_eq!(report.problems[0].kind, "ungenerated");
        assert!(
            report.problems[0].message.contains("new models: Author"),
            "{}",
            report.problems[0].message
        );
    }
}
