//! Expand → backfill → contract (ADR-0040, ADR-0042, ADR-0043): a change
//! that asks the rows a table already holds for values no statement can
//! supply.
//!
//! ```text
//! class Author(Model):                 0012_author_slug/
//!     slug: str   # new, required  ──▶   01_expand          ADD COLUMN "slug" varchar   (nullable)
//!                                        02_backfill_author @chunked(…slug == None…)  author.slug = todo(…)
//!                                        03_add_constraint  ADD CONSTRAINT "_ferro_notnull_author_slug"
//!                                                             CHECK ("slug" IS NOT NULL) NOT VALID   (Postgres)
//!                                        04_contract        VALIDATE …; SET NOT NULL; DROP CONSTRAINT …
//!                                                           (SQLite: the table rebuilt to the target shape)
//! ```
//!
//! [`columns::assign`] decides *that* an op demands values
//! ([`columns::demands_values`]); this module decides what follows from it:
//! the [`Demand`] (why, and whether the backfill pages by keyset), the
//! migration's intermediate shape with every demanded column nullable
//! ([`relaxed`]), and the steps after the backfill — the Postgres staged
//! `NOT NULL` ([`add_constraint_step`], named [`staged_not_null_name`], never
//! `ck_*`, so check reconciliation and the drift check never claim it) and the
//! contract ([`contract_step`]), which also validates every foreign key and
//! check the expand staged `NOT VALID` (no separate validate step). The data
//! steps carry their scaffold's inputs ([`DataStep`]); the Python side writes
//! the file.

use super::columns::{self, PlanContext, PlanDirection};
use super::staging::StagedConstraint;
use super::{GenerateError, GeneratedStep, Rendering, enums, find_model, rebuild, step_text};
use crate::directory::{Headers, StepDialect, StepKind};
use crate::order::order_by_dependencies;
use crate::plan::enum_declaration;
use crate::{Dialect, MigrationOp, MigrationPlan, render_plan};
use ferro_ddl_lowering::{
    ResolvedStorage, quote_ident, quote_label, render_drop_constraint, render_validate_constraint,
    resolve_column_storage,
};
use ferro_schema_ir::{IrEnvelope, SchemaIrPayload, SchemaModel};
use std::collections::BTreeMap;

/// Why the rows a table holds need a value for a column.
///
/// On the wire (the scaffold's input): `{"kind": "no_default"}`,
/// `{"kind": "default_factory", "factory": "uuid.uuid4"}`,
/// `{"kind": "label_removed", "type_name": "orderstatus", "label": "canceled"}`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Reason {
    /// A3: a new required column with no default.
    NoDefault,
    /// A2b: a new required column whose field declares a Python
    /// `default_factory` (its `module.qualname`), which never reaches DDL.
    DefaultFactory(String),
    /// A7a: an existing nullable column made required.
    NowNotNull,
    /// C1: a new required foreign-key column.
    RequiredFk,
    /// D2 (#536): the column's enum dropped `label`, which its rows may
    /// still hold; the backfill gives each such row another label.
    LabelRemoved {
        /// The enum type.
        type_name: String,
        /// The label removed.
        label: String,
    },
}

impl serde::Serialize for Reason {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        use serde::ser::SerializeMap;
        let mut map = serializer.serialize_map(None)?;
        match self {
            Reason::NoDefault => map.serialize_entry("kind", "no_default")?,
            Reason::DefaultFactory(factory) => {
                map.serialize_entry("kind", "default_factory")?;
                map.serialize_entry("factory", factory)?;
            }
            Reason::NowNotNull => map.serialize_entry("kind", "now_not_null")?,
            Reason::RequiredFk => map.serialize_entry("kind", "required_fk")?,
            Reason::LabelRemoved { type_name, label } => {
                map.serialize_entry("kind", "label_removed")?;
                map.serialize_entry("type_name", type_name)?;
                map.serialize_entry("label", label)?;
            }
        }
        map.end()
    }
}

impl Reason {
    /// Whether the rows hold `NULL` where they need a value (every reason
    /// but a removed label, whose rows hold the label).
    pub fn fills_nulls(&self) -> bool {
        !matches!(self, Reason::LabelRemoved { .. })
    }
}

/// How a generated backfill runs (ADR-0024).
#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Driver {
    /// Paged by keyset over the model's one primary key.
    Chunked,
    /// One transaction: the model has no single primary key to page by.
    Atomic,
}

/// One column whose existing rows need a value.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Demand {
    /// The table.
    pub table: String,
    /// The column.
    pub column: String,
    /// Why.
    pub reason: Reason,
    /// How the table's backfill runs.
    pub driver: Driver,
}

fn driver_of(model: &SchemaModel) -> Driver {
    if model.columns.iter().filter(|col| col.primary_key).count() == 1 {
        Driver::Chunked
    } else {
        Driver::Atomic
    }
}

/// The demand `op` makes in the up file turning `before` into `after`, or
/// `None` when it asks the existing rows for nothing.
pub fn demands(
    op: &MigrationOp,
    before: &IrEnvelope<SchemaIrPayload>,
    after: &IrEnvelope<SchemaIrPayload>,
) -> Option<Demand> {
    // Whether an op demands values is a property of the models, never of the
    // dialect: every dialect's context answers the same.
    let ctx = PlanContext::of(op, before, after, Dialect::Postgres, PlanDirection::Up);
    if !columns::demands_values(op, &ctx) {
        return None;
    }
    let model = ctx.after?;
    let (column, reason) = match op {
        MigrationOp::AddColumn { column, .. } => {
            let col = model.columns.iter().find(|col| &col.name == column)?;
            let reason = if model.foreign_keys.iter().any(|fk| &fk.column == column) {
                Reason::RequiredFk
            } else if let Some(factory) = &col.default_factory {
                Reason::DefaultFactory(factory.clone())
            } else {
                Reason::NoDefault
            };
            (column, reason)
        }
        MigrationOp::AlterColumnNullability { column, .. } => (column, Reason::NowNotNull),
        _ => return None,
    };
    Some(Demand {
        table: model.table_name.clone(),
        column: column.clone(),
        reason,
        driver: driver_of(model),
    })
}

/// The demands a [`MigrationOp::RemoveEnumLabel`] makes in the up file
/// turning `before` into `after` (D2): one per column of the type whose rows
/// may hold the label — a column `before` already declares with the type (a
/// column the same file adds holds no row yet). Empty for any other op.
pub fn label_demands(
    op: &MigrationOp,
    before: &IrEnvelope<SchemaIrPayload>,
    after: &IrEnvelope<SchemaIrPayload>,
) -> Vec<Demand> {
    let MigrationOp::RemoveEnumLabel {
        type_name,
        label,
        columns,
    } = op
    else {
        return Vec::new();
    };
    columns
        .iter()
        .filter_map(|(table, column)| {
            let held = find_model(before, table)?
                .columns
                .iter()
                .find(|col| &col.name == column)
                .and_then(enum_declaration)
                .is_some_and(|(declared, _)| &declared == type_name);
            let model = find_model(after, table)?;
            held.then(|| Demand {
                table: table.clone(),
                column: column.clone(),
                reason: Reason::LabelRemoved {
                    type_name: type_name.clone(),
                    label: label.clone(),
                },
                driver: driver_of(model),
            })
        })
        .collect()
}

/// Every demand `ops` make (an op per dialect's plan may repeat one), each
/// once, tables in foreign-key order (parents first, as `after` declares
/// them), columns in the order the model declares them; a column's removed
/// labels in the order the plan removes them.
pub fn collect(
    ops: &[MigrationOp],
    before: &IrEnvelope<SchemaIrPayload>,
    after: &IrEnvelope<SchemaIrPayload>,
) -> Vec<Demand> {
    let mut found: Vec<Demand> = Vec::new();
    for op in ops {
        for demand in demands(op, before, after)
            .into_iter()
            .chain(label_demands(op, before, after))
        {
            if !found.contains(&demand) {
                found.push(demand);
            }
        }
    }
    let models: Vec<&SchemaModel> = order_by_dependencies(
        after.payload.models.iter().collect(),
        |model| model.table_name.clone(),
        |model| {
            model
                .foreign_keys
                .iter()
                .map(|fk| fk.to_table.clone())
                .collect()
        },
    );
    let mut ordered = Vec::with_capacity(found.len());
    for model in models {
        for col in &model.columns {
            ordered.extend(
                found
                    .iter()
                    .filter(|d| d.table == model.table_name && d.column == col.name)
                    .cloned(),
            );
        }
    }
    ordered
}

/// `ir` with each `(table, column)` of `columns` declared nullable.
pub fn relax_columns(
    ir: &IrEnvelope<SchemaIrPayload>,
    columns: &[(String, String)],
) -> IrEnvelope<SchemaIrPayload> {
    let mut relaxed = ir.clone();
    for model in &mut relaxed.payload.models {
        for col in &mut model.columns {
            if columns
                .iter()
                .any(|(table, column)| *table == model.table_name && *column == col.name)
            {
                col.nullable = true;
            }
        }
    }
    relaxed
}

/// `ir` with every column a demand fills `NULL`s of nullable: the schema
/// between the expand and the contract, which the backfill fills in.
pub fn relaxed(
    ir: &IrEnvelope<SchemaIrPayload>,
    demands: &[Demand],
) -> IrEnvelope<SchemaIrPayload> {
    let columns: Vec<(String, String)> = demands
        .iter()
        .filter(|d| d.reason.fills_nulls())
        .map(|d| (d.table.clone(), d.column.clone()))
        .collect();
    relax_columns(ir, &columns)
}

/// Each `(type, label)` a [`MigrationOp::RemoveEnumLabel`] of `ops` removes,
/// once, in plan order — whether or not a row can hold it (a type only new
/// columns carry still swaps on Postgres).
pub fn label_removals(ops: &[MigrationOp]) -> Vec<(String, String)> {
    let mut out: Vec<(String, String)> = Vec::new();
    for op in ops {
        if let MigrationOp::RemoveEnumLabel {
            type_name, label, ..
        } = op
        {
            let pair = (type_name.clone(), label.clone());
            if !out.contains(&pair) {
                out.push(pair);
            }
        }
    }
    out
}

/// `ir` with every label of `removals` still declared, at its place in
/// `before`'s declaration, on every column of its type: the schema between
/// the backfill and the contract, where a row may still hold the label (the
/// historical model's union, ADR-0025). Labels `ir` adds follow `before`'s.
pub fn with_removed_labels(
    ir: &IrEnvelope<SchemaIrPayload>,
    before: &IrEnvelope<SchemaIrPayload>,
    removals: &[(String, String)],
) -> IrEnvelope<SchemaIrPayload> {
    let mut restored = ir.clone();
    if removals.is_empty() {
        return restored;
    }
    let old_labels = |type_name: &str| {
        before
            .payload
            .models
            .iter()
            .flat_map(|model| &model.columns)
            .find_map(|col| enum_declaration(col).filter(|(declared, _)| declared == type_name))
            .map(|(_, labels)| labels)
            .unwrap_or_default()
    };
    for model in &mut restored.payload.models {
        let mut restored_checks: Vec<(String, Vec<String>, Vec<String>)> = Vec::new();
        for col in &mut model.columns {
            let Some((type_name, labels)) = enum_declaration(col) else {
                continue;
            };
            let removed: Vec<&String> = removals
                .iter()
                .filter(|(t, _)| *t == type_name)
                .map(|(_, label)| label)
                .collect();
            if removed.is_empty() {
                continue;
            }
            let old = old_labels(&type_name);
            let union: Vec<String> = old
                .iter()
                .filter(|label| labels.contains(label) || removed.contains(label))
                .chain(labels.iter().filter(|label| !old.contains(label)))
                .cloned()
                .collect();
            col.enum_values = Some(
                union
                    .iter()
                    .map(|label| serde_json::Value::String(label.clone()))
                    .collect(),
            );
            restored_checks.push((col.name.clone(), labels, union));
        }
        // A `db_check` over the column lists one literal per label (the IR
        // compiler's `IN (…)`): the removed labels are allowed again too.
        for (column, labels, union) in restored_checks {
            for check in &mut model.checks {
                if check.column != column || check.values.len() != labels.len() {
                    continue;
                }
                check.values = union
                    .iter()
                    .map(|label| match labels.iter().position(|l| l == label) {
                        Some(at) => check.values[at].clone(),
                        None => quote_label(label),
                    })
                    .collect();
            }
        }
    }
    restored
}

/// The prefix of every [`staged_not_null_name`]: what the runner reads a
/// contract's failed `VALIDATE` off (`src/errors.rs`).
pub const STAGED_NOT_NULL_PREFIX: &str = "_ferro_notnull_";

/// The temporary check that stages `NOT NULL` on Postgres (ADR-0042):
/// `_ferro_notnull_<table>_<column>`, guarded at 63 characters like every
/// ferro name. Deliberately not `ck_*`: check reconciliation and the drift
/// check own only `ck_*` names, so a run stopped between the add-constraint
/// step and the contract leaves a check neither reads as a dropped
/// declaration.
pub fn staged_not_null_name(table: &str, column: &str) -> String {
    let raw = format!("{STAGED_NOT_NULL_PREFIX}{table}_{column}");
    if raw.chars().count() > 63 {
        return format!("{}_nn", raw.chars().take(60).collect::<String>());
    }
    raw
}

/// `ALTER TABLE "<table>" ADD CONSTRAINT "_ferro_notnull_…" CHECK ("<col>"
/// IS NOT NULL) NOT VALID`.
fn render_staged_not_null(demand: &Demand) -> String {
    format!(
        "ALTER TABLE {} ADD CONSTRAINT {} CHECK ({} IS NOT NULL) NOT VALID",
        quote_ident(&demand.table),
        quote_ident(&staged_not_null_name(&demand.table, &demand.column)),
        quote_ident(&demand.column),
    )
}

/// One column a data step fills, and why.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize)]
pub struct DemandedColumn {
    /// The column.
    pub name: String,
    /// Why its existing rows need a value.
    pub reason: Reason,
}

/// What a generated data step's scaffold is written from: the Python side
/// (`ferro.migrations.backfill_scaffold`) renders the file.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize)]
pub struct DataStep {
    /// The model's class name (`Author`), as `ctx.models` names it.
    pub model: String,
    /// Its table.
    pub table: String,
    /// The columns it fills, in order.
    pub columns: Vec<DemandedColumn>,
    /// How it runs.
    pub driver: Driver,
    /// The model's one primary-key column, which a chunked backfill pages
    /// by; `None` for an atomic one.
    pub key: Option<String>,
    /// The reason its `down` declares `@nothing_to_reverse` (ADR-0033: a
    /// generated down is never irreversible by default).
    pub reverse: String,
    /// The step is the `--no-backfill` guard (`guard_<model>`, ADR-0037):
    /// it checks that no row needs a value instead of writing one.
    pub guard: bool,
}

/// `--no-backfill <table>.<column>`: a column whose backfill the developer
/// declares unneeded, each `(table, column)`.
pub type NoBackfill = [(String, String)];

/// The refusal of a `--no-backfill` the migration cannot honour: a column
/// nothing backfills, or only some of one model's columns (one model has
/// one data step).
fn refuse_no_backfill(demands: &[Demand], skipped: &NoBackfill) -> Result<(), GenerateError> {
    let shown = |pairs: &[(&str, &str)]| {
        pairs
            .iter()
            .map(|(t, c)| format!("{t}.{c}"))
            .collect::<Vec<_>>()
            .join(", ")
    };
    let unknown: Vec<(&str, &str)> = skipped
        .iter()
        .filter(|(t, c)| !demands.iter().any(|d| &d.table == t && &d.column == c))
        .map(|(t, c)| (t.as_str(), c.as_str()))
        .collect();
    if !unknown.is_empty() {
        let demanded: Vec<(&str, &str)> = demands
            .iter()
            .map(|d| (d.table.as_str(), d.column.as_str()))
            .collect();
        let listed = if demanded.is_empty() {
            "none: the models change nothing that asks existing rows for a value".to_string()
        } else {
            shown(&demanded)
        };
        return Err(GenerateError::NoBackfill(format!(
            "--no-backfill {}: no generated backfill fills that column; the columns this \
             migration backfills: {listed}",
            shown(&unknown)
        )));
    }
    for demand in demands {
        let mine: Vec<&Demand> = demands.iter().filter(|d| d.table == demand.table).collect();
        let (named, rest): (Vec<&Demand>, Vec<&Demand>) = mine
            .into_iter()
            .partition(|d| skipped.iter().any(|(t, c)| *t == d.table && *c == d.column));
        if !named.is_empty() && !rest.is_empty() {
            let pairs = |ds: &[&Demand]| {
                ds.iter()
                    .map(|d| format!("{}.{}", d.table, d.column))
                    .collect::<Vec<_>>()
                    .join(", ")
            };
            let columns: Vec<&str> = rest.iter().map(|d| d.column.as_str()).collect();
            return Err(GenerateError::NoBackfill(format!(
                "--no-backfill {}: {} also needs a value for {} in this migration, and one \
                 model has one data step; add --no-backfill for {} too, or for none of them",
                pairs(&named),
                demand.table,
                columns.join(", "),
                pairs(&rest)
            )));
        }
    }
    Ok(())
}

/// One data step per table `demands` name, in their (foreign-key) order,
/// with its scaffold's inputs and no rendering: `backfill_<model>`, or, when
/// `skipped` names every one of the table's demanded columns, the guard
/// `guard_<model>` (ADR-0037). The `reverse` reason names the expand step
/// once the caller numbers the steps ([`name_reverse`]).
///
/// # Errors
/// [`GenerateError::NoBackfill`] for a `skipped` column nothing backfills,
/// or one naming only some of a table's demanded columns.
pub fn data_steps(
    demands: &[Demand],
    target: &IrEnvelope<SchemaIrPayload>,
    skipped: &NoBackfill,
) -> Result<Vec<GeneratedStep>, GenerateError> {
    refuse_no_backfill(demands, skipped)?;
    let mut tables: Vec<&str> = Vec::new();
    for demand in demands {
        if !tables.contains(&demand.table.as_str()) {
            tables.push(&demand.table);
        }
    }
    Ok(tables
        .into_iter()
        .map(|table| {
            let mine: Vec<&Demand> = demands.iter().filter(|d| d.table == table).collect();
            let declared = find_model(target, table);
            let model = declared
                .map(|model| super::short_model_name(&model.model_name).to_string())
                .unwrap_or_else(|| table.to_string());
            let key = declared
                .filter(|model| driver_of(model) == Driver::Chunked)
                .and_then(|model| model.columns.iter().find(|col| col.primary_key))
                .map(|col| col.name.clone());
            let guard = mine
                .iter()
                .all(|d| skipped.iter().any(|(t, c)| *t == d.table && *c == d.column));
            let kind = if guard { "guard" } else { "backfill" };
            GeneratedStep {
                ordinal: 0,
                name: format!("{kind}_{}", model.to_lowercase()),
                kind: StepKind::Data,
                renderings: BTreeMap::new(),
                data: Some(DataStep {
                    model,
                    table: table.to_string(),
                    columns: mine
                        .iter()
                        .map(|d| DemandedColumn {
                            name: d.column.clone(),
                            reason: d.reason.clone(),
                        })
                        .collect(),
                    driver: mine[0].driver,
                    key,
                    reverse: String::new(),
                    guard,
                }),
            }
        })
        .collect())
}

/// Fill each data step's `reverse` reason once `steps` are numbered: a
/// column the expand added goes with `NN_expand.down.sql`; a column made
/// required was nullable before, and the values written into it stay.
pub fn name_reverse(steps: &mut [GeneratedStep]) {
    let expand = steps
        .iter()
        .find(|step| step.name == "expand")
        .map(|step| format!("{:02}_expand.down.sql", step.ordinal));
    for step in steps.iter_mut() {
        let Some(data) = step.data.as_mut() else {
            continue;
        };
        let (relabelled, filled): (Vec<&DemandedColumn>, Vec<&DemandedColumn>) = data
            .columns
            .iter()
            .partition(|col| !col.reason.fills_nulls());
        let (made_required, added): (Vec<&DemandedColumn>, Vec<&DemandedColumn>) = filled
            .into_iter()
            .partition(|col| col.reason == Reason::NowNotNull);
        let mut clauses = Vec::new();
        if !relabelled.is_empty() {
            let labels = if relabelled.len() == 1 {
                "the label"
            } else {
                "the labels"
            };
            clauses.push(format!("the contract's down restores {labels}"));
        }
        if let (Some(expand), false) = (&expand, added.is_empty()) {
            let what = if added.len() == 1 {
                "the column"
            } else {
                "the columns"
            };
            clauses.push(format!("{expand} drops {what}"));
        }
        if !made_required.is_empty() {
            let names: Vec<&str> = made_required.iter().map(|c| c.name.as_str()).collect();
            clauses.push(format!(
                "{} was nullable before this migration, so the values written stay",
                names.join(", ")
            ));
        }
        data.reverse = clauses.join("; ");
    }
}

fn rendering(up: (Vec<String>, Headers), down: (Vec<String>, Headers)) -> Rendering {
    let (up_statements, mut up_headers) = up;
    let (down_statements, mut down_headers) = down;
    if up_statements.is_empty() {
        up_headers = Headers {
            not_applicable: true,
            ..Headers::default()
        };
    }
    if down_statements.is_empty() {
        down_headers = Headers {
            not_applicable: true,
            ..Headers::default()
        };
    }
    Rendering {
        up: step_text(&up_headers, &up_statements),
        down: step_text(&down_headers, &down_statements),
        headers: up_headers,
        down_headers,
    }
}

/// The add-constraint step (ADR-0042): on Postgres each demanded column's
/// [`staged_not_null_name`] check added `NOT VALID` (instant, and refusing a
/// `NULL` on every write from its commit on), its down dropping it; on
/// SQLite the one-line `-- ferro: not-applicable` both ways. `None` when no
/// configured dialect stages (a SQLite-only project), or nothing is demanded.
pub fn add_constraint_step(demands: &[Demand], dialects: &[Dialect]) -> Option<GeneratedStep> {
    let demands: Vec<Demand> = demands
        .iter()
        .filter(|d| d.reason.fills_nulls())
        .cloned()
        .collect();
    let demands = demands.as_slice();
    if demands.is_empty() || !dialects.contains(&Dialect::Postgres) {
        return None;
    }
    let renderings = dialects
        .iter()
        .map(|&dialect| {
            let (up, down) = match dialect {
                Dialect::Postgres => (
                    demands.iter().map(render_staged_not_null).collect(),
                    demands
                        .iter()
                        .map(|d| {
                            render_drop_constraint(
                                &d.table,
                                &staged_not_null_name(&d.table, &d.column),
                            )
                        })
                        .collect(),
                ),
                Dialect::Sqlite => (Vec::new(), Vec::new()),
            };
            (
                StepDialect::from(dialect),
                rendering((up, Headers::default()), (down, Headers::default())),
            )
        })
        .collect();
    Some(GeneratedStep {
        ordinal: 0,
        name: "add_constraint".to_string(),
        kind: StepKind::Ddl,
        renderings,
        data: None,
    })
}

/// The statements one nullability op renders from `old` to `new` on
/// Postgres (`SET NOT NULL` / `DROP NOT NULL`), as the pass renders them.
fn nullability(
    demand: &Demand,
    old: &IrEnvelope<SchemaIrPayload>,
    new: &IrEnvelope<SchemaIrPayload>,
) -> Result<Vec<String>, GenerateError> {
    let plan = MigrationPlan {
        operations: vec![MigrationOp::AlterColumnNullability {
            table: demand.table.clone(),
            column: demand.column.clone(),
        }],
        ..MigrationPlan::default()
    };
    let rendered = render_plan(&plan, old, new, Dialect::Postgres)?;
    super::refuse_unrendered(&rendered, Dialect::Postgres)?;
    Ok(rendered.into_iter().flat_map(|op| op.statements).collect())
}

/// The contract step: `relaxed_target` (every demanded column nullable, as
/// the index steps leave the schema) into `target`.
///
/// On Postgres (ADR-0042, ADR-0043) it validates each staged `NOT NULL`
/// check — first, so a row written behind the backfill's cursor fails here
/// and is counted with the recipe — then each foreign key and check the
/// expand staged `NOT VALID` (`staged`), sets each column `NOT NULL`
/// (Postgres trusts the validated check and does not scan again) and drops
/// the staging checks. Its down puts the checks back `NOT VALID` and drops
/// `NOT NULL`, then un-validates each staged constraint the only way there is
/// (drop and add `NOT VALID`), reaching the add-constraint step's state.
///
/// On SQLite (ADR-0034, ADR-0046) it rebuilds each demanded table into the
/// target shape, whose copy fails on a row that still holds `NULL`; its down
/// rebuilds it back.
///
/// A removed enum label (D2) is the rest of `relaxed_target` → `target`
/// ([`label_contract`]): on Postgres the swap-type recipe for a native type
/// and the pass's own statements for the rest (a text enum's check rebuilt
/// to the new labels), the down putting each label back; on SQLite a rebuild
/// only of a table whose shape changes (its check, or a column narrowed to
/// the longest label left), else nothing.
///
/// # Errors
/// A demanded table missing from either side, or a statement that does not
/// render.
pub fn contract_step(
    demands: &[Demand],
    staged: &[StagedConstraint],
    relaxed_target: &IrEnvelope<SchemaIrPayload>,
    target: &IrEnvelope<SchemaIrPayload>,
    dialects: &[Dialect],
) -> Result<GeneratedStep, GenerateError> {
    let demands: Vec<Demand> = demands
        .iter()
        .filter(|d| d.reason.fills_nulls())
        .cloned()
        .collect();
    let demands = demands.as_slice();
    let mut renderings = BTreeMap::new();
    for &dialect in dialects {
        let rendering = match dialect {
            Dialect::Postgres => {
                let mut up = Vec::new();
                let mut down = Vec::new();
                for d in demands {
                    up.push(render_validate_constraint(
                        &d.table,
                        &staged_not_null_name(&d.table, &d.column),
                    ));
                }
                for c in staged {
                    up.push(render_validate_constraint(&c.table, &c.name));
                }
                for d in demands {
                    up.extend(nullability(d, relaxed_target, target)?);
                }
                for d in demands {
                    up.push(render_drop_constraint(
                        &d.table,
                        &staged_not_null_name(&d.table, &d.column),
                    ));
                }
                let (labels_up, labels_down) = label_contract(relaxed_target, target)?;
                up.extend(labels_up);
                down.extend(labels_down);
                for d in demands {
                    down.push(render_staged_not_null(d));
                }
                for d in demands {
                    down.extend(nullability(d, target, relaxed_target)?);
                }
                for c in staged {
                    down.push(render_drop_constraint(&c.table, &c.name));
                    down.extend(c.add.iter().cloned());
                }
                let up_headers = Headers {
                    data_dependent: true,
                    ..Headers::default()
                };
                rendering((up, up_headers), (down, Headers::default()))
            }
            Dialect::Sqlite => {
                let mut tables: Vec<String> = Vec::new();
                for d in demands {
                    if !tables.contains(&d.table) {
                        tables.push(d.table.clone());
                    }
                }
                // A table a removed label reshapes (its check, or a column
                // narrowed to the longest label left).
                for op in super::plan(relaxed_target, target, Dialect::Sqlite)?.operations {
                    if let Some(table) = op.table()
                        && !tables.iter().any(|t| t == table)
                    {
                        tables.push(table.to_string());
                    }
                }
                let mut up = Vec::new();
                let mut down = Vec::new();
                for table in &tables {
                    let side = |ir, which: &str| {
                        find_model(ir, table).ok_or_else(|| {
                            GenerateError::Render(format!(
                                "cannot contract table '{table}': it is missing from the \
                                 {which} snapshot"
                            ))
                        })
                    };
                    let (loose, strict) =
                        (side(relaxed_target, "expanded")?, side(target, "target")?);
                    up.extend(rebuild::render(table, strict, loose, &[])?);
                    down.extend(rebuild::render(table, loose, strict, &[])?);
                }
                let up_headers = Headers {
                    foreign_keys_off: true,
                    data_dependent: true,
                    ..Headers::default()
                };
                let down_headers = Headers {
                    foreign_keys_off: true,
                    ..Headers::default()
                };
                rendering((up, up_headers), (down, down_headers))
            }
        };
        renderings.insert(StepDialect::from(dialect), rendering);
    }
    Ok(GeneratedStep {
        ordinal: 0,
        name: "contract".to_string(),
        kind: StepKind::Ddl,
        renderings,
        data: None,
    })
}

/// The removed labels' half of the Postgres contract, up and down: every op
/// planning `relaxed_target` → `target` but the nullability the staged
/// `NOT NULL` owns. A native type's removals are one swap per type
/// ([`enums::render_swap_type`], over every column the target stores as that
/// type); anything else (a text enum's check rebuilt to the labels left) is
/// the pass's statement, added validated. The down is the planner run back:
/// `ADD VALUE IF NOT EXISTS` for each label of a native type (ADR-0011's
/// statement) and the check rebuilt over the labels again.
fn label_contract(
    relaxed_target: &IrEnvelope<SchemaIrPayload>,
    target: &IrEnvelope<SchemaIrPayload>,
) -> Result<(Vec<String>, Vec<String>), GenerateError> {
    let rest = |ops: Vec<MigrationOp>| -> Vec<MigrationOp> {
        ops.into_iter()
            .filter(|op| {
                !matches!(
                    op,
                    MigrationOp::AlterColumnNullability { .. }
                        | MigrationOp::RemoveEnumLabel { .. }
                )
            })
            .collect()
    };
    let rendered = |ops: Vec<MigrationOp>, old, new| -> Result<Vec<String>, GenerateError> {
        let plan = MigrationPlan {
            operations: ops,
            ..MigrationPlan::default()
        };
        let rendered = render_plan(&plan, old, new, Dialect::Postgres)?;
        super::refuse_unrendered(&rendered, Dialect::Postgres)?;
        Ok(rendered.into_iter().flat_map(|op| op.statements).collect())
    };
    let forward = super::plan(relaxed_target, target, Dialect::Postgres)?.operations;
    let mut up = Vec::new();
    let mut swapped: Vec<&str> = Vec::new();
    for op in &forward {
        let MigrationOp::RemoveEnumLabel {
            type_name, columns, ..
        } = op
        else {
            continue;
        };
        if swapped.contains(&type_name.as_str()) {
            continue;
        }
        swapped.push(type_name);
        let mut labels_after = Vec::new();
        let mut native = Vec::new();
        for (table, column) in columns {
            let Some(col) = find_model(target, table)
                .and_then(|model| model.columns.iter().find(|col| &col.name == column))
            else {
                continue;
            };
            if let Ok(ResolvedStorage::PgEnum { labels, .. }) =
                resolve_column_storage(col, Dialect::Postgres)
            {
                labels_after = labels;
                native.push(enums::SwapColumn {
                    table: table.clone(),
                    column: column.clone(),
                    default: col
                        .default
                        .as_ref()
                        .and_then(serde_json::Value::as_str)
                        .map(str::to_string),
                });
            }
        }
        if !native.is_empty() {
            up.extend(enums::render_swap_type(type_name, &labels_after, &native));
        }
    }
    up.extend(rendered(rest(forward), relaxed_target, target)?);
    let backward = super::plan(target, relaxed_target, Dialect::Postgres)?.operations;
    let down = rendered(rest(backward), target, relaxed_target)?;
    Ok((up, down))
}

#[cfg(test)]
mod tests {
    use super::super::tests::{column, ir, model, pk};
    use super::*;
    use ferro_schema_ir::{SchemaColumn, SchemaForeignKey};

    fn slug_demand(table: &str, column: &str) -> Demand {
        Demand {
            table: table.into(),
            column: column.into(),
            reason: Reason::NoDefault,
            driver: Driver::Chunked,
        }
    }

    fn skip(pairs: &[(&str, &str)]) -> Vec<(String, String)> {
        pairs
            .iter()
            .map(|(t, c)| (t.to_string(), c.to_string()))
            .collect()
    }

    #[test]
    fn no_backfill_over_every_demanded_column_makes_the_step_the_guard() {
        let target = ir(vec![author(vec![column("slug", "string")])]);
        let demands = [slug_demand("author", "slug")];
        let steps = data_steps(&demands, &target, &skip(&[("author", "slug")])).expect("ok");
        assert_eq!(steps.len(), 1);
        assert_eq!(steps[0].name, "guard_author");
        assert!(steps[0].data.as_ref().is_some_and(|data| data.guard));
        let steps = data_steps(&demands, &target, &[]).expect("ok");
        assert_eq!(steps[0].name, "backfill_author");
        assert!(steps[0].data.as_ref().is_some_and(|data| !data.guard));
    }

    #[test]
    fn no_backfill_naming_an_undemanded_or_a_partial_set_of_columns_is_refused() {
        let target = ir(vec![author(vec![
            column("bio", "string"),
            column("slug", "string"),
        ])]);
        let demands = [slug_demand("author", "bio"), slug_demand("author", "slug")];
        assert_eq!(
            data_steps(&demands, &target, &skip(&[("author", "nickname")]))
                .expect_err("refused")
                .to_string(),
            "--no-backfill author.nickname: no generated backfill fills that column; the \
             columns this migration backfills: author.bio, author.slug"
        );
        assert_eq!(
            data_steps(&demands, &target, &skip(&[("author", "slug")]))
                .expect_err("refused")
                .to_string(),
            "--no-backfill author.slug: author also needs a value for bio in this \
             migration, and one model has one data step; add --no-backfill for author.bio \
             too, or for none of them"
        );
    }

    fn author(extra: Vec<SchemaColumn>) -> SchemaModel {
        let mut columns = vec![pk(), column("name", "string")];
        columns.extend(extra);
        model("Author", columns)
    }

    fn nullable(name: &str) -> SchemaColumn {
        SchemaColumn {
            nullable: true,
            ..column(name, "string")
        }
    }

    fn add(column: &str) -> MigrationOp {
        MigrationOp::AddColumn {
            table: "author".into(),
            column: column.into(),
        }
    }

    fn demand_of(op: &MigrationOp, before: SchemaModel, after: SchemaModel) -> Option<Demand> {
        demands(op, &ir(vec![before]), &ir(vec![after]))
    }

    fn status(labels: &[&str]) -> SchemaColumn {
        SchemaColumn {
            enum_values: Some(labels.iter().map(|l| serde_json::json!(l)).collect()),
            enum_type_name: Some("status".into()),
            ..column("status", "string")
        }
    }

    #[test]
    fn a_removed_label_demands_a_value_of_every_column_that_held_it_before() {
        let before = ir(vec![author(vec![status(&["draft", "gone"])])]);
        // `previous` is new in this file: it holds no row yet.
        let mut previous = status(&["draft"]);
        previous.name = "previous".into();
        let after = ir(vec![author(vec![status(&["draft"]), previous])]);
        let op = MigrationOp::RemoveEnumLabel {
            type_name: "status".into(),
            label: "gone".into(),
            columns: vec![
                ("author".into(), "status".into()),
                ("author".into(), "previous".into()),
            ],
        };
        assert_eq!(demands(&op, &before, &after), None);
        assert_eq!(
            collect(&[op.clone(), op], &before, &after),
            [Demand {
                table: "author".into(),
                column: "status".into(),
                reason: Reason::LabelRemoved {
                    type_name: "status".into(),
                    label: "gone".into(),
                },
                driver: Driver::Chunked,
            }]
        );
        // Between the backfill and the contract every column of the type
        // still declares the label, at its old place.
        let removals = label_removals(&[MigrationOp::RemoveEnumLabel {
            type_name: "status".into(),
            label: "gone".into(),
            columns: vec![],
        }]);
        let restored = with_removed_labels(&after, &before, &removals);
        for col in &restored.payload.models[0].columns[2..] {
            assert_eq!(
                col.enum_values,
                Some(vec![serde_json::json!("draft"), serde_json::json!("gone")])
            );
        }
    }

    #[test]
    fn each_reason_is_read_off_the_change() {
        let factory = SchemaColumn {
            default_factory: Some("uuid.uuid4".into()),
            ..column("token", "uuid")
        };
        let mut fk = author(vec![column("team_id", "integer")]);
        fk.foreign_keys.push(SchemaForeignKey {
            renamed_from: None,
            column: "team_id".into(),
            to_table: "team".into(),
            to_column: "id".into(),
            on_delete: None,
            name: Some("fk_author_team_id_team".into()),
        });
        let cases = [
            (
                add("slug"),
                author(vec![]),
                author(vec![column("slug", "string")]),
                Reason::NoDefault,
            ),
            (
                add("token"),
                author(vec![]),
                author(vec![factory]),
                Reason::DefaultFactory("uuid.uuid4".into()),
            ),
            (add("team_id"), author(vec![]), fk, Reason::RequiredFk),
            (
                MigrationOp::AlterColumnNullability {
                    table: "author".into(),
                    column: "slug".into(),
                },
                author(vec![nullable("slug")]),
                author(vec![column("slug", "string")]),
                Reason::NowNotNull,
            ),
        ];
        for (op, before, after, reason) in cases {
            let demand = demand_of(&op, before, after).expect("a demand");
            assert_eq!(demand.reason, reason, "{op:?}");
            assert_eq!(demand.table, "author");
            assert_eq!(demand.driver, Driver::Chunked);
        }
    }

    #[test]
    fn a_change_that_asks_the_rows_for_nothing_is_no_demand() {
        let defaulted = SchemaColumn {
            default: Some(serde_json::json!("free")),
            ..column("tier", "string")
        };
        assert_eq!(
            demand_of(&add("tier"), author(vec![]), author(vec![defaulted])),
            None
        );
        assert_eq!(
            demand_of(&add("bio"), author(vec![]), author(vec![nullable("bio")])),
            None
        );
        // Relaxing NOT NULL asks for nothing.
        let relax = MigrationOp::AlterColumnNullability {
            table: "author".into(),
            column: "slug".into(),
        };
        assert_eq!(
            demand_of(
                &relax,
                author(vec![column("slug", "string")]),
                author(vec![nullable("slug")])
            ),
            None
        );
        // A table the same file creates has no rows.
        let created = ir(vec![author(vec![column("slug", "string")])]);
        assert_eq!(demands(&add("slug"), &ir(vec![]), &created), None);
    }

    #[test]
    fn a_model_without_one_primary_key_is_backfilled_atomically() {
        let keyless = model("Tag", vec![column("label", "string")]);
        let mut with_slug = keyless.clone();
        with_slug.columns.push(column("slug", "string"));
        let op = MigrationOp::AddColumn {
            table: "tag".into(),
            column: "slug".into(),
        };
        assert_eq!(
            demand_of(&op, keyless, with_slug).map(|d| d.driver),
            Some(Driver::Atomic)
        );
    }

    #[test]
    fn the_staged_check_is_never_a_ck_name_and_is_guarded_at_63() {
        assert_eq!(
            staged_not_null_name("author", "slug"),
            "_ferro_notnull_author_slug"
        );
        assert!(!staged_not_null_name("author", "slug").starts_with("ck_"));
        let long = staged_not_null_name(&"t".repeat(40), &"c".repeat(40));
        assert_eq!(long.chars().count(), 63);
        assert!(long.starts_with("_ferro_notnull_") && long.ends_with("_nn"));
    }

    #[test]
    fn demands_are_ordered_parents_first() {
        let team = model("Team", vec![pk()]);
        let mut member = model("Member", vec![pk(), column("team_id", "integer")]);
        member.foreign_keys.push(SchemaForeignKey {
            renamed_from: None,
            column: "team_id".into(),
            to_table: "team".into(),
            to_column: "id".into(),
            on_delete: None,
            name: None,
        });
        let before = ir(vec![member.clone(), team.clone()]);
        let mut member_after = member.clone();
        member_after.columns.push(column("slug", "string"));
        let mut team_after = team.clone();
        team_after.columns.push(column("slug", "string"));
        let after = ir(vec![member_after, team_after]);
        let ops = [
            MigrationOp::AddColumn {
                table: "member".into(),
                column: "slug".into(),
            },
            MigrationOp::AddColumn {
                table: "team".into(),
                column: "slug".into(),
            },
        ];
        let tables: Vec<String> = collect(&ops, &before, &after)
            .into_iter()
            .map(|d| d.table)
            .collect();
        assert_eq!(tables, ["team", "member"]);
    }
}
