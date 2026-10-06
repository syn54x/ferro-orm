//! Schema IR diffing and SQL emission for migration planning.
//!
//! There is **one planner** ([`plan_from_ir`]): it compares two
//! [`ferro_schema_ir::SchemaIrPayload`] snapshots — declared against declared,
//! or declared against the live database read into an IR plus its
//! [`LiveFacts`] — and decides every change for the whole modelset as one
//! ordered [`MigrationPlan`]. [`render_plan`] lowers each op to executable,
//! dialect-specific DDL through the `ferro_ddl_lowering` functions every
//! migration door shares (AGENTS.md § I-1).
//!
//! The in-house migration system's offline half lives beside it: the schema
//! snapshot ([`snapshot`]), the migrations-directory reader ([`directory`]) and
//! the generator ([`generate`]), which renders the planner's ops into a
//! migration's step files.

pub mod directory;
mod emit;
pub mod generate;
mod order;
mod plan;
mod render;
pub mod snapshot;

pub use directory::{
    DirectoryError, Headers, Migration, MigrationsDir, Step, StepDialect, StepKind,
};
pub use emit::{CreateTableEmission, order_models_for_create, render_create_table};
pub use ferro_ddl_lowering::Dialect;
pub use generate::{CheckReport, GenerateError, GeneratedMigration, check_migrations, generate};
pub use order::order_by_dependencies;
pub use plan::{
    LiveCheckFact, LiveFacts, LiveTableFacts, plan_check_drops, plan_check_rebuilds, plan_from_ir,
    plan_index_rebuilds, plan_missing_checks, plan_validations,
};
pub use render::{RenderedOp, render_plan, validate_schema_ir};
pub use snapshot::{Snapshot, SnapshotError};

/// Executable SQL plus non-fatal warnings for one rendered op.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct EmissionResult {
    /// DDL statements to execute in order.
    pub statements: Vec<String>,
    /// Human-readable warnings (backend limitations, skipped alters, …).
    pub warnings: Vec<String>,
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
    /// A declared FK whose column exists live but has no FK constraint at all.
    /// (FKs on newly added columns ride the `AddColumn` emission instead.)
    AddForeignKey {
        /// Owning table.
        table: String,
        /// Local FK column.
        column: String,
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
    /// The table this op changes, or `None` for an op on an enum type.
    pub fn table(&self) -> Option<&str> {
        match self {
            MigrationOp::AddEnumLabel { .. }
            | MigrationOp::CreateEnumType { .. }
            | MigrationOp::DropEnumType { .. } => None,
            MigrationOp::AddTable { table }
            | MigrationOp::DropTable { table }
            | MigrationOp::AddColumn { table, .. }
            | MigrationOp::DropColumn { table, .. }
            | MigrationOp::AlterColumnType { table, .. }
            | MigrationOp::AlterColumnNullability { table, .. }
            | MigrationOp::AddIndex { table, .. }
            | MigrationOp::DropIndex { table, .. }
            | MigrationOp::AddForeignKey { table, .. }
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
/// always `true` on SQLite). The thin live-state slice [`plan_validations`]
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

/// The whole modelset's ordered operations plus the warnings planning raised.
#[derive(Clone, Debug, Default, PartialEq, Eq, serde::Serialize)]
pub struct MigrationPlan {
    /// Operations to apply, in execution order (see [`plan_from_ir`]).
    pub operations: Vec<MigrationOp>,
    /// Advisory warnings planning raised (a user-owned FK that drifts,
    /// leftover CHECKs, extra enum labels). Warnings an op raises while
    /// rendering travel on its [`RenderedOp`] instead.
    pub warnings: Vec<String>,
    /// Warnings about a standing condition of the live database that holds
    /// on every run until someone acts — every row-security report (a
    /// foreign or unverifiable policy, a dropped declaration, a teardown).
    /// Callers surface these every time: a table whose rows are not fenced
    /// the way the model says is still not fenced on the next run, and
    /// silence after the first would be misread as safety.
    pub always_warnings: Vec<String>,
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

impl MigrationPlan {
    /// Returns `true` when there are no operations to run.
    pub fn is_empty(&self) -> bool {
        self.operations.is_empty()
    }
}

#[cfg(test)]
mod tests;
