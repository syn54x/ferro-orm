//! The one planner (ADR-0027, ADR-0041): every change between two SchemaIR
//! snapshots of a whole modelset, decided once, in execution order.
//!
//! The reconciliation pass, the migration generator, the drift check and the
//! Alembic bridge all ask [`plan_from_ir`] the same question — "what turns
//! `old` into `new`?" — and differ only in where `old` comes from: a declared
//! snapshot, or the live database read into an IR. What a live database holds
//! that the IR cannot say (catalog CHECK bodies, validity flags, row policies
//! as `pg_policy` prints them, live enum labels) travels beside it as
//! [`LiveFacts`].
//!
//! Every decision is the `ferro_ddl_lowering` function the pass consumed
//! before this module existed (AGENTS.md § I-1 items 11–16); this module only
//! decides *where* in the plan each verdict lands.

use crate::{
    Dialect, LiveCheckValidity, LiveFkValidity, LiveIndexValidity, MigrationOp, MigrationPlan,
    PlanOptions, emit,
};
use ferro_ddl_lowering::{
    EnumTypeProvenance, LiveRowPolicy, LiveRowSecurity, ResolvedStorage, drifted_check_names,
    enum_type_provenance, excess_row_security_flag_statements, extra_check_names,
    extra_check_names_warning, extra_enum_labels, extra_enum_labels_warning, fk_action_from_str,
    fk_action_sql, fk_name, is_ferro_fk_name, is_ferro_row_policy_name, missing_check_names,
    missing_enum_labels, missing_row_security_flag_statements, plan_row_security_reconcile,
    render_check_body, render_disable_row_security, render_enable_row_security,
    render_force_row_security, render_no_force_row_security, render_table_check_body,
    resolve_column_storage, row_policy_clauses, row_policy_command_token,
    schema_columns_storage_drift,
};
use ferro_schema_ir::{IrEnvelope, SchemaIrPayload, SchemaModel};
use std::borrow::Cow;
use std::collections::{BTreeMap, BTreeSet};

fn serde_default_true() -> bool {
    true
}

/// One live CHECK constraint as the catalog reports it. Its body is the
/// backend's own rendering — the IR carries exactly one body language, so a
/// live CHECK travels here rather than as a `SchemaCheck` (AGENTS.md § I-1).
#[derive(Clone, Debug, Default, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct LiveCheckFact {
    /// Constraint name.
    pub name: String,
    /// The catalog definition (`pg_get_constraintdef` on Postgres, the
    /// inline `CHECK (…)` fragment on SQLite).
    pub definition: String,
    /// Whether the name follows ferro's `ck_` convention. Only a ferro-owned
    /// CHECK is ever rebuilt or dropped.
    #[serde(default)]
    pub ferro_owned: bool,
    /// `pg_constraint.convalidated`; always `true` on SQLite.
    #[serde(default = "serde_default_true")]
    pub validated: bool,
}

/// Everything about one live table the planner reads that is not IR.
#[derive(Clone, Debug, Default, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct LiveTableFacts {
    /// Every live CHECK on the table, ferro-owned or not.
    #[serde(default)]
    pub checks: Vec<LiveCheckFact>,
    /// Validity of every live named foreign key.
    #[serde(default)]
    pub foreign_keys: Vec<LiveFkValidity>,
    /// Validity of every live ferro-owned index.
    #[serde(default)]
    pub indexes: Vec<LiveIndexValidity>,
    /// The table's row-security flags and every policy on it, with bodies as
    /// the catalog prints them. The "off, no policies" state on SQLite.
    #[serde(default, serialize_with = "serialize_row_security")]
    pub row_security: LiveRowSecurity,
}

/// The facts a live database holds beside its IR, keyed by table.
///
/// A table absent from [`tables`](Self::tables) reads as the `old` snapshot
/// declares it: every declared CHECK present with its canonical body and
/// valid, every FK and index valid, its row security exactly as declared.
/// A type absent from [`enum_labels`](Self::enum_labels) reads its labels
/// from the `old` snapshot's columns. So [`LiveFacts::declared`] — nothing
/// recorded — is the side-table for planning one declared snapshot against
/// another.
#[derive(Clone, Debug, Default, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct LiveFacts {
    /// Live facts per table name.
    #[serde(default)]
    pub tables: BTreeMap<String, LiveTableFacts>,
    /// Every live native enum type's labels in enum sort order (Postgres).
    #[serde(default)]
    pub enum_labels: BTreeMap<String, Vec<String>>,
}

impl LiveFacts {
    /// The all-valid, nothing-foreign, bodies-equal-to-canonical side-table:
    /// every table and type reads as the `old` snapshot declares it.
    pub fn declared() -> Self {
        Self::default()
    }
}

/// `LiveRowSecurity` lives in `ferro_ddl_lowering` with `Deserialize` only;
/// its wire shape is that same field layout.
fn serialize_row_security<S: serde::Serializer>(
    live: &LiveRowSecurity,
    serializer: S,
) -> Result<S::Ok, S::Error> {
    use serde::Serialize;
    let policies: Vec<serde_json::Value> = live
        .policies
        .iter()
        .map(|policy| {
            serde_json::json!({
                "name": policy.name,
                "command": policy.command,
                "restrictive": policy.restrictive,
                "using": policy.using,
                "with_check": policy.with_check,
                "roles": policy.roles,
                "ferro_owned": policy.ferro_owned,
            })
        })
        .collect();
    serde_json::json!({
        "enabled": live.enabled,
        "forced": live.forced,
        "policies": policies,
    })
    .serialize(serializer)
}

/// The facts a table declared in `old` would show live: every declared CHECK
/// with its canonical body, valid; every FK and index valid; row security on
/// exactly as declared, each policy's bodies as ferro renders them.
fn declared_table_facts(old_model: &SchemaModel) -> LiveTableFacts {
    let checks = old_model
        .table_checks
        .iter()
        .map(|check| (check.name.clone(), render_table_check_body(check)))
        .chain(
            old_model
                .checks
                .iter()
                .map(|check| (check.name.clone(), render_check_body(check))),
        )
        .map(|(name, body)| LiveCheckFact {
            name,
            definition: format!("CHECK ({body})"),
            ferro_owned: true,
            validated: true,
        })
        .collect();
    LiveTableFacts {
        checks,
        foreign_keys: Vec::new(),
        indexes: Vec::new(),
        row_security: declared_row_security(old_model),
    }
}

/// A declared table as introspection would report it: on Postgres, a column
/// whose storage resolves to a native enum is read back as one
/// (`postgres_native_enum`), the flag the storage-drift decision reads on its
/// live side. Without it two identical declared enum columns would read as
/// drift.
fn declared_live_view(model: &SchemaModel, dialect: Dialect) -> Cow<'_, SchemaModel> {
    if dialect != Dialect::Postgres {
        return Cow::Borrowed(model);
    }
    let native_enum = |col: &ferro_schema_ir::SchemaColumn| {
        matches!(
            resolve_column_storage(col, dialect),
            Ok(ResolvedStorage::PgEnum { .. })
        )
    };
    if !model
        .columns
        .iter()
        .any(|col| native_enum(col) && !col.postgres_native_enum)
    {
        return Cow::Borrowed(model);
    }
    let mut view = model.clone();
    for col in &mut view.columns {
        if native_enum(col) {
            col.postgres_native_enum = true;
        }
    }
    Cow::Owned(view)
}

fn declared_row_security(model: &SchemaModel) -> LiveRowSecurity {
    let Some(declaration) = model.row_security.as_ref() else {
        return LiveRowSecurity::default();
    };
    LiveRowSecurity {
        enabled: true,
        forced: declaration.force,
        policies: declaration
            .policies
            .iter()
            .map(|policy| {
                // A policy whose clauses cannot render is invalid IR, which
                // `render_plan` rejects before any statement of this plan can
                // run (`validate_schema_ir`).
                let (using, with_check) = row_policy_clauses(model, policy).unwrap_or_default();
                LiveRowPolicy {
                    name: policy.name.clone(),
                    command: row_policy_command_token(policy.command).to_string(),
                    restrictive: policy.restrictive,
                    using,
                    with_check,
                    roles: Vec::new(),
                    ferro_owned: is_ferro_row_policy_name(&policy.name),
                }
            })
            .collect(),
    }
}

/// Decide every change that turns `old` into `new`, for the whole modelset.
///
/// `facts` carries what the live database holds beside its IR; pass
/// [`LiveFacts::declared`] when `old` is a declared snapshot. `options`
/// gates the ops that remove something (ADR-0013's ladder): without
/// `destructive`, drops are left out and their leftover warnings stand.
///
/// The plan is in execution order:
///
/// 1. On Postgres, label additions to existing enum types (ADR-0011) — first,
///    so any later statement may name a new label — then the creation of
///    every enum type the plan introduces and that does not yet exist.
/// 2. New tables, parents before children.
/// 3. Every table in both snapshots, ordered by model name and then so a
///    table follows the tables its foreign keys reference, each table's ops
///    in the reconciliation pass's order: column adds and alters; index adds
///    (and, destructive, drops); invalid-index rebuilds; FK adds and
///    rebuilds; CHECK adds, rebuilds, validations of `NOT VALID`
///    constraints, (destructive) CHECK drops; row security last — flags,
///    policy adds and rebuilds, (destructive) orphan drops and teardown —
///    after the table's column and data steps; then (destructive) its
///    column drops.
/// 4. (Destructive) dropped tables, children before parents, then the enum
///    types nothing declares any more.
pub fn plan_from_ir(
    old: &IrEnvelope<SchemaIrPayload>,
    new: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
    facts: &LiveFacts,
    options: PlanOptions,
) -> MigrationPlan {
    let old_models = index_models(&old.payload.models);
    let new_models = index_models(&new.payload.models);
    let mut plan = MigrationPlan::default();

    let added: Vec<&SchemaModel> = new_models
        .iter()
        .filter(|(table, _)| !old_models.contains_key(*table))
        .map(|(_, model)| *model)
        .collect();
    let dropped: Vec<&SchemaModel> = old_models
        .iter()
        .filter(|(table, _)| !new_models.contains_key(*table))
        .map(|(_, model)| *model)
        .collect();

    if dialect == Dialect::Postgres {
        plan_enum_label_additions(old, new, facts, &mut plan);
        plan_enum_type_creation(old, new, &old_models, facts, &mut plan);
    }

    for model in emit::order_models_for_create(&added) {
        plan.operations.push(MigrationOp::AddTable {
            table: model.table_name.clone(),
        });
    }

    for (old_model, new_model) in order_existing_tables(&old_models, &new_models) {
        let (old_view, table_facts) = match facts.tables.get(&new_model.table_name) {
            Some(live) => (Cow::Borrowed(old_model), Cow::Borrowed(live)),
            None => (
                declared_live_view(old_model, dialect),
                Cow::Owned(declared_table_facts(old_model)),
            ),
        };
        plan_existing_table(
            &old_view,
            new_model,
            dialect,
            &table_facts,
            options,
            &mut plan,
        );
    }

    if options.destructive {
        for model in emit::order_models_for_create(&dropped).into_iter().rev() {
            plan.operations.push(MigrationOp::DropTable {
                table: model.table_name.clone(),
            });
        }
        if dialect == Dialect::Postgres {
            plan_enum_type_drops(old, new, &old_models, &new_models, &mut plan);
        }
    }

    plan
}

/// Tables present in both snapshots, sorted by model name and then so each
/// follows the tables its foreign keys reference — the order the
/// reconciliation pass has always visited them in (#302).
fn order_existing_tables<'a>(
    old_models: &BTreeMap<String, &'a SchemaModel>,
    new_models: &BTreeMap<String, &'a SchemaModel>,
) -> Vec<(&'a SchemaModel, &'a SchemaModel)> {
    let mut existing: Vec<(&SchemaModel, &SchemaModel)> = new_models
        .iter()
        .filter_map(|(table, new_model)| Some((*old_models.get(table)?, *new_model)))
        .collect();
    existing.sort_by(|a, b| a.1.model_name.cmp(&b.1.model_name));
    crate::order_by_dependencies(
        existing,
        |(_, new_model)| new_model.table_name.clone(),
        |(_, new_model)| {
            new_model
                .foreign_keys
                .iter()
                .map(|fk| fk.to_table.clone())
                .collect()
        },
    )
}

fn plan_existing_table(
    old_model: &SchemaModel,
    new_model: &SchemaModel,
    dialect: Dialect,
    facts: &LiveTableFacts,
    options: PlanOptions,
    plan: &mut MigrationPlan,
) {
    let table = new_model.table_name.as_str();
    let mut ops = Vec::new();
    let mut column_drops = Vec::new();

    diff_model_columns(
        table,
        old_model,
        new_model,
        dialect,
        &mut ops,
        &mut column_drops,
    );
    diff_model_indexes(table, old_model, new_model, options.destructive, &mut ops);
    // Invalid-index rebuilds (#515; ADR-0044) go where `AddIndex` goes: after
    // the column ops, ahead of the foreign-key ops. An invalid index is
    // present live, so the diff above never planned an `AddIndex` for it.
    ops.extend(index_rebuilds(table, new_model, &facts.indexes));
    diff_model_foreign_keys(table, old_model, new_model, &mut ops, &mut plan.warnings);

    // Check addition (#343; ADR-0013) lands after the column ops, so a CHECK
    // over a newly added column follows its ADD COLUMN.
    let live_check_names: Vec<String> = facts
        .checks
        .iter()
        .map(|check| check.name.clone())
        .collect();
    ops.extend(missing_checks(
        table,
        old_model,
        new_model,
        &live_check_names,
    ));
    // Body drift (#344; ADR-0015), after the adds. Only ferro-owned names are
    // eligible: a user-owned CHECK is never dropped.
    let live_ferro_owned: Vec<(String, String)> = facts
        .checks
        .iter()
        .filter(|check| check.ferro_owned)
        .map(|check| (check.name.clone(), check.definition.clone()))
        .collect();
    ops.extend(check_rebuilds(table, new_model, &live_ferro_owned));
    // Validation (#515; ADR-0043): after every add, leaving out any name a
    // rebuild covers — the rebuild's bare ADD installs a valid constraint.
    let rebuilt: Vec<String> = ops
        .iter()
        .filter_map(|op| match op {
            MigrationOp::RebuildCheck { name, .. } => Some(name.clone()),
            MigrationOp::RebuildForeignKey { old_name, .. } => Some(old_name.clone()),
            _ => None,
        })
        .collect();
    let check_validity: Vec<LiveCheckValidity> = facts
        .checks
        .iter()
        .map(|check| LiveCheckValidity {
            name: check.name.clone(),
            validated: check.validated,
        })
        .collect();
    ops.extend(
        validations(table, new_model, &facts.foreign_keys, &check_validity)
            .into_iter()
            .filter(|op| {
                !matches!(op, MigrationOp::ValidateConstraint { name, .. } if rebuilt.contains(name))
            }),
    );
    // Leftovers (#345; ADR-0013): always reported — a leftover CHECK keeps
    // rejecting rows the model now allows — and dropped only when destructive.
    let live_ferro_owned_names: Vec<String> = live_ferro_owned
        .iter()
        .map(|(name, _)| name.clone())
        .collect();
    let extras = extra_check_names(&declared_check_names(new_model), &live_ferro_owned_names);
    if let Some(warning) = extra_check_names_warning(table, &extras) {
        plan.warnings.push(warning);
    }
    if options.destructive {
        ops.extend(check_drops(table, new_model, &live_ferro_owned_names));
    }

    // Row security lands last for the table (#413; PRD #406 user story 20):
    // every column change and data-shaped step above has run before any
    // policy starts filtering the rows it touches.
    plan_row_security(
        new_model,
        &facts.row_security,
        dialect,
        options.destructive,
        &mut ops,
        &mut plan.always_warnings,
    );

    if options.destructive {
        ops.extend(column_drops);
    }
    plan.operations.extend(ops);
}

/// Translate the row-security reconciliation decision for one live table
/// (`plan_row_security_reconcile`, the single seam; AGENTS.md § I-1 item 16)
/// into ops, in its execution order: missing flags, policy additions,
/// rebuilds, then (destructive) orphan drops and the flag teardown. Its
/// warnings — foreign and unverifiable policies, dropped declarations,
/// teardowns — become the plan's always-warnings; a foreign policy and an
/// unverifiable raw body are reported and never become an op.
fn plan_row_security(
    model: &SchemaModel,
    live: &LiveRowSecurity,
    dialect: Dialect,
    destructive: bool,
    ops: &mut Vec<MigrationOp>,
    always_warnings: &mut Vec<String>,
) {
    // An Err is a declared policy whose clauses cannot render: invalid IR,
    // which `render_plan` rejects (`validate_schema_ir`) before anything
    // executes.
    let Ok(decision) = plan_row_security_reconcile(model, live, dialect, destructive) else {
        return;
    };
    always_warnings.extend(decision.warnings);
    if dialect != Dialect::Postgres {
        return;
    }
    let table = model.table_name.as_str();
    ops.extend(
        missing_row_security_flag_statements(model, live)
            .iter()
            .filter_map(|statement| row_security_flag_op(table, statement)),
    );
    ops.extend(
        decision
            .missing
            .into_iter()
            .map(|name| MigrationOp::AddRowPolicy {
                table: table.to_string(),
                name,
            }),
    );
    ops.extend(
        decision
            .drifted
            .into_iter()
            .map(|name| MigrationOp::RebuildRowPolicy {
                table: table.to_string(),
                name,
            }),
    );
    if destructive {
        ops.extend(
            decision
                .extra
                .into_iter()
                .map(|name| MigrationOp::DropRowPolicy {
                    table: table.to_string(),
                    name,
                }),
        );
        ops.extend(
            excess_row_security_flag_statements(model, live)
                .iter()
                .filter_map(|statement| row_security_flag_op(table, statement)),
        );
    }
}

/// The op whose rendering is `statement`, one of the four flag statements the
/// row-security family decides (`missing_row_security_flag_statements` /
/// `excess_row_security_flag_statements` return nothing else).
fn row_security_flag_op(table: &str, statement: &str) -> Option<MigrationOp> {
    let table_owned = table.to_string();
    [
        (
            render_enable_row_security(table),
            MigrationOp::EnableRowSecurity {
                table: table_owned.clone(),
            },
        ),
        (
            render_force_row_security(table),
            MigrationOp::ForceRowSecurity {
                table: table_owned.clone(),
            },
        ),
        (
            render_no_force_row_security(table),
            MigrationOp::NoForceRowSecurity {
                table: table_owned.clone(),
            },
        ),
        (
            render_disable_row_security(table),
            MigrationOp::DisableRowSecurity { table: table_owned },
        ),
    ]
    .into_iter()
    .find(|(rendered, _)| rendered == statement)
    .map(|(_, op)| op)
}

/// Every native enum type the models declare (Postgres storage), with the
/// labels of its first declaring column and every `(table, column)` that
/// declares it, in model order.
struct DeclaredEnumTypes {
    labels: BTreeMap<String, Vec<String>>,
    declaring: BTreeMap<String, Vec<(String, String)>>,
}

fn declared_enum_types(models: &[SchemaModel]) -> DeclaredEnumTypes {
    let mut declared = DeclaredEnumTypes {
        labels: BTreeMap::new(),
        declaring: BTreeMap::new(),
    };
    for model in models {
        for col in &model.columns {
            if let Ok(ResolvedStorage::PgEnum { type_name, labels }) =
                resolve_column_storage(col, Dialect::Postgres)
            {
                declared
                    .declaring
                    .entry(type_name.clone())
                    .or_default()
                    .push((model.table_name.clone(), col.name.clone()));
                declared.labels.entry(type_name).or_insert(labels);
            }
        }
    }
    declared
}

/// Label addition (ADR-0011): for every declared type that already exists,
/// the labels it lacks, and a warning naming the live labels the model no
/// longer declares (warn-never-act). A type's existing labels are the live
/// database's when `facts` records it, else the `old` snapshot's.
fn plan_enum_label_additions(
    old: &IrEnvelope<SchemaIrPayload>,
    new: &IrEnvelope<SchemaIrPayload>,
    facts: &LiveFacts,
    plan: &mut MigrationPlan,
) {
    let declared = declared_enum_types(&new.payload.models);
    let before = declared_enum_types(&old.payload.models);
    for (type_name, labels) in &declared.labels {
        let Some(existing) = facts
            .enum_labels
            .get(type_name)
            .or_else(|| before.labels.get(type_name))
        else {
            continue;
        };
        let extra = extra_enum_labels(labels, existing);
        if let Some(warning) = extra_enum_labels_warning(type_name, &extra) {
            plan.warnings.push(warning);
        }
        for label in missing_enum_labels(labels, existing) {
            plan.operations.push(MigrationOp::AddEnumLabel {
                type_name: type_name.clone(),
                label,
            });
        }
    }
}

/// Type creation: every declared type the plan introduces
/// (`enum_type_provenance` over the columns it adds — a new table's, or an
/// existing table's new column) that neither the live database nor the `old`
/// snapshot already has.
fn plan_enum_type_creation(
    old: &IrEnvelope<SchemaIrPayload>,
    new: &IrEnvelope<SchemaIrPayload>,
    old_models: &BTreeMap<String, &SchemaModel>,
    facts: &LiveFacts,
    plan: &mut MigrationPlan,
) {
    let declared = declared_enum_types(&new.payload.models);
    let before = declared_enum_types(&old.payload.models);
    let mut added_columns = Vec::new();
    let mut inline_created_columns = Vec::new();
    for model in &new.payload.models {
        let old_model = old_models.get(&model.table_name);
        for col in &model.columns {
            let pair = (model.table_name.clone(), col.name.clone());
            match old_model {
                None => {
                    added_columns.push(pair.clone());
                    inline_created_columns.push(pair);
                }
                Some(old_model) if !old_model.columns.iter().any(|c| c.name == col.name) => {
                    added_columns.push(pair);
                }
                Some(_) => {}
            }
        }
    }
    for (type_name, provenance) in
        enum_type_provenance(&declared.declaring, &added_columns, &inline_created_columns)
    {
        let exists =
            facts.enum_labels.contains_key(&type_name) || before.labels.contains_key(&type_name);
        if matches!(provenance, EnumTypeProvenance::Introduced { .. }) && !exists {
            let labels = declared.labels.get(&type_name).cloned().unwrap_or_default();
            plan.operations
                .push(MigrationOp::CreateEnumType { type_name, labels });
        }
    }
}

/// Type drops (destructive; ADR-0020): every type the `old` snapshot declares
/// whose declaring columns the plan all removes, and that `new` no longer
/// declares anywhere.
fn plan_enum_type_drops(
    old: &IrEnvelope<SchemaIrPayload>,
    new: &IrEnvelope<SchemaIrPayload>,
    old_models: &BTreeMap<String, &SchemaModel>,
    new_models: &BTreeMap<String, &SchemaModel>,
    plan: &mut MigrationPlan,
) {
    let before = declared_enum_types(&old.payload.models);
    let after = declared_enum_types(&new.payload.models);
    let mut removed_columns = Vec::new();
    let mut removed_with_table = Vec::new();
    for (table, old_model) in old_models {
        let new_model = new_models.get(table);
        for col in &old_model.columns {
            let pair = (table.clone(), col.name.clone());
            match new_model {
                None => {
                    removed_columns.push(pair.clone());
                    removed_with_table.push(pair);
                }
                Some(new_model) if !new_model.columns.iter().any(|c| c.name == col.name) => {
                    removed_columns.push(pair);
                }
                Some(_) => {}
            }
        }
    }
    for (type_name, provenance) in
        enum_type_provenance(&before.declaring, &removed_columns, &removed_with_table)
    {
        if matches!(provenance, EnumTypeProvenance::Introduced { .. })
            && !after.labels.contains_key(&type_name)
        {
            plan.operations
                .push(MigrationOp::DropEnumType { type_name });
        }
    }
}

/// Plan the [`MigrationOp::AddCheck`] operations for one table (#343;
/// ADR-0013): every declared CHECK constraint — table check or column check —
/// that `live_check_names` does not already cover. The decision is name-based
/// and single-sourced in `ferro_ddl_lowering::missing_check_names`.
pub fn plan_missing_checks(
    table: &str,
    old_ir: &IrEnvelope<SchemaIrPayload>,
    new_ir: &IrEnvelope<SchemaIrPayload>,
    live_check_names: &[String],
) -> Vec<MigrationOp> {
    let old_models = index_models(&old_ir.payload.models);
    let new_models = index_models(&new_ir.payload.models);
    let (Some(old_model), Some(new_model)) = (old_models.get(table), new_models.get(table)) else {
        return Vec::new();
    };
    missing_checks(table, old_model, new_model, live_check_names)
}

fn missing_checks(
    table: &str,
    old_model: &SchemaModel,
    new_model: &SchemaModel,
    live_check_names: &[String],
) -> Vec<MigrationOp> {
    let old_col_names: BTreeSet<&str> = old_model.columns.iter().map(|c| c.name.as_str()).collect();
    missing_check_names(new_model, live_check_names)
        .into_iter()
        .filter(|name| {
            // A column check whose column is newly added rides the AddColumn
            // emission, the same dedup `diff_model_indexes` applies to
            // single-column indexes. Table checks always stand alone.
            new_model
                .checks
                .iter()
                .find(|check| &check.name == name)
                .is_none_or(|check| old_col_names.contains(check.column.as_str()))
        })
        .map(|name| MigrationOp::AddCheck {
            table: table.to_string(),
            name,
        })
        .collect()
}

/// Plan the [`MigrationOp::RebuildCheck`] operations for one table (#344;
/// ADR-0015): every declared CHECK whose live counterpart exists and whose
/// normalized body differs from the canonical rendering. `live` is
/// `(name, catalog definition)` pairs of ferro-owned CHECKs.
pub fn plan_check_rebuilds(
    table: &str,
    new_ir: &IrEnvelope<SchemaIrPayload>,
    live: &[(String, String)],
) -> Vec<MigrationOp> {
    let new_models = index_models(&new_ir.payload.models);
    let Some(new_model) = new_models.get(table) else {
        return Vec::new();
    };
    check_rebuilds(table, new_model, live)
}

fn check_rebuilds(
    table: &str,
    new_model: &SchemaModel,
    live: &[(String, String)],
) -> Vec<MigrationOp> {
    drifted_check_names(new_model, live)
        .into_iter()
        .map(|name| MigrationOp::RebuildCheck {
            table: table.to_string(),
            name,
        })
        .collect()
}

/// Plan the [`MigrationOp::DropCheck`] operations for one table (#345;
/// ADR-0013): every live ferro-owned CHECK name the model no longer declares,
/// in live order.
pub fn plan_check_drops(
    table: &str,
    new_ir: &IrEnvelope<SchemaIrPayload>,
    live_ferro_owned_names: &[String],
) -> Vec<MigrationOp> {
    let new_models = index_models(&new_ir.payload.models);
    let Some(new_model) = new_models.get(table) else {
        return Vec::new();
    };
    check_drops(table, new_model, live_ferro_owned_names)
}

fn check_drops(
    table: &str,
    new_model: &SchemaModel,
    live_ferro_owned_names: &[String],
) -> Vec<MigrationOp> {
    extra_check_names(&declared_check_names(new_model), live_ferro_owned_names)
        .into_iter()
        .map(|name| MigrationOp::DropCheck {
            table: table.to_string(),
            name,
        })
        .collect()
}

fn declared_check_names(model: &SchemaModel) -> Vec<String> {
    model
        .table_checks
        .iter()
        .map(|check| check.name.clone())
        .chain(model.checks.iter().map(|check| check.name.clone()))
        .collect()
}

/// Plan the [`MigrationOp::ValidateConstraint`] operations for one table
/// (#515; ADR-0043): every declared FK, then every declared CHECK (table
/// checks, then column checks), whose live constraint of the same name exists
/// `NOT VALID`.
pub fn plan_validations(
    table: &str,
    new_ir: &IrEnvelope<SchemaIrPayload>,
    live_fks: &[LiveFkValidity],
    live_checks: &[LiveCheckValidity],
) -> Vec<MigrationOp> {
    let new_models = index_models(&new_ir.payload.models);
    let Some(new_model) = new_models.get(table) else {
        return Vec::new();
    };
    validations(table, new_model, live_fks, live_checks)
}

fn validations(
    table: &str,
    new_model: &SchemaModel,
    live_fks: &[LiveFkValidity],
    live_checks: &[LiveCheckValidity],
) -> Vec<MigrationOp> {
    let unvalidated_fk = |name: &String| {
        live_fks
            .iter()
            .any(|live| &live.name == name && !live.validated)
    };
    let unvalidated_check = |name: &String| {
        live_checks
            .iter()
            .any(|live| &live.name == name && !live.validated)
    };
    let fk_names = new_model
        .foreign_keys
        .iter()
        .map(|fk| emit::fk_constraint_name(table, fk))
        .filter(unvalidated_fk);
    let check_names = declared_check_names(new_model)
        .into_iter()
        .filter(unvalidated_check);
    fk_names
        .chain(check_names)
        .map(|name| MigrationOp::ValidateConstraint {
            table: table.to_string(),
            name,
        })
        .collect()
}

/// Plan the [`MigrationOp::RebuildIndex`] operations for one table (#515;
/// ADR-0044): every declared standalone index or unique whose live index of
/// the same name exists but is invalid, in declared order.
pub fn plan_index_rebuilds(
    table: &str,
    new_ir: &IrEnvelope<SchemaIrPayload>,
    live_indexes: &[LiveIndexValidity],
) -> Vec<MigrationOp> {
    let new_models = index_models(&new_ir.payload.models);
    let Some(new_model) = new_models.get(table) else {
        return Vec::new();
    };
    index_rebuilds(table, new_model, live_indexes)
}

fn index_rebuilds(
    table: &str,
    new_model: &SchemaModel,
    live_indexes: &[LiveIndexValidity],
) -> Vec<MigrationOp> {
    emit::standalone_indexes(new_model)
        .into_iter()
        .filter(|(name, _, _)| {
            live_indexes
                .iter()
                .any(|live| &live.name == name && !live.valid)
        })
        .map(|(name, columns, unique)| MigrationOp::RebuildIndex {
            table: table.to_string(),
            name,
            columns,
            unique,
        })
        .collect()
}

pub(crate) fn index_models(models: &[SchemaModel]) -> BTreeMap<String, &SchemaModel> {
    models
        .iter()
        .map(|model| (model.table_name.clone(), model))
        .collect()
}

fn diff_model_columns(
    table: &str,
    old_model: &SchemaModel,
    new_model: &SchemaModel,
    dialect: Dialect,
    ops: &mut Vec<MigrationOp>,
    column_drops: &mut Vec<MigrationOp>,
) {
    let old_cols: BTreeMap<&str, _> = old_model
        .columns
        .iter()
        .map(|column| (column.name.as_str(), column))
        .collect();
    let new_cols: BTreeMap<&str, _> = new_model
        .columns
        .iter()
        .map(|column| (column.name.as_str(), column))
        .collect();

    for col in new_cols.keys().filter(|name| !old_cols.contains_key(*name)) {
        ops.push(MigrationOp::AddColumn {
            table: table.to_string(),
            column: (*col).to_string(),
        });
    }
    for col in old_cols.keys().filter(|name| !new_cols.contains_key(*name)) {
        column_drops.push(MigrationOp::DropColumn {
            table: table.to_string(),
            column: (*col).to_string(),
        });
    }
    for (name, new_col) in &new_cols {
        let Some(old_col) = old_cols.get(name) else {
            continue;
        };
        if schema_columns_storage_drift(old_col, new_col, dialect) {
            ops.push(MigrationOp::AlterColumnType {
                table: table.to_string(),
                column: (*name).to_string(),
            });
        }
        if old_col.nullable != new_col.nullable {
            ops.push(MigrationOp::AlterColumnNullability {
                table: table.to_string(),
                column: (*name).to_string(),
            });
        }
    }
}

fn diff_model_indexes(
    table: &str,
    old_model: &SchemaModel,
    new_model: &SchemaModel,
    destructive: bool,
    ops: &mut Vec<MigrationOp>,
) {
    // Indexes covering only NEW columns are emitted by the AddColumn
    // rendering, so no redundant standalone AddIndex is planned for them.
    let old_col_names: BTreeSet<&str> = old_model.columns.iter().map(|c| c.name.as_str()).collect();

    let old_by_name: BTreeMap<String, (Vec<String>, bool)> = old_model
        .indexes
        .iter()
        .map(|i| (i.name.clone(), (i.columns.clone(), i.unique)))
        .collect();
    let new_set = emit::standalone_indexes(new_model);
    let new_names: BTreeSet<&str> = new_set.iter().map(|(n, _, _)| n.as_str()).collect();

    for (name, columns, unique) in &new_set {
        if !old_by_name.contains_key(name) {
            // Skip AddIndex only for a single-column index whose sole column
            // is newly added — the AddColumn rendering emits that CREATE
            // INDEX. Composite indexes are never emitted by AddColumn and must
            // NOT be skipped, even when every indexed column is new (I-1).
            if columns.len() == 1 && !old_col_names.contains(columns[0].as_str()) {
                continue;
            }
            ops.push(MigrationOp::AddIndex {
                table: table.to_string(),
                name: name.clone(),
                columns: columns.clone(),
                unique: *unique,
            });
        }
    }
    if destructive {
        for name in old_by_name.keys() {
            if !new_names.contains(name.as_str()) {
                ops.push(MigrationOp::DropIndex {
                    table: table.to_string(),
                    name: name.clone(),
                });
            }
        }
    }
}

fn diff_model_foreign_keys(
    table: &str,
    old_model: &SchemaModel,
    new_model: &SchemaModel,
    ops: &mut Vec<MigrationOp>,
    warnings: &mut Vec<String>,
) {
    let old_col_names: BTreeSet<&str> = old_model.columns.iter().map(|c| c.name.as_str()).collect();

    for fk in &new_model.foreign_keys {
        // An FK on a newly added column rides the AddColumn emission; the
        // reconcile step only governs FKs whose column already exists live.
        if !old_col_names.contains(fk.column.as_str()) {
            continue;
        }

        let Some(live) = old_model
            .foreign_keys
            .iter()
            .find(|live| live.column == fk.column)
        else {
            ops.push(MigrationOp::AddForeignKey {
                table: table.to_string(),
                column: fk.column.clone(),
            });
            continue;
        };

        // Live `to_column` can be empty when the backend reports an
        // implicit-PK reference (SQLite); only a stated target can drift.
        let target_drift = live.to_table != fk.to_table
            || (!live.to_column.is_empty() && live.to_column != fk.to_column);
        // Compare via the canonical SQL rendering — sea-query's
        // `ForeignKeyAction` has no equality of its own.
        let action_drift = fk_action_sql(fk_action_from_str(live.on_delete.as_deref()))
            != fk_action_sql(fk_action_from_str(fk.on_delete.as_deref()));
        if !target_drift && !action_drift {
            continue;
        }

        match live.name.as_deref() {
            // A drifting constraint ferro does not own is never altered —
            // but it is never silent either.
            Some(name) if !is_ferro_fk_name(name) => {
                warnings.push(format!(
                    "Foreign key on '{}.{}' drifts from the model (live: REFERENCES {} \
                     ON DELETE {}; declared: REFERENCES {} ON DELETE {}), but the live \
                     constraint '{}' is not ferro-owned, so it is left untouched. \
                     Migrate it manually or with Alembic.",
                    table,
                    fk.column,
                    live.to_table,
                    fk_action_sql(fk_action_from_str(live.on_delete.as_deref())),
                    fk.to_table,
                    fk_action_sql(fk_action_from_str(fk.on_delete.as_deref())),
                    name,
                ));
            }
            _ => {
                ops.push(MigrationOp::RebuildForeignKey {
                    table: table.to_string(),
                    column: fk.column.clone(),
                    // SQLite exposes no live constraint names; fall back to
                    // the canonical name (unused there — emission warns).
                    old_name: live
                        .name
                        .clone()
                        .unwrap_or_else(|| fk_name(table, &fk.column, &live.to_table)),
                });
            }
        }
    }
}
