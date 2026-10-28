//! A step's class (ADR-0054): whether the previous release's writes keep
//! succeeding beside the step (`expand`) or not (`contract`). It is a
//! **write** notion: an added enum label is `expand`, though the previous
//! release's reads have never met it.
//!
//! ```text
//! class Author(Model):                         0002_author_bio/
//!     bio: str | None = None        ──▶  01_schema   ADD COLUMN "bio"            expand
//!     # name: str (dropped)         ──▶  01_schema   DROP COLUMN "name"          contract
//!     email: str = Field(unique=True) ─▶ 02_uq_…     CREATE UNIQUE INDEX …       contract
//! ```
//!
//! Decided once, here, by [`step_class`]: the generator hands it each step
//! it lays out ([`LaidOutStep`]: the step's phase, the planned ops it
//! carries with their verdicts (ADR-0050), the schemas on either side of
//! it, its rendering's headers), and the run and the baseline record what
//! the generator decides, regenerating a migration from its two snapshots
//! ([`migration_classes`]) when they record it. A hand-written SQL step is
//! `contract` unless its file declares `-- ferro: expand`; a data step is
//! always `expand`.
//!
//! | The step carries | Class |
//! | :-- | :-- |
//! | an op that drops data, demands values of existing rows or can fail on them | contract |
//! | a rename of a column, a table, an enum type or a label | contract |
//! | a type change | contract |
//! | a constraint added `NOT VALID`, or validated (the validate, add-constraint and contract steps) | contract |
//! | a unique index built or redefined | contract |
//! | a row-security change that can refuse a write (a policy, `ENABLE` / `FORCE`) | contract |
//! | an added table or nullable column, `DROP NOT NULL`, a plain index, a dropped index, check, foreign key or unique, an added label | expand |
//! | a data step, a guard step | expand |
//! | a SQLite rebuild carrying only expand ops | expand |
//! | a step whose rendering on the dialect runs nothing (`not-applicable`) | expand |

use crate::directory::{Headers, Migration, MigrationsDir, Step, StepDialect, StepKind};
use crate::generate::backfill::Demand;
use crate::generate::staging::stages_not_valid;
use crate::generate::{GenerateError, GenerateOptions, GeneratedStep, generate_with};
use crate::run_plan::RunRefusal;
use crate::snapshot::sha384;
use crate::{Dialect, MigrationOp, PlannedOp, RowRisk, declared_index};
use ferro_schema_ir::{IrEnvelope, SchemaIrPayload};
use std::collections::BTreeMap;

/// Whether the previous release's writes keep succeeding beside a step.
/// Ordered so that the class of several things is their maximum.
#[derive(
    Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, serde::Serialize, serde::Deserialize,
)]
#[serde(rename_all = "lowercase")]
pub enum StepClass {
    /// The previous release's writes keep succeeding beside the step.
    Expand,
    /// A write the previous release makes can fail beside the step.
    Contract,
}

impl StepClass {
    /// The spelling stored in the `class` column and printed by `status`.
    pub fn as_str(self) -> &'static str {
        match self {
            StepClass::Expand => "expand",
            StepClass::Contract => "contract",
        }
    }

    /// The class the `class` column spells.
    pub fn parse(text: &str) -> Option<Self> {
        match text {
            "expand" => Some(StepClass::Expand),
            "contract" => Some(StepClass::Contract),
            _ => None,
        }
    }
}

/// The two schemas a step leads between: what its ops read besides their
/// verdicts (whether a redefined index is unique, whether a rebuilt column
/// only gained labels, whether a constraint is added to a table that
/// already exists).
#[derive(Clone, Copy, Debug)]
pub(crate) struct Sides<'a> {
    /// The schema the step starts from.
    pub before: &'a IrEnvelope<SchemaIrPayload>,
    /// The schema it leads to.
    pub target: &'a IrEnvelope<SchemaIrPayload>,
}

/// A migration's `schema` (or `expand`) step on one dialect.
#[derive(Clone, Copy, Debug)]
pub(crate) struct SchemaStep<'a> {
    /// The planned ops it carries, a demanded column's add among them,
    /// which the step renders nullable.
    pub ops: &'a [PlannedOp],
    /// The schemas it leads between.
    pub sides: Sides<'a>,
    /// The dialect it renders for: on Postgres it adds a constraint to a
    /// table that already exists `NOT VALID` (ADR-0043).
    pub dialect: Dialect,
}

/// A step as the generator lays it out on one dialect: what its class is
/// read from.
#[derive(Clone, Copy, Debug)]
pub(crate) enum LaidOutStep<'a> {
    /// The `labels` step: each label added to an existing enum type.
    Labels {
        /// The planned ops it carries.
        ops: &'a [PlannedOp],
        /// The schemas it leads between.
        sides: Sides<'a>,
    },
    /// The `schema` (or `expand`) step.
    Schema(SchemaStep<'a>),
    /// An index step: the one index it builds, drops or redefines
    /// (ADR-0044) toward `target`.
    Index {
        /// The index op.
        op: &'a MigrationOp,
        /// The schema the step leads to, which declares the index it
        /// redefines.
        target: &'a IrEnvelope<SchemaIrPayload>,
    },
    /// The validate or add-constraint step, which only adds constraints
    /// `NOT VALID` or validates them, by its rendering's headers.
    Constraints {
        /// Its up file's headers on the dialect.
        headers: &'a Headers,
    },
    /// The contract: what the migration holds back for its data steps.
    Contract {
        /// The planned ops it carries: the drops held for the data steps,
        /// the removed labels and the column changes they make.
        ops: &'a [PlannedOp],
        /// The schemas it leads between.
        sides: Sides<'a>,
        /// The `schema` (or `expand`) step it finishes, whose constraints
        /// staged `NOT VALID` it validates.
        expand: SchemaStep<'a>,
        /// Every column whose existing rows the data steps fill, which the
        /// contract holds to its values (`NOT NULL`, a removed label gone).
        demands: &'a [Demand],
    },
    /// A data step, a guard step included.
    Data,
    /// A hand-written SQL step, by its file's headers.
    HandSql {
        /// Its up file's headers.
        headers: &'a Headers,
    },
}

/// The class of `step`: `contract` when anything it does can make a write
/// of the previous release fail, `expand` otherwise.
pub(crate) fn step_class(step: &LaidOutStep<'_>) -> StepClass {
    match *step {
        LaidOutStep::Labels { ops, sides } => ops_class(ops, sides),
        LaidOutStep::Schema(schema) => {
            // A column that demands values is added nullable (its backfill
            // and contract come later), so the previous release's writes,
            // which leave it empty, keep succeeding.
            let relaxed: Vec<PlannedOp> = schema
                .ops
                .iter()
                .map(|planned| match planned.op {
                    MigrationOp::AddColumn { .. } if planned.verdict.demands_values => {
                        let mut nullable = planned.clone();
                        nullable.verdict.demands_values = false;
                        nullable.verdict.fails_on_rows = RowRisk::None;
                        nullable
                    }
                    _ => planned.clone(),
                })
                .collect();
            ops_class(&relaxed, schema.sides).max(constraint_class(stages(schema)))
        }
        LaidOutStep::Index { op, target } => kind_class(op, target),
        LaidOutStep::Constraints { headers } => constraint_class(!headers.not_applicable),
        LaidOutStep::Contract {
            ops,
            sides,
            expand,
            demands,
        } => ops_class(ops, sides).max(constraint_class(stages(expand) || !demands.is_empty())),
        LaidOutStep::Data => StepClass::Expand,
        LaidOutStep::HandSql { headers } => {
            if headers.expand {
                StepClass::Expand
            } else {
                StepClass::Contract
            }
        }
    }
}

/// Whether `schema` adds a constraint `NOT VALID`, which a later step
/// validates.
fn stages(schema: SchemaStep<'_>) -> bool {
    schema.ops.iter().any(|planned| {
        stages_not_valid(
            &planned.op,
            schema.sides.before,
            schema.sides.target,
            schema.dialect,
        )
    })
}

/// `contract` when a step adds a constraint `NOT VALID` or validates one:
/// either refuses a write the previous release may make.
fn constraint_class(constrains: bool) -> StepClass {
    if constrains {
        StepClass::Contract
    } else {
        StepClass::Expand
    }
}

/// The class of a step running each of `ops` (natively, or folded into a
/// SQLite rebuild, which runs each of them) between `sides`.
fn ops_class(ops: &[PlannedOp], sides: Sides<'_>) -> StepClass {
    ops.iter()
        .map(|op| op_class(op, sides))
        .fold(StepClass::Expand, StepClass::max)
}

/// The class of one planned op between `sides`, from its verdict and its
/// kind.
fn op_class(planned: &PlannedOp, sides: Sides<'_>) -> StepClass {
    let verdict = &planned.verdict;
    // A label added to a column's enum (on SQLite, where labels are text, a
    // rebuild widening the column; a text column's check widened to admit
    // it) admits every value the previous release writes.
    if adds_labels_only(&planned.op, sides) {
        return StepClass::Expand;
    }
    if verdict.drops_data || verdict.demands_values || verdict.fails_on_rows != RowRisk::None {
        return StepClass::Contract;
    }
    kind_class(&planned.op, sides.target)
}

/// The class `op`'s kind alone decides toward `target`.
fn kind_class(op: &MigrationOp, target: &IrEnvelope<SchemaIrPayload>) -> StepClass {
    match op {
        // A rename moves what the previous release writes to.
        MigrationOp::RenameTable { .. }
        | MigrationOp::RenameColumn { .. }
        | MigrationOp::RenameEnumType { .. }
        | MigrationOp::RenameEnumLabel { .. }
        // A type change, a removed label, a moved key: the previous
        // release's values may no longer fit.
        | MigrationOp::AlterColumnType { .. }
        | MigrationOp::RemoveEnumLabel { .. }
        | MigrationOp::ChangePrimaryKey { .. }
        // A constraint added or rebuilt refuses rows it did not refuse.
        | MigrationOp::AddCheck { .. }
        | MigrationOp::RebuildCheck { .. }
        | MigrationOp::AddForeignKey { .. }
        | MigrationOp::RebuildForeignKey { .. }
        | MigrationOp::ValidateConstraint { .. }
        // A policy, or row security turned on, can refuse a write.
        | MigrationOp::AddRowPolicy { .. }
        | MigrationOp::RebuildRowPolicy { .. }
        | MigrationOp::DropRowPolicy { .. }
        | MigrationOp::EnableRowSecurity { .. }
        | MigrationOp::ForceRowSecurity { .. } => StepClass::Contract,
        // A unique index refuses a duplicate the previous release may write.
        MigrationOp::AddIndex { unique, .. } | MigrationOp::RebuildIndex { unique, .. } => {
            constraint_class(*unique)
        }
        // Undeclared on the target, its uniqueness is unknown: contract.
        MigrationOp::RedefineIndex { table, name } => constraint_class(
            declared_index(target, table, name).is_none_or(|(_, unique)| unique),
        ),
        MigrationOp::AddEnumLabel { .. }
        | MigrationOp::CreateEnumType { .. }
        | MigrationOp::DropEnumType { .. }
        | MigrationOp::AddTable { .. }
        | MigrationOp::DropTable { .. }
        | MigrationOp::AddColumn { .. }
        | MigrationOp::DropColumn { .. }
        | MigrationOp::AlterColumnNullability { .. }
        | MigrationOp::DropIndex { .. }
        | MigrationOp::RenameIndex { .. }
        | MigrationOp::RenameConstraint { .. }
        | MigrationOp::RenamePolicy { .. }
        | MigrationOp::DropForeignKey { .. }
        // A foreign default nothing declares; no release relies on it.
        | MigrationOp::DropDefault { .. }
        | MigrationOp::DropCheck { .. }
        | MigrationOp::DisableRowSecurity { .. }
        | MigrationOp::NoForceRowSecurity { .. } => StepClass::Expand,
    }
}

/// Whether `op` changes nothing of its column but the labels it admits, and
/// only adds to them: a column's type or check rebuilt because an enum
/// gained a label.
fn adds_labels_only(op: &MigrationOp, sides: Sides<'_>) -> bool {
    let model = |ir: &IrEnvelope<SchemaIrPayload>, table: &str| {
        ir.payload
            .models
            .iter()
            .find(|model| model.table_name == table)
            .cloned()
    };
    let superset = |old: &[serde_json::Value], new: &[serde_json::Value]| {
        new.len() > old.len() && old.iter().all(|label| new.contains(label))
    };
    match op {
        MigrationOp::AlterColumnType { table, column } => {
            let column_of = |ir| {
                model(ir, table).and_then(|m| m.columns.into_iter().find(|c| &c.name == column))
            };
            match (column_of(sides.before), column_of(sides.target)) {
                (Some(old), Some(new)) => match (&old.enum_values, &new.enum_values) {
                    (Some(was), Some(now)) => {
                        superset(was, now) && storage_facts(&old) == storage_facts(&new)
                    }
                    _ => false,
                },
                _ => false,
            }
        }
        MigrationOp::RebuildCheck { table, name } => {
            let check_of =
                |ir| model(ir, table).and_then(|m| m.checks.into_iter().find(|c| &c.name == name));
            match (check_of(sides.before), check_of(sides.target)) {
                (Some(old), Some(new)) => {
                    old.column == new.column
                        && new.values.len() > old.values.len()
                        && old.values.iter().all(|value| new.values.contains(value))
                }
                _ => false,
            }
        }
        _ => false,
    }
}

/// `column` without its labels and the facts that render no storage. The
/// schema IR offers no DDL-bearing projection of a column, so this is the
/// one list, and its rule is: a field is left out only when it renders no
/// storage (the labels it retires (ADR-0053), whether it is retired, its
/// server default and its Python default factory), or it is the labels it
/// admits, which [`adds_labels_only`] compares on its own (a superset).
/// Every field that renders storage (the type, `db_type`, nullability,
/// length, key and enum-type facts) stays in the comparison. Two columns
/// with equal storage facts differ, if at all, in what they admit and in
/// what no statement changes.
fn storage_facts(column: &ferro_schema_ir::SchemaColumn) -> ferro_schema_ir::SchemaColumn {
    ferro_schema_ir::SchemaColumn {
        enum_values: None,
        enum_retired_labels: None,
        retired: false,
        generated: None,
        default: None,
        default_factory: None,
        ..column.clone()
    }
}

/// The class of every step of `migration` on a `dialect` database, by
/// ordinal: what the generator decides for the step when it regenerates the
/// migration from its parent's snapshot and its own (a generated step is
/// known by its name), the file's own declaration for a step it does not
/// generate (a hand-written one), and `expand` for a data step, each by
/// [`step_class`]. A DDL step regeneration does not produce under its name, or
/// produces with another body than its file holds on the dialect (a step a
/// person edited, ADR-0030), is read as hand-written: `contract` unless its
/// file says `expand`.
///
/// # Errors
/// [`RunRefusal::Unclassifiable`] when the migration holds a generated step
/// and its snapshots do not regenerate: the generator refuses them, so no
/// class is decided and none is guessed.
pub fn migration_classes(
    dir: &MigrationsDir,
    migration: &Migration,
    dialect: Dialect,
) -> Result<BTreeMap<u8, StepClass>, RunRefusal> {
    let generated = regenerate(dir, migration).map_err(|error| RunRefusal::Unclassifiable {
        migration: migration.number,
        migration_name: migration.dir_name(),
        error: error.to_string(),
    })?;
    let on = StepDialect::from(dialect);
    Ok(migration
        .steps
        .iter()
        .map(|step| {
            let class = match step.kind {
                StepKind::Data => step_class(&LaidOutStep::Data),
                StepKind::PortableSql => declared_class(step, on),
                StepKind::Ddl => generated
                    .iter()
                    .find(|g| g.kind == StepKind::Ddl && g.name == step.name)
                    .filter(|g| renders_as_its_file(g, step, on))
                    .and_then(|g| g.class.get(&on).copied())
                    .unwrap_or_else(|| declared_class(step, on)),
            };
            (step.ordinal, class)
        })
        .collect())
}

/// Whether `step`'s file on dialect `on` is, byte for byte, what the
/// generator renders for it (`generated`): only then is the class the
/// generator decides the class of what the file does. A generated step a
/// person edited (ADR-0030) is classed as a hand-written one.
fn renders_as_its_file(generated: &GeneratedStep, step: &Step, on: StepDialect) -> bool {
    match (generated.renderings.get(&on), step.files.get(&on)) {
        (Some(rendering), Some(file)) => sha384(rendering.up.as_bytes()) == file.up_checksum,
        _ => false,
    }
}

/// A step's class as its file declares it, on dialect `on`.
fn declared_class(step: &Step, on: StepDialect) -> StepClass {
    step.files
        .get(&on)
        .or_else(|| step.files.get(&StepDialect::Portable))
        .map_or(StepClass::Contract, |file| {
            step_class(&LaidOutStep::HandSql {
                headers: &file.headers,
            })
        })
}

/// `migration`'s steps as the generator lays them out from its parent's
/// snapshot to its own, for the dialects its generated steps render; with
/// the data step a person wrote (`--data-step <Model>`, `backfill_<model>`)
/// when the migration holds one the plain layout lacks, since it holds the
/// migration's drops back for its contract. Empty when the migration holds
/// no generated step, or its snapshots change nothing.
///
/// # Errors
/// The generator's refusal of the migration's snapshots.
fn regenerate(
    dir: &MigrationsDir,
    migration: &Migration,
) -> Result<Vec<GeneratedStep>, GenerateError> {
    let mut dialects: Vec<Dialect> = Vec::new();
    for step in migration.steps.iter().filter(|s| s.kind == StepKind::Ddl) {
        for on in step.files.keys() {
            let dialect = match on {
                StepDialect::Postgres => Dialect::Postgres,
                StepDialect::Sqlite => Dialect::Sqlite,
                StepDialect::Portable => continue,
            };
            if !dialects.contains(&dialect) {
                dialects.push(dialect);
            }
        }
    }
    if dialects.is_empty() {
        return Ok(Vec::new());
    }
    let parent = migration
        .number
        .checked_sub(1)
        .and_then(|number| dir.migrations.iter().find(|m| m.number == number))
        .map(|m| &m.snapshot);
    let target = &migration.snapshot.ir;
    let layout = |options: &GenerateOptions| {
        generate_with(parent, target, &dialects, options)
            .map(|generated| generated.map(|g| g.steps).unwrap_or_default())
    };
    let steps = layout(&GenerateOptions::default())?;
    let hand = migration.steps.iter().find(|step| {
        step.kind == StepKind::Data
            && !steps
                .iter()
                .any(|g| g.kind == StepKind::Data && g.name == step.name)
    });
    let hand_model = hand.and_then(|step| {
        target
            .payload
            .models
            .iter()
            .map(|model| model.model_name.rsplit('.').next().unwrap_or_default())
            .find(|short| format!("backfill_{}", short.to_lowercase()) == step.name)
    });
    match hand_model {
        Some(model) => layout(&GenerateOptions {
            data_step: Some(model.to_string()),
            ..GenerateOptions::default()
        }),
        None => Ok(steps),
    }
}

#[cfg(test)]
mod tests {
    //! The golden table: every row of ADR-0054's table, through the
    //! generator's own layout (the class the run and the baseline record),
    //! on both dialects.
    use super::*;
    use crate::generate::generate;
    use crate::snapshot::Snapshot;
    use crate::{Side, plan_from_ir};
    use ferro_schema_ir::{SchemaCheck, SchemaColumn, SchemaForeignKey, SchemaIndex, SchemaModel};

    use StepClass::{Contract, Expand};

    fn ir(models: Vec<SchemaModel>) -> IrEnvelope<SchemaIrPayload> {
        IrEnvelope {
            ir_kind: "schema".into(),
            ir_version: 2,
            payload: SchemaIrPayload {
                dialect_agnostic: true,
                models,
            },
        }
    }

    fn column(name: &str, logical_type: &str) -> SchemaColumn {
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
            enum_retired_labels: Default::default(),
            default_factory: None,
            retired: false,
            generated: None,
        }
    }

    fn nullable(name: &str, logical_type: &str) -> SchemaColumn {
        SchemaColumn {
            nullable: true,
            ..column(name, logical_type)
        }
    }

    fn pk() -> SchemaColumn {
        SchemaColumn {
            primary_key: true,
            autoincrement: true,
            ..column("id", "integer")
        }
    }

    fn model(name: &str, columns: Vec<SchemaColumn>) -> SchemaModel {
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

    /// `author (id, name, team_id)`, `team (id)`, with an FK from
    /// `author.team_id`.
    fn author(extra: Vec<SchemaColumn>) -> SchemaModel {
        let mut columns = vec![
            pk(),
            column("name", "string"),
            nullable("team_id", "integer"),
        ];
        columns.extend(extra);
        model("Author", columns)
    }

    fn team() -> SchemaModel {
        model("Team", vec![pk()])
    }

    fn with_fk(mut author: SchemaModel) -> SchemaModel {
        author.foreign_keys.push(SchemaForeignKey {
            renamed_from: None,
            column: "team_id".into(),
            to_table: "team".into(),
            to_column: "id".into(),
            on_delete: Some("CASCADE".into()),
            name: Some("fk_author_team_id_team".into()),
        });
        author
    }

    fn with_index(mut author: SchemaModel, unique: bool) -> SchemaModel {
        author.indexes.push(SchemaIndex {
            name: "idx_author_name".into(),
            columns: vec!["name".into()],
            unique,
        });
        author
    }

    fn with_check(mut author: SchemaModel) -> SchemaModel {
        author.checks.push(SchemaCheck {
            name: "ck_author_name".into(),
            column: "name".into(),
            values: vec!["'a'".into(), "'b'".into()],
        });
        author
    }

    fn status(labels: &[&str]) -> SchemaColumn {
        SchemaColumn {
            enum_values: Some(labels.iter().map(|l| serde_json::json!(l)).collect()),
            enum_type_name: Some("status".into()),
            postgres_native_enum: true,
            ..column("status", "string")
        }
    }

    const BOTH: [Dialect; 2] = [Dialect::Postgres, Dialect::Sqlite];

    /// Each generated step of `before → after` on `dialect`, `NN_<name>`
    /// with its class.
    fn classes(
        before: Vec<SchemaModel>,
        after: Vec<SchemaModel>,
        dialect: Dialect,
    ) -> Vec<(String, StepClass)> {
        let parent =
            Snapshot::load(&Snapshot::store(&ir(before), None, &[]).expect("store")).expect("load");
        let migration = generate(Some(&parent), &ir(after), &BOTH)
            .expect("generates")
            .expect("changes something");
        migration
            .steps
            .iter()
            .map(|step| {
                (
                    format!("{:02}_{}", step.ordinal, step.name),
                    step.class[&StepDialect::from(dialect)],
                )
            })
            .collect()
    }

    /// A step file holding `up`, as the Python layout writes a rendering.
    fn step_file(on: StepDialect, ordinal: u8, name: &str, up: &str) -> crate::directory::StepFile {
        let suffix = match on {
            StepDialect::Postgres => ".postgres",
            StepDialect::Sqlite => ".sqlite",
            StepDialect::Portable => "",
        };
        crate::directory::StepFile {
            up: format!("/m/{ordinal:02}_{name}.up{suffix}.sql").into(),
            down: Some(format!("/m/{ordinal:02}_{name}.down{suffix}.sql").into()),
            up_checksum: crate::snapshot::sha384(up.as_bytes()),
            headers: Headers::parse(up).expect("headers"),
            down_checksum: Some(crate::snapshot::sha384(b"")),
            down_headers: Headers::default(),
        }
    }

    /// The directory `ferro migrate new` writes for each schema in turn,
    /// every generated file as the generator renders it.
    fn generated_dir(schemas: Vec<Vec<SchemaModel>>) -> MigrationsDir {
        let mut migrations: Vec<Migration> = Vec::new();
        for (i, models) in schemas.into_iter().enumerate() {
            let target = ir(models);
            let parent = migrations.last().map(|m| &m.snapshot);
            let generated = generate(parent, &target, &BOTH)
                .expect("generates")
                .expect("changes something");
            let bytes = Snapshot::store(&target, parent.map(|s| s.checksum), &[]).expect("store");
            let steps = generated
                .steps
                .iter()
                .map(|g| Step {
                    ordinal: g.ordinal,
                    name: g.name.clone(),
                    kind: g.kind,
                    files: if g.kind == StepKind::Data {
                        BTreeMap::from([(
                            StepDialect::Portable,
                            crate::directory::StepFile {
                                up: format!("/m/{:02}_{}.py", g.ordinal, g.name).into(),
                                down: None,
                                up_checksum: crate::snapshot::sha384(b"# data"),
                                headers: Headers::default(),
                                down_checksum: None,
                                down_headers: Headers::default(),
                            },
                        )])
                    } else {
                        g.renderings
                            .iter()
                            .map(|(on, r)| (*on, step_file(*on, g.ordinal, &g.name, &r.up)))
                            .collect()
                    },
                })
                .collect();
            let number = u16::try_from(i + 1).expect("number");
            migrations.push(Migration {
                number,
                name: format!("m{number}"),
                dir: format!("/migrations/{number:04}_m{number}").into(),
                steps,
                witness: Vec::new(),
                snapshot: Snapshot::load(&bytes).expect("load"),
            });
        }
        MigrationsDir {
            path: "/proj/migrations".into(),
            migrations,
        }
    }

    #[test]
    fn a_generated_step_edited_by_hand_is_classed_by_its_file() {
        let mut dir = generated_dir(vec![
            vec![author(vec![])],
            vec![author(vec![nullable("bio", "string")])],
        ]);
        for dialect in BOTH {
            let classes = migration_classes(&dir, &dir.migrations[1], dialect).expect("classes");
            assert_eq!(classes[&1], Expand, "as generated, {dialect:?}");
        }
        // ADR-0030 lets a person edit an unfinished step: a drop added to
        // the generated expand step makes it contract, and only the
        // file's own `-- ferro: expand` says otherwise.
        let on = StepDialect::Sqlite;
        let schema = &mut dir.migrations[1].steps[0];
        let body = |header: &str| {
            format!(
                "{header}ALTER TABLE \"author\" ADD COLUMN \"bio\" varchar;\nALTER TABLE \"author\" DROP COLUMN \"name\";\n"
            )
        };
        schema
            .files
            .insert(on, step_file(on, 1, "schema", &body("")));
        assert_eq!(
            migration_classes(&dir, &dir.migrations[1], Dialect::Sqlite).expect("classes")[&1],
            Contract
        );
        let schema = &mut dir.migrations[1].steps[0];
        schema
            .files
            .insert(on, step_file(on, 1, "schema", &body("-- ferro: expand\n")));
        assert_eq!(
            migration_classes(&dir, &dir.migrations[1], Dialect::Sqlite).expect("classes")[&1],
            Expand
        );
        // The other dialect's file is untouched: still the generator's.
        assert_eq!(
            migration_classes(&dir, &dir.migrations[1], Dialect::Postgres).expect("classes")[&1],
            Expand
        );
    }

    #[test]
    fn a_migration_that_does_not_regenerate_refuses_naming_it() {
        // 0002's snapshots move author's key, which the generator refuses:
        // no class is decided for its generated step, and none is guessed.
        let mut dir = generated_dir(vec![vec![author(vec![])]]);
        let mut moved = author(vec![]);
        moved.columns[0].primary_key = false;
        moved.columns[0].autoincrement = false;
        moved.columns[1].primary_key = true;
        let parent = dir.migrations[0].snapshot.checksum;
        let bytes = Snapshot::store(&ir(vec![moved]), Some(parent), &[]).expect("store");
        dir.migrations.push(Migration {
            number: 2,
            name: "move_key".into(),
            dir: "/migrations/0002_move_key".into(),
            steps: vec![Step {
                ordinal: 1,
                name: "schema".into(),
                kind: StepKind::Ddl,
                files: BOTH
                    .iter()
                    .map(|d| {
                        let on = StepDialect::from(*d);
                        (on, step_file(on, 1, "schema", "SELECT 1;\n"))
                    })
                    .collect(),
            }],
            witness: Vec::new(),
            snapshot: Snapshot::load(&bytes).expect("load"),
        });
        let refusal =
            migration_classes(&dir, &dir.migrations[1], Dialect::Sqlite).expect_err("refuses");
        assert_eq!(refusal.kind(), "unclassifiable");
        let text = refusal.to_string();
        assert!(
            text.starts_with(
                "ferro migrate: 0002_move_key does not regenerate from its snapshots, so ferro \
                 cannot decide the class of its generated steps (ADR-0054): "
            ),
            "{text}"
        );
        assert!(text.contains("primary key"), "{text}");
        assert!(
            text.ends_with("No class was recorded for it, and none of its steps was applied."),
            "{text}"
        );
        // A migration that regenerates still answers.
        assert!(migration_classes(&dir, &dir.migrations[0], Dialect::Sqlite).is_ok());
    }

    fn named(rows: &[(&str, StepClass)]) -> Vec<(String, StepClass)> {
        rows.iter().map(|(n, c)| (n.to_string(), *c)).collect()
    }

    #[test]
    fn adding_a_table_or_a_nullable_column_is_expand() {
        for dialect in BOTH {
            assert_eq!(
                classes(vec![author(vec![])], vec![author(vec![]), team()], dialect),
                named(&[("01_schema", Expand)]),
                "{dialect:?}"
            );
            assert_eq!(
                classes(
                    vec![author(vec![])],
                    vec![author(vec![nullable("bio", "string")])],
                    dialect
                ),
                named(&[("01_schema", Expand)]),
                "{dialect:?}"
            );
        }
    }

    #[test]
    fn dropping_data_is_contract() {
        let with_bio = author(vec![nullable("bio", "string")]);
        for dialect in BOTH {
            assert_eq!(
                classes(vec![with_bio.clone()], vec![author(vec![])], dialect),
                named(&[("01_schema", Contract)]),
                "drop column on {dialect:?}"
            );
            assert_eq!(
                classes(vec![author(vec![]), team()], vec![author(vec![])], dialect),
                named(&[("01_schema", Contract)]),
                "drop table on {dialect:?}"
            );
        }
    }

    #[test]
    fn a_column_that_demands_values_expands_then_contracts() {
        let required = author(vec![column("slug", "string")]);
        assert_eq!(
            classes(
                vec![author(vec![])],
                vec![required.clone()],
                Dialect::Postgres
            ),
            named(&[
                ("01_expand", Expand),
                ("02_backfill_author", Expand),
                ("03_add_constraint", Contract),
                ("04_contract", Contract),
            ])
        );
        assert_eq!(
            classes(vec![author(vec![])], vec![required], Dialect::Sqlite),
            named(&[
                ("01_expand", Expand),
                ("02_backfill_author", Expand),
                // Rendered not-applicable on SQLite: it runs nothing.
                ("03_add_constraint", Expand),
                ("04_contract", Contract),
            ])
        );
    }

    #[test]
    fn set_not_null_is_contract_and_drop_not_null_is_expand() {
        let loose = author(vec![nullable("bio", "string")]);
        let strict = author(vec![column("bio", "string")]);
        for dialect in BOTH {
            assert_eq!(
                classes(vec![strict.clone()], vec![loose.clone()], dialect),
                named(&[("01_schema", Expand)]),
                "DROP NOT NULL on {dialect:?}"
            );
            let set = classes(vec![loose.clone()], vec![strict.clone()], dialect);
            assert_eq!(set.last().map(|(_, c)| *c), Some(Contract), "{set:?}");
            assert!(set.iter().any(|(n, _)| n.ends_with("_contract")), "{set:?}");
        }
    }

    #[test]
    fn renames_are_contract() {
        let mut renamed = author(vec![]);
        renamed.columns[1] = SchemaColumn {
            renamed_from: Some("name".into()),
            ..column("full_name", "string")
        };
        let mut table = author(vec![]);
        table.renamed_from = Some("author".into());
        table.table_name = "writer".into();
        table.model_name = "myapp.models.Writer".into();
        for dialect in BOTH {
            assert_eq!(
                classes(vec![author(vec![])], vec![renamed.clone()], dialect),
                named(&[("01_schema", Contract)]),
                "column rename on {dialect:?}"
            );
            assert_eq!(
                classes(vec![author(vec![])], vec![table.clone()], dialect),
                named(&[("01_schema", Contract)]),
                "table rename on {dialect:?}"
            );
        }
    }

    #[test]
    fn a_type_change_is_contract_on_every_dialect_a_sqlite_rebuild_included() {
        let mut retyped = author(vec![]);
        retyped.columns[1] = column("name", "integer");
        for dialect in BOTH {
            assert_eq!(
                classes(vec![author(vec![])], vec![retyped.clone()], dialect),
                named(&[("01_schema", Contract)]),
                "{dialect:?}"
            );
        }
    }

    #[test]
    fn indexes_are_expand_unless_unique() {
        let before = author(vec![]);
        for dialect in BOTH {
            let plain = classes(
                vec![before.clone()],
                vec![with_index(before.clone(), false)],
                dialect,
            );
            assert_eq!(plain.len(), 1, "{plain:?}");
            assert_eq!(plain[0].1, Expand, "plain index on {dialect:?}");
            let unique = classes(
                vec![before.clone()],
                vec![with_index(before.clone(), true)],
                dialect,
            );
            assert_eq!(unique.len(), 1, "{unique:?}");
            assert_eq!(unique[0].1, Contract, "unique index on {dialect:?}");
            for was_unique in [false, true] {
                let dropped = classes(
                    vec![with_index(before.clone(), was_unique)],
                    vec![before.clone()],
                    dialect,
                );
                assert_eq!(dropped.len(), 1, "{dropped:?}");
                assert_eq!(dropped[0].1, Expand, "dropped index on {dialect:?}");
            }
        }
    }

    #[test]
    fn dropping_a_check_or_a_foreign_key_is_expand_a_sqlite_rebuild_included() {
        let before = author(vec![]);
        for dialect in BOTH {
            assert_eq!(
                classes(
                    vec![with_check(before.clone())],
                    vec![before.clone()],
                    dialect
                ),
                named(&[("01_schema", Expand)]),
                "drop check on {dialect:?}"
            );
            assert_eq!(
                classes(
                    vec![with_fk(before.clone()), team()],
                    vec![before.clone(), team()],
                    dialect
                ),
                named(&[("01_schema", Expand)]),
                "drop foreign key on {dialect:?}"
            );
        }
    }

    #[test]
    fn a_sqlite_rebuild_carrying_one_contract_op_is_contract() {
        let mut retyped = author(vec![]);
        retyped.columns[1] = column("name", "integer");
        let rows = classes(
            vec![with_fk(author(vec![])), team()],
            vec![retyped, team()],
            Dialect::Sqlite,
        );
        assert_eq!(rows, named(&[("01_schema", Contract)]));
    }

    #[test]
    fn adding_a_check_or_a_foreign_key_stages_it_not_valid_and_is_contract() {
        let before = author(vec![]);
        assert_eq!(
            classes(
                vec![before.clone()],
                vec![with_check(before.clone())],
                Dialect::Postgres
            ),
            named(&[("01_schema", Contract), ("02_validate", Contract)])
        );
        assert_eq!(
            classes(
                vec![before.clone()],
                vec![with_check(before.clone())],
                Dialect::Sqlite
            ),
            // The rebuild validates by copying; the validate step runs
            // nothing on SQLite.
            named(&[("01_schema", Contract), ("02_validate", Expand)])
        );
        assert_eq!(
            classes(
                vec![before.clone(), team()],
                vec![with_fk(before.clone()), team()],
                Dialect::Postgres
            ),
            named(&[("01_schema", Contract), ("02_validate", Contract)])
        );
    }

    #[test]
    fn an_added_label_is_expand() {
        let before = author(vec![status(&["draft", "live"])]);
        let after = author(vec![status(&["draft", "live", "archived"])]);
        for dialect in BOTH {
            let rows = classes(vec![before.clone()], vec![after.clone()], dialect);
            assert!(
                rows.iter().all(|(_, class)| *class == Expand),
                "{dialect:?}: {rows:?}"
            );
        }
    }

    #[test]
    fn an_added_label_beside_a_retired_one_is_expand() {
        // Release B adds `cancelled_by_user` and retires `canceled` (the
        // online label-rename recipe). On SQLite the longer label widens the
        // column's `varchar`, a rebuild; the retirement renders nothing.
        let before = author(vec![status(&["draft", "canceled"])]);
        let after = author(vec![SchemaColumn {
            enum_retired_labels: Some(vec!["canceled".into()]),
            ..status(&["draft", "canceled", "cancelled_by_user"])
        }]);
        for dialect in BOTH {
            let rows = classes(vec![before.clone()], vec![after.clone()], dialect);
            assert!(!rows.is_empty(), "{dialect:?}");
            assert!(
                rows.iter().all(|(_, class)| *class == Expand),
                "{dialect:?}: {rows:?}"
            );
        }
    }

    #[test]
    fn a_hand_written_sql_step_is_contract_unless_it_says_expand() {
        assert_eq!(
            step_class(&LaidOutStep::HandSql {
                headers: &Headers::default()
            }),
            Contract
        );
        assert_eq!(
            step_class(&LaidOutStep::HandSql {
                headers: &Headers {
                    expand: true,
                    ..Headers::default()
                }
            }),
            Expand
        );
    }

    #[test]
    fn a_data_step_is_expand() {
        assert_eq!(step_class(&LaidOutStep::Data), Expand);
    }

    #[test]
    fn a_constraint_step_is_contract_wherever_it_runs_a_statement() {
        assert_eq!(
            step_class(&LaidOutStep::Constraints {
                headers: &Headers::default()
            }),
            Contract
        );
        assert_eq!(
            step_class(&LaidOutStep::Constraints {
                headers: &Headers {
                    not_applicable: true,
                    ..Headers::default()
                }
            }),
            Expand
        );
    }

    #[test]
    fn every_op_reads_its_class_off_its_verdict_and_kind() {
        // The op-level table, against the verdicts the one planner decides.
        // The whole plan carried by one contract step, which runs each op as
        // it is planned: what the verdicts and kinds alone decide.
        let class_of = |before: Vec<SchemaModel>, after: Vec<SchemaModel>| {
            let (before, after) = (ir(before), ir(after));
            let plan = plan_from_ir(
                &Side::declared(before.clone()),
                &Side::declared(after.clone()),
                Dialect::Postgres,
                crate::PlanOptions { destructive: true },
            );
            let sides = Sides {
                before: &before,
                target: &after,
            };
            step_class(&LaidOutStep::Contract {
                ops: &plan.operations,
                sides,
                expand: SchemaStep {
                    ops: &[],
                    sides,
                    dialect: Dialect::Postgres,
                },
                demands: &[],
            })
        };
        assert_eq!(
            class_of(vec![author(vec![])], vec![author(vec![]), team()]),
            Expand
        );
        assert_eq!(
            class_of(vec![author(vec![]), team()], vec![author(vec![])]),
            Contract
        );
        assert_eq!(
            class_of(
                vec![author(vec![])],
                vec![author(vec![column("slug", "string")])]
            ),
            Contract,
            "a column demanding values, planned alone"
        );
        assert_eq!(
            class_of(
                vec![author(vec![])],
                vec![author(vec![nullable("bio", "string")])]
            ),
            Expand
        );
    }

    /// The class of a `schema` step on `dialect` carrying `ops` between
    /// `sides`.
    fn schema_class(ops: &[PlannedOp], sides: Sides<'_>, dialect: Dialect) -> StepClass {
        step_class(&LaidOutStep::Schema(SchemaStep {
            ops,
            sides,
            dialect,
        }))
    }

    #[test]
    fn every_op_kind_has_its_class_under_a_quiet_verdict() {
        // Each op kind with a verdict that drops nothing, demands nothing
        // and cannot fail on rows, alone in a schema step: what the kind
        // alone decides.
        let t = || "author".to_string();
        let empty = ir(vec![]);
        let sides = Sides {
            before: &empty,
            target: &empty,
        };
        let quiet = |op: MigrationOp| PlannedOp {
            op,
            verdict: crate::OpVerdict::default(),
        };
        let rows: Vec<(MigrationOp, StepClass)> = vec![
            (
                MigrationOp::AddEnumLabel {
                    type_name: "s".into(),
                    label: "x".into(),
                },
                Expand,
            ),
            (
                MigrationOp::CreateEnumType {
                    type_name: "s".into(),
                    labels: vec![],
                },
                Expand,
            ),
            (
                MigrationOp::DropEnumType {
                    type_name: "s".into(),
                },
                Expand,
            ),
            (
                MigrationOp::RenameEnumLabel {
                    type_name: "s".into(),
                    old: "a".into(),
                    new: "b".into(),
                    columns: vec![],
                },
                Contract,
            ),
            (
                MigrationOp::RemoveEnumLabel {
                    type_name: "s".into(),
                    label: "a".into(),
                    columns: vec![],
                },
                Contract,
            ),
            (
                MigrationOp::RenameEnumType {
                    old: "s".into(),
                    new: "t".into(),
                },
                Contract,
            ),
            (MigrationOp::AddTable { table: t() }, Expand),
            (MigrationOp::DropTable { table: t() }, Expand),
            (
                MigrationOp::RenameTable {
                    old: "a".into(),
                    new: t(),
                },
                Contract,
            ),
            (
                MigrationOp::RenameColumn {
                    table: t(),
                    old: "a".into(),
                    new: "b".into(),
                },
                Contract,
            ),
            (
                MigrationOp::RenameIndex {
                    table: t(),
                    old: "a".into(),
                    new: "b".into(),
                },
                Expand,
            ),
            (
                MigrationOp::RenameConstraint {
                    table: t(),
                    old: "a".into(),
                    new: "b".into(),
                },
                Expand,
            ),
            (
                MigrationOp::RenamePolicy {
                    table: t(),
                    old: "a".into(),
                    new: "b".into(),
                },
                Expand,
            ),
            (
                MigrationOp::AddColumn {
                    table: t(),
                    column: "c".into(),
                },
                Expand,
            ),
            (
                MigrationOp::DropColumn {
                    table: t(),
                    column: "c".into(),
                },
                Expand,
            ),
            (
                MigrationOp::AlterColumnType {
                    table: t(),
                    column: "c".into(),
                },
                Contract,
            ),
            (
                MigrationOp::AlterColumnNullability {
                    table: t(),
                    column: "c".into(),
                },
                Expand,
            ),
            (
                MigrationOp::ChangePrimaryKey {
                    table: t(),
                    from: vec![],
                    to: vec![],
                },
                Contract,
            ),
            (
                MigrationOp::AddIndex {
                    table: t(),
                    name: "i".into(),
                    columns: vec![],
                    unique: false,
                },
                Expand,
            ),
            (
                MigrationOp::AddIndex {
                    table: t(),
                    name: "i".into(),
                    columns: vec![],
                    unique: true,
                },
                Contract,
            ),
            (
                MigrationOp::DropIndex {
                    table: t(),
                    name: "i".into(),
                },
                Expand,
            ),
            // Undeclared on the target: its uniqueness is unknown.
            (
                MigrationOp::RedefineIndex {
                    table: t(),
                    name: "i".into(),
                },
                Contract,
            ),
            (
                MigrationOp::AddForeignKey {
                    table: t(),
                    column: "c".into(),
                },
                Contract,
            ),
            (
                MigrationOp::DropForeignKey {
                    table: t(),
                    column: "c".into(),
                    name: "f".into(),
                },
                Expand,
            ),
            (
                MigrationOp::AddCheck {
                    table: t(),
                    name: "k".into(),
                },
                Contract,
            ),
            (
                MigrationOp::RebuildCheck {
                    table: t(),
                    name: "k".into(),
                },
                Contract,
            ),
            (
                MigrationOp::DropCheck {
                    table: t(),
                    name: "k".into(),
                },
                Expand,
            ),
            (
                MigrationOp::RebuildForeignKey {
                    table: t(),
                    column: "c".into(),
                    old_name: "f".into(),
                },
                Contract,
            ),
            (
                MigrationOp::ValidateConstraint {
                    table: t(),
                    name: "k".into(),
                },
                Contract,
            ),
            (
                MigrationOp::RebuildIndex {
                    table: t(),
                    name: "i".into(),
                    columns: vec![],
                    unique: false,
                },
                Expand,
            ),
            (
                MigrationOp::RebuildIndex {
                    table: t(),
                    name: "i".into(),
                    columns: vec![],
                    unique: true,
                },
                Contract,
            ),
            (
                MigrationOp::AddRowPolicy {
                    table: t(),
                    name: "p".into(),
                },
                Contract,
            ),
            (
                MigrationOp::RebuildRowPolicy {
                    table: t(),
                    name: "p".into(),
                },
                Contract,
            ),
            (
                MigrationOp::DropRowPolicy {
                    table: t(),
                    name: "p".into(),
                },
                Contract,
            ),
            (MigrationOp::EnableRowSecurity { table: t() }, Contract),
            (MigrationOp::ForceRowSecurity { table: t() }, Contract),
            (MigrationOp::DisableRowSecurity { table: t() }, Expand),
            (MigrationOp::NoForceRowSecurity { table: t() }, Expand),
        ];
        for (op, class) in rows {
            for dialect in BOTH {
                assert_eq!(
                    schema_class(&[quiet(op.clone())], sides, dialect),
                    class,
                    "{op:?} on {dialect:?}"
                );
            }
        }
        // The verdict overrides the kind: dropping data, demanding values
        // and failing on rows are each contract.
        let add = MigrationOp::AddColumn {
            table: t(),
            column: "c".into(),
        };
        let demanding = PlannedOp {
            op: add.clone(),
            verdict: crate::OpVerdict {
                demands_values: true,
                ..Default::default()
            },
        };
        // In the schema (expand) step a demanded column is added nullable:
        // its backfill and contract come later. The contract that holds it
        // to its values is contract.
        assert_eq!(
            schema_class(std::slice::from_ref(&demanding), sides, Dialect::Postgres),
            Expand
        );
        assert_eq!(
            step_class(&LaidOutStep::Contract {
                ops: std::slice::from_ref(&demanding),
                sides,
                expand: SchemaStep {
                    ops: &[],
                    sides,
                    dialect: Dialect::Postgres,
                },
                demands: &[],
            }),
            Contract
        );
        for verdict in [
            crate::OpVerdict {
                drops_data: true,
                ..Default::default()
            },
            crate::OpVerdict {
                fails_on_rows: RowRisk::Always,
                ..Default::default()
            },
            crate::OpVerdict {
                fails_on_rows: RowRisk::WhenValidated,
                ..Default::default()
            },
        ] {
            let planned = PlannedOp {
                op: add.clone(),
                verdict,
            };
            assert_eq!(
                schema_class(std::slice::from_ref(&planned), sides, Dialect::Postgres),
                Contract,
                "{planned:?}"
            );
        }
        // A SQLite rebuild is how the dialect runs the op, not what it does.
        let rebuilt = PlannedOp {
            op: MigrationOp::DropForeignKey {
                table: t(),
                column: "c".into(),
                name: "f".into(),
            },
            verdict: crate::OpVerdict {
                execution: crate::Execution::Rebuild,
                ..Default::default()
            },
        };
        assert_eq!(schema_class(&[rebuilt], sides, Dialect::Sqlite), Expand);
    }

    #[test]
    fn an_index_step_is_contract_only_when_it_builds_a_unique_index() {
        let t = || "author".to_string();
        let declared = ir(vec![with_index(author(vec![]), false)]);
        let index = |op: MigrationOp| {
            step_class(&LaidOutStep::Index {
                op: &op,
                target: &declared,
            })
        };
        for unique in [false, true] {
            assert_eq!(
                index(MigrationOp::AddIndex {
                    table: t(),
                    name: "idx_author_name".into(),
                    columns: vec!["name".into()],
                    unique,
                }),
                if unique { Contract } else { Expand },
            );
        }
        assert_eq!(
            index(MigrationOp::DropIndex {
                table: t(),
                name: "idx_author_name".into(),
            }),
            Expand
        );
        // Redefined as the target declares it: a plain index here.
        assert_eq!(
            index(MigrationOp::RedefineIndex {
                table: t(),
                name: "idx_author_name".into(),
            }),
            Expand
        );
        // Undeclared on the target: its uniqueness is unknown.
        assert_eq!(
            index(MigrationOp::RedefineIndex {
                table: t(),
                name: "idx_author_other".into(),
            }),
            Contract
        );
    }

    #[test]
    fn a_schema_step_staging_a_constraint_not_valid_is_contract_and_so_is_its_contract() {
        // A nullable column with a foreign key, added to a table that
        // exists: on Postgres the schema step adds the key `NOT VALID`, which
        // a write the previous release makes can fail on, and the contract
        // validates it; SQLite's rebuild adds it validated by copying.
        let before = ir(vec![author(vec![]), team()]);
        let mut owned = author(vec![nullable("owner_id", "integer")]);
        owned.foreign_keys.push(SchemaForeignKey {
            renamed_from: None,
            column: "owner_id".into(),
            to_table: "team".into(),
            to_column: "id".into(),
            on_delete: None,
            name: Some("fk_author_owner_id_team".into()),
        });
        let after = ir(vec![owned, team()]);
        let sides = Sides {
            before: &before,
            target: &after,
        };
        let added = [PlannedOp {
            op: MigrationOp::AddColumn {
                table: "author".into(),
                column: "owner_id".into(),
            },
            verdict: crate::OpVerdict::default(),
        }];
        assert_eq!(schema_class(&added, sides, Dialect::Postgres), Contract);
        assert_eq!(schema_class(&added, sides, Dialect::Sqlite), Expand);
        for (dialect, class) in [(Dialect::Postgres, Contract), (Dialect::Sqlite, Expand)] {
            assert_eq!(
                step_class(&LaidOutStep::Contract {
                    ops: &[],
                    sides,
                    expand: SchemaStep {
                        ops: &added,
                        sides,
                        dialect,
                    },
                    demands: &[],
                }),
                class,
                "{dialect:?}"
            );
        }
    }

    #[test]
    fn a_removed_label_is_contract() {
        let before = author(vec![status(&["draft", "live", "archived"])]);
        let after = author(vec![status(&["draft", "live"])]);
        for dialect in BOTH {
            let rows = classes(vec![before.clone()], vec![after.clone()], dialect);
            assert!(
                rows.iter().any(|(_, class)| *class == Contract),
                "{dialect:?}: {rows:?}"
            );
        }
    }
}
