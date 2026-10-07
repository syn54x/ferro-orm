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
use super::{GenerateError, GeneratedStep, Rendering, rebuild, step_text};
use crate::directory::{Headers, StepDialect, StepKind};
use crate::order::order_by_dependencies;
use crate::{Dialect, MigrationOp, MigrationPlan, render_plan};
use ferro_ddl_lowering::{quote_ident, render_drop_constraint, render_validate_constraint};
use ferro_schema_ir::{IrEnvelope, SchemaIrPayload, SchemaModel};
use std::collections::BTreeMap;

/// Why the rows a table holds need a value for a column.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize)]
#[serde(tag = "kind", content = "factory", rename_all = "snake_case")]
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

fn find_model<'a>(ir: &'a IrEnvelope<SchemaIrPayload>, table: &str) -> Option<&'a SchemaModel> {
    ir.payload
        .models
        .iter()
        .find(|model| model.table_name == table)
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

/// Every demand `ops` make (an op per dialect's plan may repeat one), each
/// once, tables in foreign-key order (parents first, as `after` declares
/// them), columns in the order the model declares them.
pub fn collect(
    ops: &[MigrationOp],
    before: &IrEnvelope<SchemaIrPayload>,
    after: &IrEnvelope<SchemaIrPayload>,
) -> Vec<Demand> {
    let mut found: Vec<Demand> = Vec::new();
    for op in ops {
        if let Some(demand) = demands(op, before, after)
            && !found
                .iter()
                .any(|d| d.table == demand.table && d.column == demand.column)
        {
            found.push(demand);
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

/// `ir` with every demanded column nullable: the schema between the expand
/// and the contract, which the backfill fills in.
pub fn relaxed(
    ir: &IrEnvelope<SchemaIrPayload>,
    demands: &[Demand],
) -> IrEnvelope<SchemaIrPayload> {
    let columns: Vec<(String, String)> = demands
        .iter()
        .map(|d| (d.table.clone(), d.column.clone()))
        .collect();
    relax_columns(ir, &columns)
}

/// The temporary check that stages `NOT NULL` on Postgres (ADR-0042):
/// `_ferro_notnull_<table>_<column>`, guarded at 63 characters like every
/// ferro name. Deliberately not `ck_*`: check reconciliation and the drift
/// check own only `ck_*` names, so a run stopped between the add-constraint
/// step and the contract leaves a check neither reads as a dropped
/// declaration.
pub fn staged_not_null_name(table: &str, column: &str) -> String {
    let raw = format!("_ferro_notnull_{table}_{column}");
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
    /// The reason its `down` declares `@nothing_to_reverse` (ADR-0033: a
    /// generated down is never irreversible by default).
    pub reverse: String,
}

/// One backfill step per table `demands` name, in their (foreign-key) order:
/// `backfill_<model>`, with its scaffold's inputs and no rendering. The
/// `reverse` reason names the expand step once the caller numbers the steps
/// ([`name_reverse`]).
pub fn data_steps(demands: &[Demand], target: &IrEnvelope<SchemaIrPayload>) -> Vec<GeneratedStep> {
    let mut tables: Vec<&str> = Vec::new();
    for demand in demands {
        if !tables.contains(&demand.table.as_str()) {
            tables.push(&demand.table);
        }
    }
    tables
        .into_iter()
        .map(|table| {
            let mine: Vec<&Demand> = demands.iter().filter(|d| d.table == table).collect();
            let model = find_model(target, table)
                .map(|model| super::short_model_name(&model.model_name).to_string())
                .unwrap_or_else(|| table.to_string());
            GeneratedStep {
                ordinal: 0,
                name: format!("backfill_{}", model.to_lowercase()),
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
                    reverse: String::new(),
                }),
            }
        })
        .collect()
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
        let (made_required, added): (Vec<&DemandedColumn>, Vec<&DemandedColumn>) = data
            .columns
            .iter()
            .partition(|col| col.reason == Reason::NowNotNull);
        let mut clauses = Vec::new();
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
                let mut tables: Vec<&str> = Vec::new();
                for d in demands {
                    if !tables.contains(&d.table.as_str()) {
                        tables.push(&d.table);
                    }
                }
                let mut up = Vec::new();
                let mut down = Vec::new();
                for table in tables {
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

#[cfg(test)]
mod tests {
    use super::super::tests::{column, ir, model, pk};
    use super::*;
    use ferro_schema_ir::{SchemaColumn, SchemaForeignKey};

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
