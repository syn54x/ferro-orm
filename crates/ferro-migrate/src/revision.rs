//! The Alembic bridge's revision, decided in one call (ADR-0041, as amended
//! by ADR-0050..0052).
//!
//! A model gains a required column:
//!
//! ```python
//! class Card(Model):
//!     flavor: str        # new, NOT NULL, no default
//! ```
//!
//! `alembic revision --autogenerate` against a Postgres database holding
//! `card` writes:
//!
//! ```python
//! def upgrade():
//!     # ferro: data-dependent (fails while card has rows; ferro migrations generate the backfill: `ferro migrate new`)
//!     op.add_column('card', sa.Column('flavor', sa.String(), nullable=False))
//!
//! def downgrade():
//!     op.drop_column('card', 'flavor')
//! ```
//!
//! On SQLite, which cannot add that column in place, the same edit is
//! refused, naming `ferro migrate new`.
//!
//! [`plan_revision`] answers everything in that file: which ops the upgrade
//! and the downgrade hold, in what order, each one's statements, its marker
//! and any irreversible reason, and whether Alembic's own op writes it or the
//! pass's statements do. The bridge's translator builds Alembic ops from that
//! answer and decides nothing.

use crate::plan::{Execution, PlannedOp, Side, plan_down, plan_from_ir};
use crate::{Dialect, EmissionError, MigrationOp, Plan, PlanOptions, RenderedOp, Report};
use ferro_ddl_lowering::ReportKind;
use ferro_schema_ir::{IrEnvelope, SchemaIrPayload, SchemaModel};
use std::collections::BTreeSet;

/// One Alembic revision: the upgrade's ops, the downgrade's, and the
/// planner's one-off reports the upgrade writes as comments ahead of its ops.
#[derive(Clone, Debug, Default, PartialEq, Eq, serde::Serialize)]
pub struct Revision {
    /// What `upgrade()` writes, in the planner's order.
    pub upgrade: Vec<RevisionOp>,
    /// What `downgrade()` writes: the upgrade's artifacts planned back to
    /// the live database ([`plan_down`]), in that plan's order.
    pub downgrade: Vec<RevisionOp>,
    /// The upgrade plan's one-off reports (no op answers them, such as an
    /// enum label the model no longer declares), written as `# ferro:`
    /// comments. A recurring report stays the connect-time warning it is
    /// (ADR-0019) and is not written.
    pub reports: Vec<Report>,
}

/// One planner op as a revision writes it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RevisionOp {
    /// The op.
    pub op: MigrationOp,
    /// The statements the reconciliation pass runs for it (none for a column
    /// that demands values of existing rows, which the pass refuses and the
    /// revision writes as Alembic's plain op).
    pub statements: Vec<String>,
    /// For an `AddTable`, the row-security statements that close its
    /// `statements`: what the revision runs as written beside Alembic's
    /// `create_table`.
    pub row_security_statements: Vec<String>,
    /// Written as Alembic's own op (ADR-0041: a table, a column, its type
    /// and nullability, an index, a foreign key, a table or column rename),
    /// or, when `false`, as `op.execute` of each of `statements`.
    pub twin: bool,
    /// Its statements run committed, outside the revision's transaction
    /// (`op.get_context().autocommit_block()`): an enum label addition, which
    /// Postgres lets no later statement of the same transaction use. The
    /// generator's `labels` step is the same fact on the other door.
    pub autocommit: bool,
    /// The foreign key Alembic's `create_foreign_key` writes with the op, read
    /// from the side the revision leads to: an `AddForeignKey`'s, or the one
    /// riding a column added with no statement of the pass's (a column that
    /// demands values of existing rows, written as Alembic's plain op).
    pub foreign_key: Option<RevisionForeignKey>,
    /// For a `RedefineIndex`, the definition its create builds, read from the
    /// side the revision leads to: `(columns, unique)`.
    pub index: Option<(Vec<String>, bool)>,
    /// The `# ferro:` comment above the op.
    pub marker: Option<Marker>,
    /// Nothing this revision writes undoes it: `raise RuntimeError(<reason>)`
    /// in place of the op.
    pub irreversible: Option<String>,
}

/// A foreign key as `op.create_foreign_key` takes it.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize)]
pub struct RevisionForeignKey {
    /// The constraint's name (`fk_<table>_<col>_<to_table>`).
    pub name: Option<String>,
    /// The local column.
    pub column: String,
    /// The referenced table.
    pub to_table: String,
    /// The referenced column.
    pub to_column: String,
    /// The `ON DELETE` action, when set.
    pub on_delete: Option<String>,
}

impl From<&ferro_schema_ir::SchemaForeignKey> for RevisionForeignKey {
    fn from(fk: &ferro_schema_ir::SchemaForeignKey) -> Self {
        Self {
            name: fk.name.clone(),
            column: fk.column.clone(),
            to_table: fk.to_table.clone(),
            to_column: fk.to_column.clone(),
            on_delete: fk.on_delete.clone(),
        }
    }
}

/// What the comment above a revision op says.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Marker {
    /// It drops data (a table, a column, or the enum type that follows
    /// them). The upgrade's only: a down never carries it (ADR-0033).
    Destructive(String),
    /// It asks existing rows for a value no statement supplies; ferro
    /// migrations generate the backfill.
    DataDependent(String),
}

impl Marker {
    /// The comment's text after `# ferro: `.
    pub fn comment(&self) -> &str {
        match self {
            Marker::Destructive(text) | Marker::DataDependent(text) => text,
        }
    }
}

/// Why autogenerate writes no revision. Every refusal names its fix
/// (AGENTS.md I-6); its text is the sentence after `ferro: autogenerate
/// refused: `.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RevisionRefusal {
    /// A rename hint the planner refused (ADR-0052): it renames nothing, so
    /// the revision would be a drop and an add.
    HintRefused(Box<Report>),
    /// An op no door runs (a primary-key change, a move to or from a native
    /// enum type), with its recipe.
    Refused(crate::Refusal),
    /// A SQLite column added `NOT NULL` with no value for the rows already
    /// there: SQLite cannot add it in place.
    SqliteRequiredColumn {
        /// The op's kind.
        kind: String,
        /// `table.column`.
        subject: String,
    },
    /// A change SQLite can only make by rebuilding the table, which a
    /// revision cannot write.
    Rebuild {
        /// The op's kind.
        kind: String,
        /// What it changes.
        subject: String,
    },
    /// An op whose rendering blocks ([`Report::blocks`]): it has no statement
    /// to write.
    Blocked(Box<Report>),
    /// An op only two declared snapshots plan (`ferro migrate new`), planned
    /// from the live database: a ferro bug, refused loudly.
    SnapshotOnly {
        /// The op's kind.
        kind: String,
    },
    /// The plan does not render: a declaration that cannot render, or a side
    /// lacking what an op names.
    Render(EmissionError),
}

impl RevisionRefusal {
    /// The refusal's kind on the wire.
    pub fn kind(&self) -> &'static str {
        match self {
            RevisionRefusal::HintRefused(_) => "hint_refused",
            RevisionRefusal::Refused(_) => "refused",
            RevisionRefusal::SqliteRequiredColumn { .. } => "sqlite_required_column",
            RevisionRefusal::Rebuild { .. } => "rebuild",
            RevisionRefusal::Blocked(_) => "blocked",
            RevisionRefusal::SnapshotOnly { .. } => "snapshot_only",
            RevisionRefusal::Render(_) => "render",
        }
    }
}

impl std::fmt::Display for RevisionRefusal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            RevisionRefusal::HintRefused(report) | RevisionRefusal::Blocked(report) => {
                f.write_str(&report.text)
            }
            RevisionRefusal::Refused(refusal) => write!(f, "{refusal}"),
            RevisionRefusal::SqliteRequiredColumn { kind, subject } => write!(
                f,
                "{}. Give it a default, or write the change as a migration, which \
                 generates the backfill: `ferro migrate new`",
                cannot_add_required(kind, subject)
            ),
            RevisionRefusal::Rebuild { kind, subject } => write!(
                f,
                "{} (batch mode has no foreign-key pragma handling, so the drop cascades \
                 into ON DELETE CASCADE children). Write this change as a migration: \
                 `ferro migrate new`",
                needs_rebuild(kind, subject)
            ),
            RevisionRefusal::SnapshotOnly { kind } => write!(
                f,
                "the plan carries a {kind} op, which only two declared snapshots plan \
                 (`ferro migrate new`), never the live database the Alembic bridge diffs; \
                 this is a ferro bug, please file an issue"
            ),
            RevisionRefusal::Render(error) => write!(f, "{error}"),
        }
    }
}

impl std::error::Error for RevisionRefusal {}

impl serde::Serialize for RevisionRefusal {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        use serde::ser::SerializeStruct;
        let mut out = serializer.serialize_struct("RevisionRefusal", 2)?;
        out.serialize_field("kind", self.kind())?;
        out.serialize_field("text", &self.to_string())?;
        out.end()
    }
}

impl serde::Serialize for RevisionOp {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        use serde::ser::SerializeStruct;
        #[derive(serde::Serialize)]
        struct Index<'a> {
            columns: &'a [String],
            unique: bool,
        }
        #[derive(serde::Serialize)]
        struct Marked<'a> {
            kind: &'static str,
            comment: &'a str,
        }
        let mut out = serializer.serialize_struct("RevisionOp", 9)?;
        out.serialize_field("op", &self.op)?;
        out.serialize_field("statements", &self.statements)?;
        out.serialize_field("row_security_statements", &self.row_security_statements)?;
        out.serialize_field("twin", &self.twin)?;
        out.serialize_field("autocommit", &self.autocommit)?;
        out.serialize_field("foreign_key", &self.foreign_key)?;
        out.serialize_field(
            "index",
            &self.index.as_ref().map(|(columns, unique)| Index {
                columns,
                unique: *unique,
            }),
        )?;
        out.serialize_field(
            "marker",
            &self.marker.as_ref().map(|marker| Marked {
                kind: match marker {
                    Marker::Destructive(_) => "destructive",
                    Marker::DataDependent(_) => "data_dependent",
                },
                comment: marker.comment(),
            }),
        )?;
        out.serialize_field("irreversible", &self.irreversible)?;
        out.end()
    }
}

/// The revision `alembic revision --autogenerate` writes for the database
/// `live` and the models `declared` on `dialect`.
///
/// The upgrade is the plan from `live` to `declared` with destructive changes
/// on (a revision is reviewed before it runs); the downgrade is
/// `plan_down(upgrade, declared, live)`, the one down every door uses. Both
/// render through the one renderer, and four rules turn their verdicts into
/// the revision:
///
/// - a change SQLite can only make by a table rebuild is refused going up
///   and irreversible going down;
/// - a change that demands values of existing rows is written as Alembic's
///   plain op under its `data-dependent` marker (refused going up and
///   irreversible going down on SQLite, which cannot add it in place);
/// - an op that renders nothing on the dialect is left out;
/// - an op whose rendering blocks is refused going up and irreversible going
///   down, with the renderer's reason.
///
/// An upgrade with nothing to write has nothing to undo: the revision is
/// then empty.
///
/// # Errors
/// A [`RevisionRefusal`] naming the change no revision writes and its fix.
pub fn plan_revision(
    live: &Side,
    declared: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
) -> Result<Revision, RevisionRefusal> {
    let declared = Side::declared(declared.clone());
    let up = plan_from_ir(live, &declared, dialect, PlanOptions { destructive: true });
    if let Some(report) = up
        .reports
        .iter()
        .find(|report| matches!(report.kind, ReportKind::HintRefused(_)))
    {
        return Err(RevisionRefusal::HintRefused(Box::new(report.clone())));
    }
    if let Some(refusal) = up
        .operations
        .iter()
        .find_map(|planned| upgrade_refusal(planned, dialect))
    {
        return Err(refusal);
    }
    let mut upgrade = Vec::new();
    for (planned, rendered) in up.operations.iter().zip(rendered(&up)?) {
        upgrade.extend(upgrade_op(planned, rendered, &up)?);
    }
    if upgrade.is_empty() {
        return Ok(Revision::default());
    }
    let up_ops: Vec<MigrationOp> = up.ops().cloned().collect();
    let down = plan_down(&up_ops, &declared, live, dialect);
    let mut downgrade = Vec::new();
    for (planned, rendered) in down.operations.iter().zip(rendered(&down)?) {
        downgrade.extend(downgrade_op(planned, rendered, &down)?);
    }
    Ok(Revision {
        upgrade,
        downgrade,
        reports: up
            .reports
            .into_iter()
            .filter(|report| !report.recurs)
            .collect(),
    })
}

/// Why the upgrade cannot write `planned` on `dialect`, read off its
/// verdict before anything renders: an op no door runs, a demanding column
/// SQLite cannot add in place, a SQLite rebuild, or an op only two declared
/// snapshots plan, which reaching here from a live database is a bug.
fn upgrade_refusal(planned: &PlannedOp, dialect: Dialect) -> Option<RevisionRefusal> {
    if snapshot_only(&planned.op) {
        return Some(RevisionRefusal::SnapshotOnly {
            kind: kind_name(&planned.op),
        });
    }
    if let Execution::Refused(refusal) = &planned.verdict.execution {
        return Some(RevisionRefusal::Refused(refusal.clone()));
    }
    if demands_column_values(planned) && dialect == Dialect::Sqlite {
        return Some(RevisionRefusal::SqliteRequiredColumn {
            kind: kind_name(&planned.op),
            subject: subject(&planned.op),
        });
    }
    if planned.verdict.execution == Execution::Rebuild {
        return Some(RevisionRefusal::Rebuild {
            kind: kind_name(&planned.op),
            subject: subject(&planned.op),
        });
    }
    None
}

/// An op a live database never takes as the side planned from (a label
/// removal, #536): going up the planner never plans one from it, and going
/// down one toward it is irreversible (ADR-0050).
fn snapshot_only(op: &MigrationOp) -> bool {
    matches!(op, MigrationOp::RemoveEnumLabel { .. })
}

/// One op of the upgrade `plan` as the revision writes it (its verdict's
/// refusals already raised): a demanding column add (`rendered` is `None`)
/// is the plain op under its marker, an op whose rendering blocks is refused
/// with the renderer's reason, and one that renders nothing is left out.
fn upgrade_op(
    planned: &PlannedOp,
    rendered: Option<RenderedOp>,
    plan: &Plan,
) -> Result<Option<RevisionOp>, RevisionRefusal> {
    if let Some(rendered) = &rendered
        && rendered.statements.is_empty()
    {
        return match rendered.reports.iter().find(|report| report.blocks()) {
            Some(report) => Err(RevisionRefusal::Blocked(Box::new(report.clone()))),
            None => Ok(None),
        };
    }
    written(planned, rendered, plan, true).map(Some)
}

/// One op of the downgrade `plan` as the revision writes it. What the live
/// database cannot express, what no door runs, a SQLite rebuild, a demanding
/// column SQLite cannot add in place and an op whose rendering blocks are
/// irreversible, with the reason; an op that renders nothing is left out.
fn downgrade_op(
    planned: &PlannedOp,
    rendered: Option<RenderedOp>,
    plan: &Plan,
) -> Result<Option<RevisionOp>, RevisionRefusal> {
    let mut reason = match &planned.verdict.execution {
        Execution::Irreversible(reason) => Some(reason.clone()),
        Execution::Refused(refusal) => Some(refusal.to_string()),
        Execution::Rebuild => Some(format!(
            "{}; `ferro migrate new` writes it",
            needs_rebuild(&kind_name(&planned.op), &subject(&planned.op))
        )),
        Execution::Native => None,
    };
    if reason.is_none() && rendered.is_none() && plan.dialect() == Dialect::Sqlite {
        reason = Some(format!(
            "{}; `ferro migrate new` writes it",
            cannot_add_required(&kind_name(&planned.op), &subject(&planned.op))
        ));
    }
    if reason.is_none()
        && let Some(rendered) = &rendered
        && rendered.statements.is_empty()
    {
        match rendered.reports.iter().find(|report| report.blocks()) {
            Some(report) => reason = Some(report.text.clone()),
            // Nothing to run on this dialect.
            None => return Ok(None),
        }
    }
    let mut op = written(planned, rendered, plan, false)?;
    op.irreversible = reason;
    Ok(Some(op))
}

/// Every op of `plan` rendered, in plan order, but a column add that demands
/// values of existing rows: the pass has no statement for it, and the
/// revision writes Alembic's plain op instead (`None`).
fn rendered(plan: &Plan) -> Result<Vec<Option<RenderedOp>>, RevisionRefusal> {
    let demanding: BTreeSet<usize> = plan
        .operations
        .iter()
        .enumerate()
        .filter(|(_, planned)| demands_column_values(planned))
        .map(|(index, _)| index)
        .collect();
    let kept: Vec<usize> = (0..plan.operations.len())
        .filter(|index| !demanding.contains(index))
        .collect();
    let mut rendered = plan
        .render_ops(&kept)
        .map_err(RevisionRefusal::Render)?
        .into_iter();
    (0..plan.operations.len())
        .map(|index| {
            if demanding.contains(&index) {
                return Ok(None);
            }
            rendered.next().map(Some).ok_or_else(|| {
                RevisionRefusal::Render(EmissionError {
                    message: "the plan rendered fewer ops than it holds; this is a ferro bug, \
                              please file an issue"
                        .into(),
                })
            })
        })
        .collect()
}

/// `planned` as its revision op: its rendering (`None` for a demanding
/// column add), how it is written against the side `plan` leads to, and its
/// marker (`destructive` only going `up`).
fn written(
    planned: &PlannedOp,
    rendered: Option<RenderedOp>,
    plan: &Plan,
    up: bool,
) -> Result<RevisionOp, RevisionRefusal> {
    let target = plan.target.ir();
    let demanding = rendered.is_none();
    let (statements, row_security_statements) = match rendered {
        Some(rendered) => (rendered.statements, rendered.row_security_statements),
        None => (Vec::new(), Vec::new()),
    };
    let foreign_key = match &planned.op {
        MigrationOp::AddForeignKey { table, column } => Some(
            plan.target
                .model(table)
                .and_then(|model| model.foreign_keys.iter().find(|fk| &fk.column == column))
                .map(RevisionForeignKey::from)
                .ok_or_else(|| {
                    RevisionRefusal::Render(EmissionError {
                        message: format!(
                            "the plan adds a foreign key on {table}.{column} the target does \
                             not declare; this is a ferro bug, please file an issue"
                        ),
                    })
                })?,
        ),
        // The pass writes a rider into its own statement; a demanding add
        // has none, so the rider is Alembic's op beside the plain add.
        MigrationOp::AddColumn { table, column } if demanding => plan
            .target
            .model(table)
            .and_then(|model| crate::emit::column_riders(model, column).foreign_key)
            .map(RevisionForeignKey::from),
        _ => None,
    };
    let twin = has_twin(&planned.op, &statements, target, plan.dialect());
    let index = match &planned.op {
        MigrationOp::RedefineIndex { table, name } => {
            Some(crate::declared_index(target, table, name).ok_or_else(|| {
                RevisionRefusal::Render(EmissionError {
                    message: format!(
                        "the plan redefines index {name} on {table}, which the target does \
                         not declare; this is a ferro bug, please file an issue"
                    ),
                })
            })?)
        }
        _ => None,
    };
    let what = marker_subject(&planned.op);
    let marker = if planned.verdict.demands_values {
        Some(Marker::DataDependent(format!(
            "data-dependent (fails while {what} has rows; ferro migrations generate the \
             backfill: `ferro migrate new`)"
        )))
    } else if up && planned.verdict.drops_data {
        let what = match op_column(&planned.op) {
            Some(column) => format!("{what}.{column}"),
            None => what,
        };
        Some(Marker::Destructive(format!(
            "destructive (drops {what} and the data it holds)"
        )))
    } else {
        None
    };
    Ok(RevisionOp {
        op: planned.op.clone(),
        statements,
        row_security_statements,
        twin,
        autocommit: planned.op.commits_alone(),
        foreign_key,
        index,
        marker,
        irreversible: None,
    })
}

/// Whether Alembic's own op writes `op` (ADR-0041's list). A column add the
/// pass backfills with a literal default, or that carries SQLite's inline
/// `REFERENCES` / `CHECK` (Alembic cannot add a constraint on SQLite), runs
/// as the pass writes it; a demanding one (no statements) is the plain op.
fn has_twin(
    op: &MigrationOp,
    statements: &[String],
    target: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
) -> bool {
    match op {
        MigrationOp::AddColumn { table, column } => {
            if statements.is_empty() {
                return true;
            }
            let model = target
                .payload
                .models
                .iter()
                .find(|model| &model.table_name == table);
            let has_default = model
                .and_then(|model| model.columns.iter().find(|c| &c.name == column))
                .is_some_and(|c| c.default.as_ref().is_some_and(|d| !d.is_null()));
            let inline = dialect == Dialect::Sqlite
                && model.is_some_and(|model| inline_constraint(model, column));
            !(has_default || inline)
        }
        MigrationOp::AddTable { .. }
        | MigrationOp::DropTable { .. }
        | MigrationOp::RenameTable { .. }
        | MigrationOp::RenameColumn { .. }
        | MigrationOp::DropColumn { .. }
        | MigrationOp::AlterColumnNullability { .. }
        | MigrationOp::AlterColumnType { .. }
        | MigrationOp::AddIndex { .. }
        | MigrationOp::DropIndex { .. }
        | MigrationOp::RedefineIndex { .. }
        | MigrationOp::AddForeignKey { .. }
        | MigrationOp::DropForeignKey { .. } => true,
        MigrationOp::AddEnumLabel { .. }
        | MigrationOp::CreateEnumType { .. }
        | MigrationOp::DropEnumType { .. }
        | MigrationOp::RenameEnumLabel { .. }
        | MigrationOp::RemoveEnumLabel { .. }
        | MigrationOp::RenameEnumType { .. }
        | MigrationOp::RenameIndex { .. }
        | MigrationOp::RenameConstraint { .. }
        | MigrationOp::RenamePolicy { .. }
        | MigrationOp::ChangePrimaryKey { .. }
        | MigrationOp::AddCheck { .. }
        | MigrationOp::RebuildCheck { .. }
        | MigrationOp::DropCheck { .. }
        | MigrationOp::RebuildForeignKey { .. }
        | MigrationOp::ValidateConstraint { .. }
        | MigrationOp::RebuildIndex { .. }
        | MigrationOp::AddRowPolicy { .. }
        | MigrationOp::RebuildRowPolicy { .. }
        | MigrationOp::DropRowPolicy { .. }
        | MigrationOp::EnableRowSecurity { .. }
        | MigrationOp::ForceRowSecurity { .. }
        | MigrationOp::DisableRowSecurity { .. }
        | MigrationOp::NoForceRowSecurity { .. } => false,
    }
}

/// Whether `column` of `model` carries a foreign key or a column check, which
/// SQLite writes inline in its `ADD COLUMN`.
fn inline_constraint(model: &SchemaModel, column: &str) -> bool {
    model.foreign_keys.iter().any(|fk| fk.column == column)
        || model.checks.iter().any(|check| check.column == column)
}

/// A column add that asks existing rows for a value no statement supplies.
fn demands_column_values(planned: &PlannedOp) -> bool {
    matches!(planned.op, MigrationOp::AddColumn { .. }) && planned.verdict.demands_values
}

fn needs_rebuild(kind: &str, subject: &str) -> String {
    format!(
        "{kind} on {subject} needs a SQLite table rebuild, which an Alembic revision cannot write"
    )
}

fn cannot_add_required(kind: &str, subject: &str) -> String {
    format!(
        "{kind} on {subject} adds a NOT NULL column with no value for the rows already there, \
         which SQLite cannot add in place"
    )
}

/// The op's kind as the plan JSON spells it (`AddColumn`, …).
fn kind_name(op: &MigrationOp) -> String {
    serde_json::to_value(op)
        .ok()
        .and_then(|value| value.get("kind")?.as_str().map(str::to_string))
        .unwrap_or_else(|| format!("{op:?}"))
}

/// The enum type an op on one names.
fn type_name(op: &MigrationOp) -> Option<&str> {
    match op {
        MigrationOp::AddEnumLabel { type_name, .. }
        | MigrationOp::CreateEnumType { type_name, .. }
        | MigrationOp::DropEnumType { type_name }
        | MigrationOp::RenameEnumLabel { type_name, .. }
        | MigrationOp::RemoveEnumLabel { type_name, .. } => Some(type_name),
        _ => None,
    }
}

/// The column an op on one names.
fn op_column(op: &MigrationOp) -> Option<&str> {
    match op {
        MigrationOp::AddColumn { column, .. }
        | MigrationOp::DropColumn { column, .. }
        | MigrationOp::AlterColumnType { column, .. }
        | MigrationOp::AlterColumnNullability { column, .. }
        | MigrationOp::AddForeignKey { column, .. }
        | MigrationOp::DropForeignKey { column, .. }
        | MigrationOp::RebuildForeignKey { column, .. } => Some(column),
        _ => None,
    }
}

/// What a marker names: the op's table, or its enum type.
fn marker_subject(op: &MigrationOp) -> String {
    match op {
        MigrationOp::RenameTable { .. } | MigrationOp::RenameEnumType { .. } => String::new(),
        _ => op
            .table()
            .or_else(|| type_name(op))
            .unwrap_or_default()
            .to_string(),
    }
}

/// What a refusal names: the table (by its new name for a rename), or the
/// enum type, and the column.
fn subject(op: &MigrationOp) -> String {
    let named = match op {
        MigrationOp::RenameEnumType { new, .. } => new.as_str(),
        _ => op.table().or_else(|| type_name(op)).unwrap_or_default(),
    };
    match op_column(op) {
        Some(column) => format!("{named}.{column}"),
        None => named.to_string(),
    }
}

#[cfg(test)]
#[path = "revision_tests.rs"]
mod tests;
