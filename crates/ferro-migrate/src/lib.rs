//! Schema IR diffing and SQL emission for migration planning.
//!
//! There is **one planner** ([`plan_from_ir`]): it compares two
//! [`ferro_schema_ir::SchemaIrPayload`] snapshots — declared against declared,
//! or declared against the live database read into an IR plus its
//! [`LiveFacts`] — and decides every change for the whole modelset as one
//! ordered [`Plan`], which holds the two sides it was planned between.
//! [`Plan::render`] lowers each op to executable, dialect-specific DDL through
//! the `ferro_ddl_lowering` functions every migration door shares (AGENTS.md
//! § I-1).
//!
//! The in-house migration system's offline half lives beside it: the schema
//! snapshot ([`snapshot`]), the migrations-directory reader ([`directory`]) and
//! the generator ([`generate`]), which renders the planner's ops into a
//! migration's step files.

pub mod directory;
mod emit;
pub mod generate;
mod order;
pub mod plan;
mod render;
pub mod run_plan;
pub mod snapshot;

pub use directory::{
    DirectoryError, Headers, Migration, MigrationsDir, Step, StepDialect, StepKind,
};
pub use emit::{CreateTableEmission, order_models_for_create, render_create_table};
pub use ferro_ddl_lowering::{Dialect, InPlaceChange, Report, ReportKind, Subject};
pub use generate::{
    CheckReport, GenerateError, GenerateOptions, GeneratedMigration, check_migrations, generate,
    generate_with,
};
pub use order::order_by_dependencies;
pub use plan::{
    Execution, Hint, HintError, LiveCheckFact, LiveFacts, LiveTableFacts, OpVerdict, PlanError,
    PlannedOp, Refusal, Rider, RowRisk, Side, live_hints, plan_down, plan_from_ir,
};
pub use render::{RenderedOp, validate_schema_ir};
pub use run_plan::{
    Direction, ExecMode, HeldDirectory, Origin, PlannedStep, RebuildExpectation, RecordKind,
    RunPlan, RunRefusal, RunStatus, StepRecord, Target, plan_run,
};
pub use snapshot::{Snapshot, SnapshotError};

/// The columns and uniqueness of the standalone index `name` that `ir`
/// declares on `table` (an `indexes` entry, a `uniques` entry or a column
/// flag), as every door builds it; `None` when it declares none.
pub fn declared_index(
    ir: &ferro_schema_ir::IrEnvelope<ferro_schema_ir::SchemaIrPayload>,
    table: &str,
    name: &str,
) -> Option<(Vec<String>, bool)> {
    let model = ir
        .payload
        .models
        .iter()
        .find(|model| model.table_name == table)?;
    emit::standalone_indexes(model)
        .into_iter()
        .find(|(index, _, _)| index == name)
        .map(|(_, columns, unique)| (columns, unique))
}

/// Executable SQL plus the reports for one rendered op.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct EmissionResult {
    /// DDL statements to execute in order.
    pub statements: Vec<String>,
    /// What rendering reports (a backend limitation, a refused cast, …).
    pub reports: Vec<Report>,
}

/// Hard failure during SQL emission (missing IR metadata, unsafe add, …).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EmissionError {
    /// Actionable error message.
    pub message: String,
}

impl std::fmt::Display for EmissionError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.message)
    }
}

impl std::error::Error for EmissionError {}

/// One change the planner decided. Serializes with its variant name as
/// `kind` beside its fields (`{"kind": "AddColumn", "table": …, "column": …}`),
/// the shape the `_core._plan_from_ir` FFI door returns.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(tag = "kind")]
pub enum MigrationOp {
    /// A label the model declares that a native Postgres enum type lacks
    /// (ADR-0011) — `ALTER TYPE … ADD VALUE IF NOT EXISTS`. Planned before
    /// every table op, so a column default naming the label can use it.
    AddEnumLabel {
        /// Enum type name.
        type_name: String,
        /// Label to append.
        label: String,
    },
    /// A native Postgres enum type the plan introduces (every column
    /// declaring it is one the plan adds) and that does not exist yet
    /// (ADR-0021, ADR-0022) — the guarded `CREATE TYPE`, ahead of every table
    /// op. An `AddTable` / `AddColumn` of the type then renders no guard.
    CreateEnumType {
        /// Enum type name.
        type_name: String,
        /// Labels in declared order.
        labels: Vec<String>,
    },
    /// A native Postgres enum type every one of whose declaring columns the
    /// plan removes, and that no surviving column declares (ADR-0020) —
    /// `DROP TYPE`, after every table op. Destructive only.
    DropEnumType {
        /// Enum type name.
        type_name: String,
    },
    /// A live label rename hint (`__ferro_renamed_labels__`, ADR-0032) —
    /// `ALTER TYPE … RENAME VALUE` on Postgres; on SQLite, where labels are
    /// text in the rows, an `UPDATE` of every column of the type. Planned only
    /// from declared hints, never inferred.
    RenameEnumLabel {
        /// Enum type name, as any type rename of the same plan leaves it.
        type_name: String,
        /// The label's old spelling.
        old: String,
        /// Its new spelling.
        new: String,
        /// Every `(table, column)` of the type whose rows hold the label, by
        /// the names the plan's table and column renames leave them.
        columns: Vec<(String, String)>,
    },
    /// A label the old side's enum declares and the new one drops, with no
    /// hint renaming it (#536). Planned only from a declared old side: from a
    /// live database a dropped label is ADR-0011's warn-never-act. Rows may
    /// hold the label, so the generator answers it with a backfill and, on
    /// Postgres, the swap-type contract (`generate::enums::render_swap_type`);
    /// toward a live database (a down planned back to it) it is
    /// irreversible: labels are append-only there.
    RemoveEnumLabel {
        /// Enum type name.
        type_name: String,
        /// The label dropped.
        label: String,
        /// Every `(table, column)` of the new snapshot declaring the type.
        columns: Vec<(String, String)>,
    },
    /// An enum type every one of whose columns now declares one and the same
    /// new type (ADR-0032: inferred from the columns, no hint) — `ALTER TYPE
    /// … RENAME TO`. Postgres only: SQLite has no enum types.
    RenameEnumType {
        /// The type's old name.
        old: String,
        /// Its new name.
        new: String,
    },
    /// A model exists in the new IR but not the old.
    AddTable {
        /// Table to create.
        table: String,
    },
    /// A model was removed — emits `DROP TABLE`.
    DropTable {
        /// Table to drop.
        table: String,
    },
    /// A live rename hint renamed a table (ADR-0032) — `ALTER TABLE … RENAME
    /// TO`, native on both dialects. Planned only from declared hints
    /// ([`plan::live_hints`], applied by [`plan_from_ir`]), never inferred.
    RenameTable {
        /// The table's name in the old snapshot.
        old: String,
        /// The table's name in the new one.
        new: String,
    },
    /// A live rename hint renamed a column (ADR-0032) — `ALTER TABLE … RENAME
    /// COLUMN`, native on both dialects.
    RenameColumn {
        /// Owning table, by the name it has once any table rename ran.
        table: String,
        /// The column's old name.
        old: String,
        /// The column's new name.
        new: String,
    },
    /// An `idx_` / `uq_` name a rename drags (ADR-0032) — `ALTER INDEX …
    /// RENAME TO` on Postgres; on SQLite, which has no index rename, `DROP
    /// INDEX` then the `CREATE INDEX` under the new name.
    RenameIndex {
        /// Owning table, by the name it has once any table rename ran. An
        /// index name is schema-wide, but the op rides its table's unit: its
        /// transaction in the reconciliation pass, its SQLite rebuild in a
        /// generated step.
        table: String,
        /// The index's old name.
        old: String,
        /// The index's new name.
        new: String,
    },
    /// A `ck_` / `fk_` name a rename drags (ADR-0032) — `ALTER TABLE … RENAME
    /// CONSTRAINT` on Postgres; a table rebuild on SQLite (ADR-0046).
    RenameConstraint {
        /// Owning table, by the name it has once any table rename ran.
        table: String,
        /// The constraint's old name.
        old: String,
        /// The constraint's new name.
        new: String,
    },
    /// An `rls_` name a table rename drags (ADR-0032) — `ALTER POLICY … RENAME
    /// TO`. Postgres only, like every row-security op (ADR-0014).
    RenamePolicy {
        /// Owning table, by the name it has once any table rename ran.
        table: String,
        /// The policy's old name.
        old: String,
        /// The policy's new name.
        new: String,
    },
    /// A column exists on the model in the new IR but not in the live/old IR.
    AddColumn {
        /// Owning table.
        table: String,
        /// Column to add.
        column: String,
    },
    /// A column was removed from the model.
    DropColumn {
        /// Owning table.
        table: String,
        /// Column to drop.
        column: String,
    },
    /// `db_type` changed for a column that exists in both snapshots.
    AlterColumnType {
        /// Owning table.
        table: String,
        /// Column whose storage type drifted.
        column: String,
    },
    /// `nullable` changed for a column that exists in both snapshots.
    AlterColumnNullability {
        /// Owning table.
        table: String,
        /// Column whose nullability drifted.
        column: String,
    },
    /// The table's primary-key columns differ between the snapshots (a key
    /// moved between columns, gained or lost one). No migration door changes
    /// a primary key in place: the pass warns and skips, the generator
    /// refuses with the recipe.
    ChangePrimaryKey {
        /// Owning table.
        table: String,
        /// The old snapshot's primary-key columns, in column order.
        from: Vec<String>,
        /// The new snapshot's primary-key columns, in column order.
        to: Vec<String>,
    },
    /// A standalone Ferro-named index/unique present in the model but not live.
    AddIndex {
        /// Owning table.
        table: String,
        /// Index name.
        name: String,
        /// Indexed columns.
        columns: Vec<String>,
        /// Whether this is a unique index.
        unique: bool,
    },
    /// A standalone Ferro-named index present live but gone from the model.
    DropIndex {
        /// Owning table.
        table: String,
        /// Index name.
        name: String,
    },
    /// A ferro-owned index that keeps its name and changes its columns or its
    /// uniqueness (ADR-0051): two declarations can build one name (a name cut
    /// to 63 characters, or an underscore join), and a live database can hold
    /// a ferro-named index another way. `DROP INDEX`, then the declared
    /// `CREATE [UNIQUE] INDEX`, read from the plan's target. Planned under
    /// `migrate_updates`: nothing a row holds is discarded; a unique
    /// redefinition fails on duplicate values.
    RedefineIndex {
        /// Owning table.
        table: String,
        /// Index name.
        name: String,
    },
    /// A declared FK whose column exists live but has no FK constraint at all.
    /// (FKs on newly added columns ride the `AddColumn` emission instead.)
    AddForeignKey {
        /// Owning table.
        table: String,
        /// Local FK column.
        column: String,
    },
    /// A live ferro-owned FK on a column both sides keep that the model no
    /// longer declares (ADR-0051) — `ALTER TABLE … DROP CONSTRAINT` on
    /// Postgres; a table rebuild on SQLite. Planned only under
    /// `migrate_destructive` (ADR-0013's ladder), though no row is lost; the
    /// generator always writes it. An FK on a dropped column goes with it.
    DropForeignKey {
        /// Owning table.
        table: String,
        /// Local FK column.
        column: String,
        /// Live constraint name (`fk_<table>_<col>_<to_table>`).
        name: String,
    },
    /// A declared CHECK constraint — table check or column check — with no live
    /// constraint of that name (#343). Looked up by `name` in the declared
    /// model's `table_checks`, then its `checks`.
    AddCheck {
        /// Owning table.
        table: String,
        /// Canonical constraint name (`ck_<table>_<suffix>`).
        name: String,
    },
    /// A live ferro-owned CHECK whose body drifted from the declared
    /// predicate on the same name — rebuilt as `DROP CONSTRAINT` + a
    /// bare `ADD CONSTRAINT … CHECK` where the backend allows it
    /// (#344; ADR-0015). Looked up by `name` in the declared model's
    /// `table_checks`, then its `checks`.
    RebuildCheck {
        /// Owning table.
        table: String,
        /// Canonical constraint name (`ck_<table>_<suffix>`).
        name: String,
    },
    /// A live ferro-owned CHECK the model no longer declares (#345;
    /// ADR-0013). Planned only under `migrate_destructive`; Alembic
    /// autogenerate always proposes the drop. The name must *not* still
    /// be declared — emitting a drop for a declared name is a loud error.
    DropCheck {
        /// Owning table.
        table: String,
        /// Live constraint name (`ck_<table>_<suffix>`).
        name: String,
    },
    /// A live ferro-owned FK whose definition (`on_delete`, target) drifted
    /// from the declared FK on the same column — rebuilt as
    /// `DROP CONSTRAINT` + `ADD CONSTRAINT` where the backend allows it.
    RebuildForeignKey {
        /// Owning table.
        table: String,
        /// Local FK column.
        column: String,
        /// Name of the live constraint to drop.
        old_name: String,
    },
    /// A declared FK or CHECK that exists live but is `NOT VALID`
    /// (`pg_constraint.convalidated = false`; ADR-0043) — validated in place
    /// with `ALTER TABLE … VALIDATE CONSTRAINT`, never dropped and re-added.
    /// Postgres-only: SQLite has no unvalidated constraints.
    ValidateConstraint {
        /// Owning table.
        table: String,
        /// Constraint name (`fk_*` / `ck_*`).
        name: String,
    },
    /// A declared index or unique that exists live but is invalid
    /// (`pg_index.indisvalid = false`, the leftover of a failed concurrent
    /// build; ADR-0044) — rebuilt as `DROP INDEX` then the same
    /// `CREATE [UNIQUE] INDEX` the [`MigrationOp::AddIndex`] path renders.
    RebuildIndex {
        /// Owning table.
        table: String,
        /// Index name.
        name: String,
        /// Indexed columns.
        columns: Vec<String>,
        /// Whether this is a unique index.
        unique: bool,
    },
    /// A declared row policy with no live policy of that name (#413) —
    /// `CREATE POLICY`. Postgres-only, like every row-security op (ADR-0014).
    AddRowPolicy {
        /// Owning table.
        table: String,
        /// Live policy name (`rls_<table>_<name>`).
        name: String,
    },
    /// A live ferro-owned policy whose definition drifted from its
    /// declaration (ADR-0019) — `DROP POLICY` + `CREATE POLICY`.
    RebuildRowPolicy {
        /// Owning table.
        table: String,
        /// Policy name.
        name: String,
    },
    /// A live ferro-owned policy the model no longer declares — `DROP
    /// POLICY`. Destructive only.
    DropRowPolicy {
        /// Owning table.
        table: String,
        /// Live policy name.
        name: String,
    },
    /// `ENABLE ROW LEVEL SECURITY` on a table that declares row security.
    EnableRowSecurity {
        /// Owning table.
        table: String,
    },
    /// `FORCE ROW LEVEL SECURITY` on a table that declares `force=True`.
    ForceRowSecurity {
        /// Owning table.
        table: String,
    },
    /// `DISABLE ROW LEVEL SECURITY` on a table whose model dropped its
    /// declaration while ferro still manages it. Destructive only.
    DisableRowSecurity {
        /// Owning table.
        table: String,
    },
    /// `NO FORCE ROW LEVEL SECURITY` on a table whose model no longer asks
    /// for `force`. Destructive only.
    NoForceRowSecurity {
        /// Owning table.
        table: String,
    },
}

impl MigrationOp {
    /// The table this op changes, or `None` for an op on an enum type. A
    /// [`MigrationOp::RenameTable`] changes the table by its new name.
    pub fn table(&self) -> Option<&str> {
        match self {
            MigrationOp::AddEnumLabel { .. }
            | MigrationOp::CreateEnumType { .. }
            | MigrationOp::DropEnumType { .. }
            | MigrationOp::RenameEnumLabel { .. }
            | MigrationOp::RemoveEnumLabel { .. }
            | MigrationOp::RenameEnumType { .. } => None,
            MigrationOp::RenameTable { new, .. } => Some(new),
            MigrationOp::RenameColumn { table, .. }
            | MigrationOp::RenameIndex { table, .. }
            | MigrationOp::RenameConstraint { table, .. }
            | MigrationOp::RenamePolicy { table, .. } => Some(table),
            MigrationOp::AddTable { table }
            | MigrationOp::DropTable { table }
            | MigrationOp::AddColumn { table, .. }
            | MigrationOp::DropColumn { table, .. }
            | MigrationOp::AlterColumnType { table, .. }
            | MigrationOp::AlterColumnNullability { table, .. }
            | MigrationOp::ChangePrimaryKey { table, .. }
            | MigrationOp::AddIndex { table, .. }
            | MigrationOp::DropIndex { table, .. }
            | MigrationOp::RedefineIndex { table, .. }
            | MigrationOp::AddForeignKey { table, .. }
            | MigrationOp::DropForeignKey { table, .. }
            | MigrationOp::AddCheck { table, .. }
            | MigrationOp::RebuildCheck { table, .. }
            | MigrationOp::DropCheck { table, .. }
            | MigrationOp::RebuildForeignKey { table, .. }
            | MigrationOp::ValidateConstraint { table, .. }
            | MigrationOp::RebuildIndex { table, .. }
            | MigrationOp::AddRowPolicy { table, .. }
            | MigrationOp::RebuildRowPolicy { table, .. }
            | MigrationOp::DropRowPolicy { table, .. }
            | MigrationOp::EnableRowSecurity { table }
            | MigrationOp::ForceRowSecurity { table }
            | MigrationOp::DisableRowSecurity { table }
            | MigrationOp::NoForceRowSecurity { table } => Some(table),
        }
    }
}

/// Whether one live FK constraint is validated (`pg_constraint.convalidated`;
/// always `true` on SQLite). The thin live-state slice [`plan_from_ir`]
/// reads — introspection types stay outside this crate.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct LiveFkValidity {
    /// Live constraint name.
    pub name: String,
    /// `false` when the constraint exists `NOT VALID`.
    pub validated: bool,
}

/// Whether one live CHECK constraint is validated
/// (`pg_constraint.convalidated`; always `true` on SQLite).
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct LiveCheckValidity {
    /// Live constraint name.
    pub name: String,
    /// `false` when the constraint exists `NOT VALID`.
    pub validated: bool,
}

/// Whether one live index is valid (`pg_index.indisvalid`; always `true` on
/// SQLite).
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct LiveIndexValidity {
    /// Live index name.
    pub name: String,
    /// `false` when the index exists but is invalid.
    pub valid: bool,
}

/// The whole modelset's ordered operations plus the reports planning raised,
/// holding the sides they were decided between: the planned-before side
/// (the old side as the plan's renames leave it, which every op but the
/// renames names its tables and columns by), the target and the dialect. An
/// op is name-only, so it means something only against those sides; the plan
/// renders itself ([`Plan::render`]), reading every table, column and body
/// from them, and no caller supplies them again.
#[derive(Clone, Debug, PartialEq)]
pub struct Plan {
    /// Operations to apply, in execution order (see [`plan_from_ir`]), each
    /// with its verdict, computed once between the plan's sides.
    pub operations: Vec<PlannedOp>,
    /// What planning reports beside its ops (a refused rename hint, leftover
    /// CHECKs, extra enum labels, a user-owned FK that drifts, every
    /// row-security report), in the order planning raised them. A report
    /// about a standing condition of the live database [`Report::recurs`]:
    /// callers surface it every time, since a table whose rows are not
    /// fenced the way the model says is still not fenced on the next run.
    /// Reports an op raises while rendering travel on its [`RenderedOp`].
    pub reports: Vec<Report>,
    /// The old side as the plan's renames leave it.
    pub(crate) before: Side,
    /// The side the plan leads to.
    pub(crate) target: Side,
    /// The dialect it was planned for.
    dialect: Dialect,
}

/// Whether a plan's own ops answer a [`Report`]: the change it reports is
/// one they make. A door that writes a reviewed file refuses every report no
/// op answers, since writing nothing for it would leave it out silently.
pub trait AnsweredBy {
    /// `true` when `ops` make the change this report is about.
    fn answered_by(&self, ops: &[MigrationOp]) -> bool;
}

impl AnsweredBy for Report {
    fn answered_by(&self, ops: &[MigrationOp]) -> bool {
        let on_table = |table: &str| match &self.subject {
            Subject::Table { table: subject } | Subject::Column { table: subject, .. } => {
                subject == table
            }
            _ => false,
        };
        let has = |pred: &dyn Fn(&MigrationOp) -> bool| ops.iter().any(pred);
        match &self.kind {
            // Each leftover is dropped by the plan.
            ReportKind::LeftoverChecks { names } => names.iter().all(|name| {
                has(&|op| {
                    matches!(op, MigrationOp::DropCheck { table, name: dropped }
                        if on_table(table) && dropped == name)
                })
            }),
            ReportKind::ExtraPolicies { names } => names.iter().all(|name| {
                has(&|op| {
                    matches!(op, MigrationOp::DropRowPolicy { table, name: dropped }
                        if on_table(table) && dropped == name)
                })
            }),
            ReportKind::ExtraEnumLabels { labels } => labels.iter().all(|label| {
                has(&|op| {
                    matches!((op, &self.subject), (
                        MigrationOp::RemoveEnumLabel { type_name, label: removed, .. },
                        Subject::EnumType { type_name: subject },
                    ) if type_name == subject && removed == label)
                })
            }),
            // Row security the model stopped asking for, torn down.
            ReportKind::DroppedRowSecurity => has(&|op| {
                matches!(op, MigrationOp::DisableRowSecurity { table }
                    | MigrationOp::NoForceRowSecurity { table } if on_table(table))
            }),
            // The report of a change the plan makes: its ops.
            ReportKind::RowSecurityTeardown { names } => {
                names.iter().all(|name| {
                    has(&|op| {
                        matches!(op, MigrationOp::DropRowPolicy { table, name: dropped }
                            if on_table(table) && dropped == name)
                    })
                }) && (!names.is_empty()
                    || has(&|op| {
                        matches!(op, MigrationOp::DisableRowSecurity { table }
                            | MigrationOp::NoForceRowSecurity { table } if on_table(table))
                    }))
            }
            ReportKind::PolicyBodyReplaced { name } => has(&|op| {
                matches!(op, MigrationOp::RebuildRowPolicy { table, name: rebuilt }
                    if on_table(table) && rebuilt == name)
            }),
            // A condition no op changes: a refused hint renames nothing, a
            // foreign artifact is never touched, an unverifiable body is left
            // as it is, and a rendering report stands in for a statement.
            ReportKind::HintRefused(_)
            | ReportKind::ForeignFkDrift { .. }
            | ReportKind::ForeignPolicies { .. }
            | ReportKind::UnverifiablePolicy { .. }
            | ReportKind::RefusedConversion
            | ReportKind::SqliteInPlace { .. }
            | ReportKind::PrimaryKeyKept
            | ReportKind::RowSecuritySkipped
            | ReportKind::PendingTableRename
            | ReportKind::StrandedLabelRename
            | ReportKind::RowSecurityUnderMigrator
            | ReportKind::RunLockWait
            | ReportKind::DdlLockRetry => false,
        }
    }
}

/// What a plan may do beyond bringing the database up to the model.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct PlanOptions {
    /// Plan the ops that remove something the model no longer declares:
    /// dropped tables, columns, indexes, checks, row policies and enum
    /// types, and the row-security teardown (ADR-0013's ladder). Without it
    /// they are left in place and reported.
    #[serde(default)]
    pub destructive: bool,
}

impl Plan {
    /// The plan `draft` decides between `before` (already the planned-before
    /// side) and `target` on `dialect`, each op with its verdict.
    pub(crate) fn decided(
        before: Side,
        target: Side,
        dialect: Dialect,
        draft: plan::Draft,
    ) -> Self {
        let operations = draft
            .operations
            .into_iter()
            .map(|op| PlannedOp::of(op, &before, &target, dialect))
            .collect();
        Self {
            operations,
            reports: draft.reports,
            before,
            target,
            dialect,
        }
    }

    /// `operations` as a plan between two empty modelsets on Postgres: a
    /// test's hand-built op list, for what reads only the ops.
    #[cfg(test)]
    pub(crate) fn unplaced(operations: Vec<MigrationOp>) -> Self {
        let empty = Side::declared(ferro_schema_ir::IrEnvelope {
            ir_kind: "schema".into(),
            ir_version: 1,
            payload: ferro_schema_ir::SchemaIrPayload {
                dialect_agnostic: true,
                models: Vec::new(),
            },
        });
        Self::decided(
            empty.clone(),
            empty,
            Dialect::Postgres,
            plan::Draft {
                operations,
                reports: Vec::new(),
            },
        )
    }

    /// Returns `true` when there are no operations to run.
    pub fn is_empty(&self) -> bool {
        self.operations.is_empty()
    }

    /// The ops, in execution order, without their verdicts.
    pub fn ops(&self) -> impl Iterator<Item = &MigrationOp> {
        self.operations.iter().map(|planned| &planned.op)
    }

    /// The ops as a list: what a test compares.
    #[cfg(test)]
    pub(crate) fn op_list(&self) -> Vec<MigrationOp> {
        self.ops().cloned().collect()
    }

    /// The dialect the plan was decided for.
    pub fn dialect(&self) -> Dialect {
        self.dialect
    }

    /// Every op rendered for the plan's dialect, in plan order: what every
    /// migration door executes for it (AGENTS.md § I-1). A native enum type a
    /// `CreateEnumType` op of this plan creates is created there and only
    /// there; an `AddTable` / `AddColumn` of any other native enum type keeps
    /// its idempotent guard.
    ///
    /// # Errors
    /// An [`EmissionError`] when a side carries a declaration that cannot
    /// render ([`validate_schema_ir`]), when an op cannot be applied safely
    /// (adding a NOT NULL column with no backfill, dropping a primary-key
    /// column), when a side lacks what an op names, or when the op cannot
    /// exist on the dialect.
    pub fn render(&self) -> Result<Vec<RenderedOp>, EmissionError> {
        let all: Vec<usize> = (0..self.operations.len()).collect();
        self.render_ops(&all)
    }

    /// The ops at `ops` (indexes into [`Self::operations`], in the order
    /// given) rendered as [`Self::render`] renders them, as if they were the
    /// whole plan: a door that writes some ops its own way (the Alembic
    /// bridge's demanding column adds) renders the rest.
    ///
    /// # Errors
    /// What [`Self::render`] raises, and an index past the plan's ops.
    pub fn render_ops(&self, ops: &[usize]) -> Result<Vec<RenderedOp>, EmissionError> {
        self.render_in(ferro_ddl_lowering::ConstraintMode::Plain, ops)
    }

    /// [`Self::render_ops`] with every foreign key and check added in
    /// `constraints` mode: `NOT VALID` is the generator's staged constraint
    /// on an existing Postgres table (ADR-0043).
    pub(crate) fn render_in(
        &self,
        constraints: ferro_ddl_lowering::ConstraintMode,
        ops: &[usize],
    ) -> Result<Vec<RenderedOp>, EmissionError> {
        let selected = ops
            .iter()
            .map(|&index| {
                self.operations
                    .get(index)
                    .map(|planned| planned.op.clone())
                    .ok_or_else(|| EmissionError {
                        message: format!(
                            "op {index} is past the plan's {} ops",
                            self.operations.len()
                        ),
                    })
            })
            .collect::<Result<Vec<_>, _>>()?;
        render::render_from(
            &selected,
            &self.before,
            &self.target,
            self.dialect,
            constraints,
        )
    }
}

#[cfg(test)]
mod tests;
