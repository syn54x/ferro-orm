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
//! The down is the same planner run backwards (target → parent), so a
//! dropped model's down recreates it from the parent snapshot exactly as a
//! new model's up creates it. Every statement comes from [`render_plan`]: the
//! generator decides which step an op lands in and which headers the file
//! carries, never a statement (AGENTS.md § I-1).
//!
//! This slice generates new and dropped models (their tables, the enum types
//! they introduce or retire, and everything a `CREATE TABLE` carries). Every
//! other change is refused naming the ticket that generates it.

use crate::directory::{DirectoryError, Headers, MigrationsDir, StepDialect, StepKind};
use crate::snapshot::{Snapshot, SnapshotError};
use crate::{
    Dialect, EmissionError, LiveFacts, MigrationOp, MigrationPlan, PlanOptions, plan_from_ir,
    render_plan,
};
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

/// The phase step an op lands in. This slice has one: `schema`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
enum Phase {
    Schema,
}

impl Phase {
    fn step_name(self) -> &'static str {
        match self {
            Phase::Schema => "schema",
        }
    }
}

/// The op's kind as the plan JSON spells it (`AddTable`, …).
fn op_kind(op: &MigrationOp) -> String {
    serde_json::to_value(op)
        .ok()
        .and_then(|value| value.get("kind")?.as_str().map(str::to_string))
        .unwrap_or_else(|| format!("{op:?}"))
}

/// Which step `op` belongs in, or the refusal naming the ticket that
/// generates it.
fn phase_of(op: &MigrationOp) -> Result<Phase, GenerateError> {
    match op {
        MigrationOp::AddTable { .. }
        | MigrationOp::DropTable { .. }
        | MigrationOp::CreateEnumType { .. }
        | MigrationOp::DropEnumType { .. } => Ok(Phase::Schema),
        // A label added to a type that already exists cannot be reversed by a
        // down (Postgres drops no enum label), so it belongs to the enum-label
        // ticket's steps, whichever table introduced it.
        MigrationOp::AddEnumLabel { type_name, .. } => Err(GenerateError::NotGeneratedYet {
            op: op_kind(op),
            subject: type_name.clone(),
            ticket: 529,
        }),
        other => Err(GenerateError::NotGeneratedYet {
            op: op_kind(other),
            subject: other.table().unwrap_or_default().to_string(),
            ticket: 524,
        }),
    }
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
/// the gate).
fn plan(
    old: &IrEnvelope<SchemaIrPayload>,
    new: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
) -> MigrationPlan {
    plan_from_ir(old, new, dialect, &LiveFacts::declared(), DESTRUCTIVE)
}

/// Every warning `plan` raises that planning `standing → standing` does not:
/// the reports this change caused, not the ones the models always raise.
fn change_warnings(plan: &MigrationPlan, standing: &MigrationPlan) -> Vec<String> {
    let already: BTreeSet<&String> = standing
        .warnings
        .iter()
        .chain(&standing.always_warnings)
        .collect();
    plan.warnings
        .iter()
        .chain(&plan.always_warnings)
        .filter(|warning| !already.contains(warning))
        .cloned()
        .collect()
}

/// Refuse anything in `plan` this generator does not generate: an op with no
/// phase, or a warning the change caused that no op answers.
fn refuse_unsupported(
    plan: &MigrationPlan,
    standing: &MigrationPlan,
    dialect: Dialect,
) -> Result<(), GenerateError> {
    for op in &plan.operations {
        phase_of(op)?;
    }
    if let Some(warning) = change_warnings(plan, standing).into_iter().next() {
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

fn removes_something(ops: &[&MigrationOp]) -> bool {
    ops.iter().any(|op| {
        matches!(
            op,
            MigrationOp::DropTable { .. } | MigrationOp::DropEnumType { .. }
        )
    })
}

/// One direction of one step on one dialect: the headers and the file text.
fn render_file(
    plan: &MigrationPlan,
    phase: Phase,
    old: &IrEnvelope<SchemaIrPayload>,
    new: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
    warnings: &mut Vec<String>,
) -> Result<(Headers, String), GenerateError> {
    let mut statements = Vec::new();
    let mut ops = Vec::new();
    for rendered in render_plan(plan, old, new, dialect)? {
        if phase_of(&rendered.op)? != phase {
            continue;
        }
        statements.extend(rendered.statements);
        for warning in rendered.warnings {
            if !warnings.contains(&warning) {
                warnings.push(warning);
            }
        }
        ops.push(rendered.op);
    }
    let headers = Headers {
        destructive: !statements.is_empty() && removes_something(&ops.iter().collect::<Vec<_>>()),
        not_applicable: statements.is_empty(),
        ..Headers::default()
    };
    let text = step_text(&headers, &statements);
    Ok((headers, text))
}

/// The short name of a model (`Author` for `myapp.models.Author`).
fn short_model_name(model_name: &str) -> &str {
    model_name.rsplit('.').next().unwrap_or(model_name)
}

/// What the up plans change, in words, over every dialect.
fn summarize(
    plans: &[MigrationPlan],
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
    for op in plans.iter().flat_map(|plan| &plan.operations) {
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
            _ => {}
        }
    }
    [
        ("new models", added),
        ("dropped models", dropped),
        ("new enum types", types_added),
        ("dropped enum types", types_dropped),
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
/// [`GenerateError::NotGeneratedYet`] for an op this slice does not generate,
/// [`GenerateError::Unplanned`] for a change the planner reports but has no
/// op for, [`GenerateError::Render`] when an op cannot render.
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

    let mut ups = Vec::new();
    let mut downs = Vec::new();
    for &dialect in dialects {
        let up = plan(parent_ir, target, dialect);
        refuse_unsupported(&up, &plan(target, target, dialect), dialect)?;
        let down = plan(target, parent_ir, dialect);
        refuse_unsupported(&down, &plan(parent_ir, parent_ir, dialect), dialect)?;
        ups.push(up);
        downs.push(down);
    }
    if ups.iter().chain(&downs).all(MigrationPlan::is_empty) {
        return Ok(None);
    }

    let phases: BTreeSet<Phase> = ups
        .iter()
        .chain(&downs)
        .flat_map(|plan| &plan.operations)
        .map(phase_of)
        .collect::<Result<_, _>>()?;
    let mut warnings = Vec::new();
    let mut steps = Vec::new();
    for (ordinal, phase) in (1u8..).zip(phases) {
        let mut renderings = BTreeMap::new();
        for ((&dialect, up), down) in dialects.iter().zip(&ups).zip(&downs) {
            let (headers, up_text) =
                render_file(up, phase, parent_ir, target, dialect, &mut warnings)?;
            let (down_headers, down_text) =
                render_file(down, phase, target, parent_ir, dialect, &mut warnings)?;
            renderings.insert(
                StepDialect::from(dialect),
                Rendering {
                    up: up_text,
                    down: down_text,
                    headers,
                    down_headers,
                },
            );
        }
        steps.push(GeneratedStep {
            ordinal,
            name: phase.step_name().to_string(),
            kind: StepKind::Ddl,
            renderings,
        });
    }

    let bytes = Snapshot::store(target, parent.map(|snapshot| snapshot.checksum));
    let snapshot = Snapshot::load(&bytes)?;
    let snapshot_json = String::from_utf8(bytes).map_err(|err| {
        GenerateError::Render(format!("the generated snapshot is not UTF-8: {err}"))
    })?;
    Ok(Some(GeneratedMigration {
        steps,
        snapshot,
        snapshot_json,
        summary: summarize(&ups, parent_ir, target),
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

    fn ir(models: Vec<SchemaModel>) -> IrEnvelope<SchemaIrPayload> {
        IrEnvelope {
            ir_kind: "schema".into(),
            ir_version: 1,
            payload: SchemaIrPayload {
                dialect_agnostic: true,
                models,
            },
        }
    }

    fn column(name: &str, logical_type: &str) -> SchemaColumn {
        SchemaColumn {
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
        }
    }

    fn pk() -> SchemaColumn {
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

    fn model(name: &str, columns: Vec<SchemaColumn>) -> SchemaModel {
        SchemaModel {
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

    fn author() -> SchemaModel {
        model(
            "Author",
            vec![pk(), column("name", "string"), status(&["draft", "live"])],
        )
    }

    fn post() -> SchemaModel {
        SchemaModel {
            foreign_keys: vec![SchemaForeignKey {
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
        Snapshot::load(&Snapshot::store(ir, parent.map(|p| p.checksum))).expect("load")
    }

    /// Every statement the create pass executes for `model`, in its order.
    fn create_pass(model: &SchemaModel, dialect: Dialect) -> Vec<String> {
        let emission = render_create_table(model, dialect).expect("create");
        let mut out = emission.pre_create_sqls;
        out.push(emission.create_sql);
        out.extend(emission.post_create_sqls);
        out
    }

    fn file(statements: &[String], headers: &str) -> String {
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
            assert!(r.down_headers.destructive);
        }
        let pg = rendering(&migration, StepDialect::Postgres);
        assert_eq!(
            pg.down,
            "-- ferro: destructive\n\nDROP TABLE \"author\";\n\nDROP TYPE \"status\";\n"
        );
        assert_eq!(
            rendering(&migration, StepDialect::Sqlite).down,
            "-- ferro: destructive\n\nDROP TABLE \"author\";\n"
        );
        assert_eq!(migration.snapshot.parent_checksum, None);
        assert_eq!(migration.snapshot.ir, target);
        assert_eq!(
            migration.snapshot_json.as_bytes(),
            Snapshot::store(&target, None).as_slice()
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
        assert_eq!(pg.down, "-- ferro: destructive\n\nDROP TABLE \"post\";\n");
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
        // drops the table the up created.
        let mut down = create_pass(&author(), Dialect::Postgres);
        down.extend(create_pass(&post(), Dialect::Postgres));
        down.push("DROP TABLE \"tag\"".to_string());
        assert_eq!(pg.down, file(&down, "-- ferro: destructive\n"));
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

    #[test]
    fn a_column_change_on_an_existing_table_is_refused_naming_its_ticket() {
        let parent = snapshot_of(&ir(vec![author()]), None);
        let mut edited = author();
        edited.columns.push(SchemaColumn {
            nullable: true,
            ..column("bio", "string")
        });
        let err = generate(Some(&parent), &ir(vec![edited]), &BOTH).expect_err("refused");
        assert_eq!(
            err.to_string(),
            "not generated yet: AddColumn on author (ticket #524)"
        );
    }

    #[test]
    fn an_enum_label_change_is_refused_naming_its_ticket() {
        let parent = snapshot_of(&ir(vec![author()]), None);
        let mut added = author();
        added.columns[2] = status(&["draft", "live", "archived"]);
        let err = generate(Some(&parent), &ir(vec![added]), &BOTH).expect_err("refused");
        assert_eq!(
            err.to_string(),
            "not generated yet: AddEnumLabel on status (ticket #529)"
        );

        let mut removed = author();
        removed.columns[2] = status(&["draft"]);
        let err = generate(Some(&parent), &ir(vec![removed]), &BOTH).expect_err("refused");
        assert!(err.to_string().starts_with("not generated yet:"), "{err}");
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

    #[test]
    fn a_dialect_with_no_work_gets_the_one_line_not_applicable_file() {
        let mut warnings = Vec::new();
        let (headers, text) = render_file(
            &MigrationPlan::default(),
            Phase::Schema,
            &ir(vec![]),
            &ir(vec![]),
            Dialect::Sqlite,
            &mut warnings,
        )
        .expect("render");
        assert!(headers.not_applicable);
        assert_eq!(text, "-- ferro: not-applicable\n");
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
