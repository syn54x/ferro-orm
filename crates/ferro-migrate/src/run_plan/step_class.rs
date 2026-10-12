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
//! Decided once, here, from the one planner's op verdicts and op kinds
//! (ADR-0050): the generator asks [`step_class`] (and its siblings) for each
//! step it lays out, and the run and the baseline record what the generator
//! decides, regenerating a migration from its two snapshots
//! ([`migration_classes`]) when they record it. A hand-written SQL step is
//! `contract` unless its file declares `-- ferro: expand`
//! ([`hand_sql_class`]); a data step is always `expand`.
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
use crate::generate::{GenerateOptions, GeneratedStep, generate_with};
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
/// only gained labels).
#[derive(Clone, Copy, Debug)]
pub struct Sides<'a> {
    /// The schema the step starts from.
    pub before: &'a IrEnvelope<SchemaIrPayload>,
    /// The schema it leads to.
    pub target: &'a IrEnvelope<SchemaIrPayload>,
}

/// The class of one planned op between `sides`, from its verdict and its
/// kind.
pub fn op_class(planned: &PlannedOp, sides: Sides<'_>) -> StepClass {
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
    let target = sides.target;
    match &planned.op {
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
        MigrationOp::AddIndex { unique, .. } | MigrationOp::RebuildIndex { unique, .. } => {
            unique_class(*unique)
        }
        MigrationOp::RedefineIndex { table, name } => unique_class(
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

/// `column` without its labels and the facts that render no storage: the
/// labels it retires (ADR-0053), whether it is retired, its server default
/// and its Python default factory. Two columns with equal storage facts
/// differ, if at all, in what they admit and in what no statement changes.
fn storage_facts(column: &ferro_schema_ir::SchemaColumn) -> ferro_schema_ir::SchemaColumn {
    ferro_schema_ir::SchemaColumn {
        enum_values: None,
        enum_retired_labels: None,
        retired: false,
        default: None,
        default_factory: None,
        ..column.clone()
    }
}

fn unique_class(unique: bool) -> StepClass {
    if unique {
        StepClass::Contract
    } else {
        StepClass::Expand
    }
}

/// The class of a SQL step carrying `ops` (natively, or folded into a SQLite
/// rebuild, which runs each of them) toward `target`, and `constrains` when
/// it also adds a constraint `NOT VALID` or validates one: `contract` when
/// anything it does is.
pub fn step_class<'a>(
    ops: impl IntoIterator<Item = &'a PlannedOp>,
    sides: Sides<'_>,
    constrains: bool,
) -> StepClass {
    let base = if constrains {
        StepClass::Contract
    } else {
        StepClass::Expand
    };
    ops.into_iter()
        .map(|op| op_class(op, sides))
        .fold(base, StepClass::max)
}

/// The class of a migration's `schema` (or `expand`) step: [`step_class`],
/// with each column that demands values read as the step adds it, nullable
/// (its backfill and contract come later), so the previous release's writes,
/// which leave it empty, keep succeeding.
pub fn schema_step_class<'a>(
    ops: impl IntoIterator<Item = &'a PlannedOp>,
    sides: Sides<'_>,
    constrains: bool,
) -> StepClass {
    let relaxed: Vec<PlannedOp> = ops
        .into_iter()
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
    step_class(&relaxed, sides, constrains)
}

/// The class of an index step building, redefining or dropping one index
/// toward `target`: a unique index built is `contract`, anything else
/// `expand`.
pub fn index_step_class(op: &MigrationOp, target: &IrEnvelope<SchemaIrPayload>) -> StepClass {
    match op {
        MigrationOp::AddIndex { unique, .. } => unique_class(*unique),
        MigrationOp::RedefineIndex { table, name } => {
            unique_class(declared_index(target, table, name).is_none_or(|(_, unique)| unique))
        }
        _ => StepClass::Expand,
    }
}

/// The class of a step that only adds constraints `NOT VALID` or validates
/// them (the validate and add-constraint steps), from its rendering's
/// headers on a dialect: `contract` wherever it runs a statement, `expand`
/// where its rendering is `not-applicable`.
pub fn constraint_step_class(headers: &Headers) -> StepClass {
    if headers.not_applicable {
        StepClass::Expand
    } else {
        StepClass::Contract
    }
}

/// The class of a hand-written SQL step: what its author declares, `expand`
/// only with `-- ferro: expand`.
pub fn hand_sql_class(headers: &Headers) -> StepClass {
    if headers.expand {
        StepClass::Expand
    } else {
        StepClass::Contract
    }
}

/// The class of every data step, a guard step included: it writes rows, and
/// changes no schema the previous release writes to.
pub const DATA_STEP_CLASS: StepClass = StepClass::Expand;

/// The class of every step of `migration` on a `dialect` database, by
/// ordinal: what the generator decides for the step when it regenerates the
/// migration from its parent's snapshot and its own (a generated step is
/// known by its name), the file's own declaration for a step it does not
/// generate (a hand-written one, [`hand_sql_class`]), and `expand` for a
/// data step. A step regeneration does not produce, because the migration
/// was generated by another ferro or its snapshots no longer generate, is
/// read as hand-written: `contract` unless its file says `expand`.
pub fn migration_classes(
    dir: &MigrationsDir,
    migration: &Migration,
    dialect: Dialect,
) -> BTreeMap<u8, StepClass> {
    let generated = regenerate(dir, migration);
    let on = StepDialect::from(dialect);
    migration
        .steps
        .iter()
        .map(|step| {
            let class = match step.kind {
                StepKind::Data => DATA_STEP_CLASS,
                StepKind::PortableSql => declared_class(step, on),
                StepKind::Ddl => generated
                    .iter()
                    .find(|g| g.kind == StepKind::Ddl && g.name == step.name)
                    .and_then(|g| g.class.get(&on).copied())
                    .unwrap_or_else(|| declared_class(step, on)),
            };
            (step.ordinal, class)
        })
        .collect()
}

/// A step's class as its file declares it, on dialect `on`.
fn declared_class(step: &Step, on: StepDialect) -> StepClass {
    step.files
        .get(&on)
        .or_else(|| step.files.get(&StepDialect::Portable))
        .map_or(StepClass::Contract, |file| hand_sql_class(&file.headers))
}

/// `migration`'s steps as the generator lays them out from its parent's
/// snapshot to its own, for the dialects its generated steps render; with
/// the data step a person wrote (`--data-step <Model>`, `backfill_<model>`)
/// when the migration holds one the plain layout lacks, since it holds the
/// migration's drops back for its contract. Empty when nothing regenerates.
fn regenerate(dir: &MigrationsDir, migration: &Migration) -> Vec<GeneratedStep> {
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
        return Vec::new();
    }
    let parent = migration
        .number
        .checked_sub(1)
        .and_then(|number| dir.migrations.iter().find(|m| m.number == number))
        .map(|m| &m.snapshot);
    let target = &migration.snapshot.ir;
    let layout = |options: &GenerateOptions| {
        generate_with(parent, target, &dialects, options)
            .ok()
            .flatten()
            .map(|generated| generated.steps)
            .unwrap_or_default()
    };
    let steps = layout(&GenerateOptions::default());
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
        None => steps,
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
            Snapshot::load(&Snapshot::store(&ir(before), None).expect("store")).expect("load");
        let migration = generate(Some(&parent), &ir(after), &BOTH)
            .expect("generates")
            .expect("changes something");
        migration
            .steps
            .iter()
            .map(|step| {
                let class = match step.kind {
                    StepKind::Data => DATA_STEP_CLASS,
                    _ => step.class[&StepDialect::from(dialect)],
                };
                (format!("{:02}_{}", step.ordinal, step.name), class)
            })
            .collect()
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
        assert_eq!(hand_sql_class(&Headers::default()), Contract);
        assert_eq!(
            hand_sql_class(&Headers {
                expand: true,
                ..Headers::default()
            }),
            Expand
        );
    }

    #[test]
    fn every_op_reads_its_class_off_its_verdict_and_kind() {
        // The op-level table, against the verdicts the one planner decides.
        let class_of = |before: Vec<SchemaModel>, after: Vec<SchemaModel>| {
            let before_models = before.clone();
            let plan = plan_from_ir(
                &Side::declared(ir(before)),
                &Side::declared(ir(after.clone())),
                Dialect::Postgres,
                crate::PlanOptions { destructive: true },
            );
            plan.operations
                .iter()
                .map(|op| {
                    op_class(
                        op,
                        Sides {
                            before: &ir(before_models.clone()),
                            target: &ir(after.clone()),
                        },
                    )
                })
                .fold(Expand, StepClass::max)
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

    #[test]
    fn every_op_kind_has_its_class_under_a_quiet_verdict() {
        // Each op kind with a verdict that drops nothing, demands nothing
        // and cannot fail on rows: what the kind alone decides.
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
            assert_eq!(op_class(&quiet(op.clone()), sides), class, "{op:?}");
        }
        // The verdict overrides the kind: dropping data, demanding values
        // and failing on rows are each contract.
        let add = MigrationOp::AddColumn {
            table: t(),
            column: "c".into(),
        };
        for verdict in [
            crate::OpVerdict {
                drops_data: true,
                ..Default::default()
            },
            crate::OpVerdict {
                demands_values: true,
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
            assert_eq!(op_class(&planned, sides), Contract, "{planned:?}");
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
        assert_eq!(op_class(&rebuilt, sides), Expand);
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
