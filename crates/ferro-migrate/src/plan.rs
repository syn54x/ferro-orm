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
    EnumTypeProvenance, LiveRowPolicy, LiveRowSecurity, ResolvedStorage, declared_row_policy_names,
    drifted_check_names, dropped_row_security_warning, enum_label_strings, enum_type_provenance,
    excess_row_security_flag_statements, extra_check_names, extra_check_names_warning,
    extra_enum_labels, extra_enum_labels_warning, extra_row_policy_names_warning,
    fk_action_from_str, fk_action_sql, fk_name, is_ferro_fk_name, is_ferro_row_policy_name,
    missing_check_names, missing_enum_labels, missing_row_security_flag_statements,
    normalize_check_definition, normalize_row_policy_expr, plan_row_security_reconcile,
    quote_label, render_check_body, render_disable_row_security, render_enable_row_security,
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
///
/// Ahead of all of it — so a new table can reference a renamed one — the
/// renames `new`'s live rename hints declare ([`live_hints`], ADR-0032), one
/// unit per table ([`rename_ops`]): its table and column renames, then every
/// derived index, constraint and (Postgres) policy name, the renamed tables
/// before the tables that reference them. Everything after is planned from
/// `old` as the renames leave it, so a rename and a type change on one column
/// are the rename, then the type change. A live IR declares no hint, so the
/// plan from a declared `old` that already holds the new names, and every
/// plan over a modelset without hints, is unchanged by them. A refused hint
/// ([`HintError`]) applies no rename and stands in
/// [`MigrationPlan::always_warnings`] naming both sides; the generator
/// refuses it before writing anything.
pub fn plan_from_ir(
    old: &IrEnvelope<SchemaIrPayload>,
    new: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
    facts: &LiveFacts,
    options: PlanOptions,
) -> MigrationPlan {
    let hints = match live_hints(&old.payload, &new.payload) {
        Ok(hints) => hints,
        Err(refusal) => {
            let mut plan = plan_named(old, new, dialect, facts, options);
            plan.always_warnings
                .push(format!("rename hint refused: {refusal}"));
            return plan;
        }
    };
    if hints.is_empty() {
        return plan_named(old, new, dialect, facts, options);
    }
    let renamed = renamed_snapshot(old, &hints);
    let mut operations = rename_ops(old, &renamed, dialect);
    fact_renames(&mut operations, facts, old, &hints, dialect);
    let facts = renamed_facts(facts, &operations, new, dialect);
    let renamed = renamed_snapshot(old, &storage_hints(&hints, dialect));
    let mut plan = plan_named(&renamed, new, dialect, &facts, options);
    operations.append(&mut plan.operations);
    plan.operations = operations;
    plan
}

/// The renames of the names only a live database's [`LiveFacts`] carry — a
/// live IR holds no check and no row policy — added to `ops`, each at the end
/// of its table's unit so the table's renames stay one contiguous unit. Each
/// ferro-owned check and `rls_` policy name is re-derived by the function
/// that made it (a column check through `db_check_constraint_name`, a table
/// check and a policy through their suffix), exactly as [`renamed_snapshot`]
/// re-derives a declared one; a name an IR rename op already covers is left
/// alone. With [`LiveFacts::declared`] there is nothing to add.
fn fact_renames(
    ops: &mut Vec<MigrationOp>,
    facts: &LiveFacts,
    old: &IrEnvelope<SchemaIrPayload>,
    hints: &[Hint],
    dialect: Dialect,
) {
    use ferro_ddl_lowering::{
        db_check_constraint_name, row_policy_name, row_policy_short_name,
        table_check_constraint_name, table_check_suffix,
    };
    let renames = Renames { hints };
    let old_models = index_models(&old.payload.models);
    for (t_old, live) in &facts.tables {
        let Some(model) = old_models.get(t_old) else {
            continue;
        };
        let t_new = renames.table(t_old).to_string();
        let mut added = Vec::new();
        for check in live.checks.iter().filter(|check| check.ferro_owned) {
            let by_column = model.columns.iter().find_map(|col| {
                (check.name == db_check_constraint_name(t_old, &col.name))
                    .then(|| db_check_constraint_name(&t_new, renames.column(t_old, &col.name)))
            });
            let renamed = by_column.or_else(|| {
                table_check_suffix(t_old, &check.name)
                    .map(|suffix| table_check_constraint_name(&t_new, suffix))
            });
            if let Some(name) = renamed.filter(|name| *name != check.name) {
                added.push(MigrationOp::RenameConstraint {
                    table: t_new.clone(),
                    old: check.name.clone(),
                    new: name,
                });
            }
        }
        if dialect == Dialect::Postgres {
            for policy in live.row_security.policies.iter().filter(|p| p.ferro_owned) {
                if let Some(short) = row_policy_short_name(t_old, &policy.name) {
                    let name = row_policy_name(&t_new, short);
                    if name != policy.name {
                        added.push(MigrationOp::RenamePolicy {
                            table: t_new.clone(),
                            old: policy.name.clone(),
                            new: name,
                        });
                    }
                }
            }
        }
        added.retain(|op| !ops.contains(op));
        if added.is_empty() {
            continue;
        }
        let at = ops
            .iter()
            .rposition(|op| op.table() == Some(t_new.as_str()))
            .map_or(ops.len(), |last| last + 1);
        ops.splice(at..at, added);
    }
}

/// `facts` as the rename `ops` leave the live database: each table under its
/// new name, every renamed check, foreign key, index and policy under its new
/// name. A rename carries every column reference with it (Postgres and SQLite
/// both rewrite the bodies that name a renamed column), so a check or
/// shorthand policy whose live body equals its declaration rendered under the
/// old names (the one normalizer each: `normalize_check_definition`,
/// ADR-0015; `normalize_row_policy_expr`) reads as the declaration under the
/// new names. Any other live body is kept as read, so the drift decision
/// rebuilds it in this same plan. A rename the dialect
/// cannot run in place renames no fact: on SQLite a constraint keeps its live
/// name until a generated migration's rebuild renames it, so the plan reports
/// the catalog as it is. The facts of [`LiveFacts::declared`] carry no table
/// and are returned unchanged.
fn renamed_facts(
    facts: &LiveFacts,
    ops: &[MigrationOp],
    new: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
) -> LiveFacts {
    if facts.tables.is_empty() || ops.is_empty() {
        return facts.clone();
    }
    let new_models = index_models(&new.payload.models);
    let mut out = facts.clone();
    for op in ops {
        if let MigrationOp::RenameTable { old, new } = op
            && let Some(table) = out.tables.remove(old)
        {
            out.tables.insert(new.clone(), table);
        }
    }
    let touched: BTreeSet<&str> = ops.iter().filter_map(MigrationOp::table).collect();
    // The renames run backwards, from the ops themselves: what turns the new
    // names back into the ones the live facts were read under.
    let old_table = |table: &str| {
        ops.iter()
            .find_map(|op| match op {
                MigrationOp::RenameTable { old, new } if new == table => Some(old.clone()),
                _ => None,
            })
            .unwrap_or_else(|| table.to_string())
    };
    let reverse: Vec<Hint> = ops
        .iter()
        .filter_map(|op| match op {
            MigrationOp::RenameTable { old, new } => Some(Hint::Table {
                old: new.clone(),
                new: old.clone(),
            }),
            MigrationOp::RenameColumn { table, old, new } => Some(Hint::Column {
                table: old_table(table),
                old: new.clone(),
                new: old.clone(),
            }),
            _ => None,
        })
        .collect();
    for (table, live) in &mut out.tables {
        if !touched.contains(table.as_str()) {
            continue;
        }
        let renamed_name = |name: &str| {
            ops.iter()
                .find_map(|op| match op {
                    MigrationOp::RenameConstraint { .. } if dialect == Dialect::Sqlite => None,
                    MigrationOp::RenameIndex { table: t, old, new }
                    | MigrationOp::RenameConstraint { table: t, old, new }
                    | MigrationOp::RenamePolicy { table: t, old, new }
                        if t == table && old == name =>
                    {
                        Some(new.clone())
                    }
                    _ => None,
                })
                .unwrap_or_else(|| name.to_string())
        };
        let declared = new_models.get(table.as_str()).copied();
        // The declaration rendered under the names the live bodies were read
        // with, before the renames: the side a live body is compared with.
        let before = declared.map(|model| renamed_model(model, &Renames { hints: &reverse }));
        for check in &mut live.checks {
            check.name = renamed_name(&check.name);
            let (Some(model), Some(before)) = (declared, before.as_ref()) else {
                continue;
            };
            let bodies = model
                .table_checks
                .iter()
                .position(|c| c.name == check.name)
                .map(|i| {
                    (
                        render_table_check_body(&before.table_checks[i]),
                        render_table_check_body(&model.table_checks[i]),
                    )
                })
                .or_else(|| {
                    model
                        .checks
                        .iter()
                        .position(|c| c.name == check.name)
                        .map(|i| {
                            (
                                render_check_body(&before.checks[i]),
                                render_check_body(&model.checks[i]),
                            )
                        })
                });
            if let Some((was, now)) = bodies
                && normalize_check_definition(&was) == normalize_check_definition(&check.definition)
            {
                check.definition = format!("CHECK ({now})");
            }
        }
        for fk in &mut live.foreign_keys {
            fk.name = renamed_name(&fk.name);
        }
        for index in &mut live.indexes {
            index.name = renamed_name(&index.name);
        }
        for policy in &mut live.row_security.policies {
            policy.name = renamed_name(&policy.name);
            let clauses = declared.zip(before.as_ref()).and_then(|(model, before)| {
                let i = model
                    .row_security
                    .as_ref()?
                    .policies
                    .iter()
                    .position(|p| p.name == policy.name)?;
                let now_ir = &model.row_security.as_ref()?.policies[i];
                let was_ir = &before.row_security.as_ref()?.policies[i];
                if !matches!(now_ir.expr, ferro_schema_ir::RowPolicyExpr::Setting { .. }) {
                    return None;
                }
                Some((
                    row_policy_clauses(before, was_ir).ok()?,
                    row_policy_clauses(model, now_ir).ok()?,
                ))
            });
            let normalized = |expr: &Option<String>| expr.as_deref().map(normalize_row_policy_expr);
            if let Some(((was_using, was_check), (now_using, now_check))) = clauses
                && normalized(&was_using) == normalized(&policy.using)
                && normalized(&was_check) == normalized(&policy.with_check)
            {
                policy.using = now_using;
                policy.with_check = now_check;
            }
        }
    }
    out
}

/// Every change [`plan_from_ir`] plans once the tables and columns of `old`
/// and `new` are matched by name.
fn plan_named(
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
        // A table with no live facts is read from the `old` snapshot: the
        // planner is diffing two declared snapshots (the generator), never a
        // live database (the pass reads facts for every live table).
        let (old_view, table_facts, side) = match facts.tables.get(&new_model.table_name) {
            Some(live) => (Cow::Borrowed(old_model), Cow::Borrowed(live), OldSide::Live),
            None => (
                declared_live_view(old_model, dialect),
                Cow::Owned(declared_table_facts(old_model)),
                OldSide::Snapshot,
            ),
        };
        plan_existing_table(
            &old_view,
            new_model,
            dialect,
            &table_facts,
            side,
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

/// What the `old` side of an existing table is.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum OldSide {
    /// A live database, read through its facts (the reconciliation pass,
    /// `drift`).
    Live,
    /// A declared snapshot (the generator): every artifact on it is ferro's
    /// own declaration.
    Snapshot,
}

fn plan_existing_table(
    old_model: &SchemaModel,
    new_model: &SchemaModel,
    dialect: Dialect,
    facts: &LiveTableFacts,
    side: OldSide,
    options: PlanOptions,
    plan: &mut MigrationPlan,
) {
    let table = new_model.table_name.as_str();
    let mut ops = Vec::new();
    let mut column_drops = Vec::new();

    ops.extend(primary_key_change(table, old_model, new_model));
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
        side,
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
///
/// Between two declared snapshots ([`OldSide::Snapshot`], the generator)
/// the parent snapshot answers the two questions a live table cannot
/// (ADR-0019, ADR-0033): a raw body that differs is the author's edit, since
/// both texts are ferro's own copies of a declaration, so it is rebuilt like
/// a shorthand one; and row security the parent declared was installed by
/// ferro, so a declaration the target drops is torn down
/// ([`snapshot_flag_teardown`]) whether or not a ferro-named policy is left
/// to witness it. Every difference is then an op in a reviewed file, so the
/// decision's reports of live conditions (unverifiable and replaced bodies,
/// a teardown done) are not carried; a non-destructive plan still reports
/// the removals it withholds.
#[allow(clippy::too_many_arguments)]
fn plan_row_security(
    model: &SchemaModel,
    live: &LiveRowSecurity,
    dialect: Dialect,
    side: OldSide,
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
    let table = model.table_name.as_str();
    match side {
        OldSide::Live => always_warnings.extend(decision.warnings),
        OldSide::Snapshot if !destructive => always_warnings.extend(
            dropped_row_security_warning(model, live)
                .into_iter()
                .chain(extra_row_policy_names_warning(table, &decision.extra)),
        ),
        OldSide::Snapshot => {}
    }
    if dialect != Dialect::Postgres {
        return;
    }
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
    // Rebuilds in declaration order: drifted bodies, and on a snapshot the
    // edited raw ones.
    let rebuilt = |name: &String| {
        decision.drifted.contains(name)
            || (side == OldSide::Snapshot && decision.unverifiable.contains(name))
    };
    ops.extend(
        declared_row_policy_names(model)
            .into_iter()
            .filter(rebuilt)
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
        let teardown = match side {
            OldSide::Live => excess_row_security_flag_statements(model, live),
            OldSide::Snapshot => snapshot_flag_teardown(model, live),
        };
        ops.extend(
            teardown
                .iter()
                .filter_map(|statement| row_security_flag_op(table, statement)),
        );
    }
}

/// The flag teardown a file between two snapshots owes: the pass's
/// (`excess_row_security_flag_statements`), except that the parent snapshot
/// declaring row security is itself the proof ferro installed it (ADR-0033) —
/// the evidence the pass reads off a ferro-named policy — so a declaration
/// the target drops clears `FORCE` and `ENABLE` even with no policy left.
fn snapshot_flag_teardown(model: &SchemaModel, live: &LiveRowSecurity) -> Vec<String> {
    let table = model.table_name.as_str();
    match model.row_security {
        None => [
            live.forced.then(|| render_no_force_row_security(table)),
            live.enabled.then(|| render_disable_row_security(table)),
        ]
        .into_iter()
        .flatten()
        .collect(),
        Some(_) => excess_row_security_flag_statements(model, live),
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

/// Every enum the models declare, with the labels of its first declaring
/// column, every `(table, column)` that declares it, in model order, and its
/// label rename hints (the first declaring column that carries any).
/// [`declared_enum_types`] keeps the native Postgres types only (the types
/// that exist as database objects); [`declared_enum_labels`] every enum, how
/// ever it is stored (a `db_type="text"` column keeps its labels in its rows).
struct DeclaredEnumTypes {
    labels: BTreeMap<String, Vec<String>>,
    declaring: BTreeMap<String, Vec<(String, String)>>,
    renamed_labels: BTreeMap<String, ferro_schema_ir::SchemaRenamedLabels>,
}

/// The native enum type `col` is stored as, when it is one.
fn enum_type_of(col: &ferro_schema_ir::SchemaColumn) -> Option<(String, Vec<String>)> {
    match resolve_column_storage(col, Dialect::Postgres) {
        Ok(ResolvedStorage::PgEnum { type_name, labels }) => Some((type_name, labels)),
        _ => None,
    }
}

/// The enum `col` declares, how ever it is stored: the type name and labels
/// it would have as a native type (`resolve_column_storage` with the storage
/// override set aside, so the name and label spelling have one source).
fn enum_declaration(col: &ferro_schema_ir::SchemaColumn) -> Option<(String, Vec<String>)> {
    if col.db_type_explicit != Some(true) {
        return enum_type_of(col);
    }
    let declared = ferro_schema_ir::SchemaColumn {
        db_type: None,
        db_type_explicit: None,
        ..col.clone()
    };
    enum_type_of(&declared)
}

/// Whether a label rename rewrites `col`'s rows on `dialect`, rather than
/// renaming the label of a native type: the label is text in the rows (every
/// enum on SQLite, a `db_type="text"` enum on Postgres). Decided by the
/// column's storage (`resolve_column_storage`), the one decider.
pub(crate) fn relabels_rows(col: &ferro_schema_ir::SchemaColumn, dialect: Dialect) -> bool {
    !matches!(
        resolve_column_storage(col, dialect),
        Ok(ResolvedStorage::PgEnum { .. })
    )
}

/// How one column declares an enum: its type name and labels, or `None`.
type EnumOf = fn(&ferro_schema_ir::SchemaColumn) -> Option<(String, Vec<String>)>;

fn collect_enums(models: &[SchemaModel], of: EnumOf) -> DeclaredEnumTypes {
    let mut declared = DeclaredEnumTypes {
        labels: BTreeMap::new(),
        declaring: BTreeMap::new(),
        renamed_labels: BTreeMap::new(),
    };
    for model in models {
        for col in &model.columns {
            if let Some((type_name, labels)) = of(col) {
                declared
                    .declaring
                    .entry(type_name.clone())
                    .or_default()
                    .push((model.table_name.clone(), col.name.clone()));
                if let Some(hints) = &col.enum_renamed_labels {
                    declared
                        .renamed_labels
                        .entry(type_name.clone())
                        .or_insert_with(|| hints.clone());
                }
                declared.labels.entry(type_name).or_insert(labels);
            }
        }
    }
    declared
}

fn declared_enum_types(models: &[SchemaModel]) -> DeclaredEnumTypes {
    collect_enums(models, enum_type_of)
}

fn declared_enum_labels(models: &[SchemaModel]) -> DeclaredEnumTypes {
    collect_enums(models, enum_declaration)
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

// -- rename hints (ADR-0032) ----------------------------------------------------------

/// One live rename hint (ADR-0032): a declaration on the new snapshot saying
/// what a table or a column was called in the old one, and live because the
/// old snapshot still holds that name and lacks the new one.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(tag = "kind")]
pub enum Hint {
    /// `__ferro_renamed_from__`: table `old` is now `new`.
    Table {
        /// The table's name in the old snapshot.
        old: String,
        /// Its name in the new one.
        new: String,
    },
    /// `renamed_from=`: column `old` is now `new`.
    Column {
        /// The owning table as the new snapshot names it.
        table: String,
        /// The column's name in the old snapshot.
        old: String,
        /// Its name in the new one.
        new: String,
    },
    /// No declaration: every column of the old snapshot's enum type `old`
    /// now declares the type `new`, which the old snapshot lacks, and `old`
    /// is declared nowhere any more. An enum type's rename takes no hint; it
    /// is read off the columns that moved to it (ADR-0032).
    EnumType {
        /// The type's name in the old snapshot.
        old: String,
        /// Its name in the new one.
        new: String,
    },
    /// `__ferro_renamed_labels__ = {new: old}` on the enum class: label `old`
    /// of the type is now `new`.
    Label {
        /// The type as the new snapshot names it.
        type_name: String,
        /// The label in the old snapshot.
        old: String,
        /// The label in the new one.
        new: String,
    },
}

/// A declared rename hint `ferro migrate new` refuses (ADR-0032). Every hint
/// is checked, live or inert: neither shape can be meant.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum HintError {
    /// The hint's old name is still declared: a field (`field`) or a model
    /// (`field` is `None`) cannot be renamed from one the models keep.
    OldStillDeclared {
        /// The table declaring the hint.
        table: String,
        /// The hinted column, or `None` for a table hint.
        field: Option<String>,
        /// The name the hint claims.
        old: String,
    },
    /// Two declarations claim one old name: within `table` (two columns), or
    /// across the modelset (`table` is `None`, two tables).
    Ambiguous {
        /// The table whose columns claim it, or `None` for tables.
        table: Option<String>,
        /// The name they all claim.
        old: String,
        /// Every claimant, in declaration order: columns, or tables.
        claimants: Vec<String>,
    },
    /// A label rename hint whose old label the enum still declares: a label
    /// cannot be renamed from one the enum keeps.
    LabelStillDeclared {
        /// The enum class declaring the hint.
        enum_class: String,
        /// Its type name.
        type_name: String,
        /// The hinted label.
        new: String,
        /// The label the hint claims it was.
        old: String,
    },
    /// Two labels of one enum declare the same old label.
    AmbiguousLabel {
        /// The enum class declaring the hints.
        enum_class: String,
        /// Its type name.
        type_name: String,
        /// The label they all claim.
        old: String,
        /// Every claimant, in label order.
        claimants: Vec<String>,
    },
}

/// `"a"`, `"a" and "b"`, `"a", "b" and "c"`.
fn and_list(names: &[String]) -> String {
    match names {
        [] => String::new(),
        [only] => only.clone(),
        [init @ .., last] => format!("{} and {last}", init.join(", ")),
    }
}

impl std::fmt::Display for HintError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            HintError::OldStillDeclared {
                table,
                field: Some(field),
                old,
            } => write!(
                f,
                "{table}.{field} declares renamed_from=\"{old}\", but {table} still declares \
                 \"{old}\": a field cannot be renamed from one the model keeps; delete the \
                 hint or the old field"
            ),
            HintError::OldStillDeclared {
                table,
                field: None,
                old,
            } => write!(
                f,
                "table \"{table}\" declares __ferro_renamed_from__ = \"{old}\", but the models \
                 still declare table \"{old}\": delete the hint or the old model"
            ),
            HintError::Ambiguous {
                table: Some(table),
                old,
                claimants,
            } => {
                let fields: Vec<String> = claimants
                    .iter()
                    .map(|column| format!("{table}.{column}"))
                    .collect();
                write!(
                    f,
                    "{} all declare renamed_from=\"{old}\": one column becomes one column; \
                     keep the hint on the field \"{old}\" became",
                    and_list(&fields)
                )
            }
            HintError::Ambiguous {
                table: None,
                old,
                claimants,
            } => {
                let tables: Vec<String> = claimants.iter().map(|t| format!("\"{t}\"")).collect();
                write!(
                    f,
                    "tables {} all declare __ferro_renamed_from__ = \"{old}\": one table becomes \
                     one table; keep the hint on the model \"{old}\" became",
                    and_list(&tables)
                )
            }
            HintError::LabelStillDeclared {
                enum_class,
                type_name,
                new,
                old,
            } => write!(
                f,
                "enum {enum_class} (type \"{type_name}\") declares __ferro_renamed_labels__ \
                 {{\"{new}\": \"{old}\"}}, but {enum_class} still declares the label \"{old}\": a \
                 label cannot be renamed from one the enum keeps; delete the hint or the old \
                 member"
            ),
            HintError::AmbiguousLabel {
                enum_class,
                type_name,
                old,
                claimants,
            } => {
                let labels: Vec<String> = claimants.iter().map(|l| format!("\"{l}\"")).collect();
                write!(
                    f,
                    "enum {enum_class} (type \"{type_name}\") declares labels {} all renamed \
                     from \"{old}\": one label becomes one label; keep the hint on the label \
                     \"{old}\" became",
                    and_list(&labels)
                )
            }
        }
    }
}

impl std::error::Error for HintError {}

/// The live rename hints `new` declares against `old` (ADR-0032), tables
/// first, then columns, in model order.
///
/// A table hint is live when `old` holds the old table and lacks the new one;
/// a column hint when the old table (the hinted table's old name, under a
/// live table hint) holds the old column and lacks the new one. Every other
/// hint is inert, which is every hint after its migration.
///
/// # Errors
/// [`HintError::OldStillDeclared`] for a hint whose old name `new` still
/// declares, and [`HintError::Ambiguous`] for two hints on one old name —
/// checked for every hint, live or not.
pub fn live_hints(old: &SchemaIrPayload, new: &SchemaIrPayload) -> Result<Vec<Hint>, HintError> {
    let mut table_claims: Vec<(String, Vec<String>)> = Vec::new();
    for model in &new.models {
        if let Some(previous) = &model.renamed_from {
            if new.models.iter().any(|m| &m.table_name == previous) {
                return Err(HintError::OldStillDeclared {
                    table: model.table_name.clone(),
                    field: None,
                    old: previous.clone(),
                });
            }
            claim(&mut table_claims, previous, &model.table_name);
        }
        let mut column_claims: Vec<(String, Vec<String>)> = Vec::new();
        for col in &model.columns {
            let Some(previous) = &col.renamed_from else {
                continue;
            };
            if model.columns.iter().any(|c| &c.name == previous) {
                return Err(HintError::OldStillDeclared {
                    table: model.table_name.clone(),
                    field: Some(col.name.clone()),
                    old: previous.clone(),
                });
            }
            claim(&mut column_claims, previous, &col.name);
        }
        if let Some((previous, claimants)) = column_claims.into_iter().find(|(_, c)| c.len() > 1) {
            return Err(HintError::Ambiguous {
                table: Some(model.table_name.clone()),
                old: previous,
                claimants,
            });
        }
    }
    if let Some((previous, claimants)) = table_claims.into_iter().find(|(_, c)| c.len() > 1) {
        return Err(HintError::Ambiguous {
            table: None,
            old: previous,
            claimants,
        });
    }
    // Label hints are checked over every enum, how ever it is stored; type
    // renames only over the native types, the ones that exist as objects.
    let new_labels = declared_enum_labels(&new.models);
    let new_types = declared_enum_types(&new.models);
    for (type_name, renamed) in &new_labels.renamed_labels {
        let labels = new_labels
            .labels
            .get(type_name)
            .cloned()
            .unwrap_or_default();
        let mut label_claims: Vec<(String, Vec<String>)> = Vec::new();
        for (label, previous) in &renamed.labels {
            if labels.contains(previous) {
                return Err(HintError::LabelStillDeclared {
                    enum_class: renamed.enum_class.clone(),
                    type_name: type_name.clone(),
                    new: label.clone(),
                    old: previous.clone(),
                });
            }
            claim(&mut label_claims, previous, label);
        }
        if let Some((previous, claimants)) = label_claims.into_iter().find(|(_, c)| c.len() > 1) {
            return Err(HintError::AmbiguousLabel {
                enum_class: renamed.enum_class.clone(),
                type_name: type_name.clone(),
                old: previous,
                claimants,
            });
        }
    }

    let old_tables = index_models(&old.models);
    let mut tables = Vec::new();
    let mut columns = Vec::new();
    for model in &new.models {
        let table_hint = model.renamed_from.as_deref().filter(|previous| {
            old_tables.contains_key(*previous) && !old_tables.contains_key(&model.table_name)
        });
        if let Some(previous) = table_hint {
            tables.push(Hint::Table {
                old: previous.to_string(),
                new: model.table_name.clone(),
            });
        }
        let Some(old_model) = old_tables.get(table_hint.unwrap_or(&model.table_name)) else {
            continue;
        };
        let holds = |name: &str| old_model.columns.iter().any(|c| c.name == name);
        for col in &model.columns {
            if let Some(previous) = &col.renamed_from
                && holds(previous)
                && !holds(&col.name)
            {
                columns.push(Hint::Column {
                    table: model.table_name.clone(),
                    old: previous.clone(),
                    new: col.name.clone(),
                });
            }
        }
    }
    tables.extend(columns);

    // Enum types and labels are matched against `old` as the table and
    // column renames leave it: a column is the same column under its new name.
    let renamed_old: Vec<SchemaModel> = {
        let renames = Renames { hints: &tables };
        old.models
            .iter()
            .map(|model| renamed_model(model, &renames))
            .collect()
    };
    let old_types = declared_enum_types(&renamed_old);
    let old_labels = declared_enum_labels(&renamed_old);
    let types = enum_type_renames(&old_types, &new_types, &new.models);
    let mut labels = Vec::new();
    for (type_name, renamed) in &new_labels.renamed_labels {
        let old_name = types
            .iter()
            .find_map(|hint| match hint {
                Hint::EnumType { old, new } if new == type_name => Some(old.as_str()),
                _ => None,
            })
            .unwrap_or(type_name);
        let Some(held) = old_labels.labels.get(old_name) else {
            continue;
        };
        for (label, previous) in &renamed.labels {
            if held.contains(previous) && !held.contains(label) {
                labels.push(Hint::Label {
                    type_name: type_name.clone(),
                    old: previous.clone(),
                    new: label.clone(),
                });
            }
        }
    }
    tables.extend(types);
    tables.extend(labels);
    Ok(tables)
}

/// The enum type renames the columns imply (ADR-0032): a type `A` the old
/// snapshot declares and the new one does not, every column of which now
/// declares one and the same type `B` that the old snapshot lacks and no
/// other vanished type moved to. Anything less — a column of `A` dropped,
/// split across two types, or moved to a type that already exists — is no
/// rename: the plain column type change is planned instead.
fn enum_type_renames(
    old_types: &DeclaredEnumTypes,
    new_types: &DeclaredEnumTypes,
    new_models: &[SchemaModel],
) -> Vec<Hint> {
    let new_type_of = |table: &str, column: &str| {
        new_models
            .iter()
            .find(|model| model.table_name == table)?
            .columns
            .iter()
            .find(|col| col.name == column)
            .and_then(enum_type_of)
            .map(|(type_name, _)| type_name)
    };
    let mut candidates: Vec<(String, String)> = Vec::new();
    for (old, columns) in &old_types.declaring {
        if new_types.labels.contains_key(old) {
            continue;
        }
        let moved: BTreeSet<Option<String>> = columns
            .iter()
            .map(|(table, column)| new_type_of(table, column))
            .collect();
        if let [Some(new)] = moved.into_iter().collect::<Vec<_>>().as_slice()
            && !old_types.labels.contains_key(new)
        {
            candidates.push((old.clone(), new.clone()));
        }
    }
    candidates
        .iter()
        .filter(|(_, new)| candidates.iter().filter(|(_, n)| n == new).count() == 1)
        .map(|(old, new)| Hint::EnumType {
            old: old.clone(),
            new: new.clone(),
        })
        .collect()
}

fn claim(claims: &mut Vec<(String, Vec<String>)>, previous: &str, claimant: &str) {
    match claims.iter_mut().find(|(name, _)| name == previous) {
        Some((_, claimants)) => claimants.push(claimant.to_string()),
        None => claims.push((previous.to_string(), vec![claimant.to_string()])),
    }
}

/// The `hints` whose renames leave a column's storage on `dialect` as the
/// rest of a plan should see it. On SQLite an enum column is text as wide as
/// its longest label, so a label rename is an `UPDATE` of its rows that can
/// also widen or narrow the column: the label hints are left out, and the
/// plan still meets the width change as the column's type change.
pub(crate) fn storage_hints(hints: &[Hint], dialect: Dialect) -> Vec<Hint> {
    hints
        .iter()
        .filter(|hint| dialect != Dialect::Sqlite || !matches!(hint, Hint::Label { .. }))
        .cloned()
        .collect()
}

/// `hints` run the other way: what turns the new snapshot back into the old
/// one. A down reverses its step's renames with these (ADR-0033).
pub fn reverse_hints(hints: &[Hint]) -> Vec<Hint> {
    let old_table = |table: &str| {
        hints
            .iter()
            .find_map(|hint| match hint {
                Hint::Table { old, new } if new == table => Some(old.clone()),
                _ => None,
            })
            .unwrap_or_else(|| table.to_string())
    };
    hints
        .iter()
        .map(|hint| match hint {
            Hint::Table { old, new } => Hint::Table {
                old: new.clone(),
                new: old.clone(),
            },
            Hint::Column { table, old, new } => Hint::Column {
                table: old_table(table),
                old: new.clone(),
                new: old.clone(),
            },
            Hint::EnumType { old, new } => Hint::EnumType {
                old: new.clone(),
                new: old.clone(),
            },
            // A label hint applies after its type's rename: backwards, after
            // the type is renamed back.
            Hint::Label {
                type_name,
                old,
                new,
            } => Hint::Label {
                type_name: hints
                    .iter()
                    .find_map(|hint| match hint {
                        Hint::EnumType { old, new } if new == type_name => Some(old.clone()),
                        _ => None,
                    })
                    .unwrap_or_else(|| type_name.clone()),
                old: new.clone(),
                new: old.clone(),
            },
        })
        .collect()
}

/// The renames `hints` make, keyed by the old snapshot's names.
struct Renames<'a> {
    hints: &'a [Hint],
}

impl Renames<'_> {
    /// The new name of the old table `table`.
    fn table<'n>(&'n self, table: &'n str) -> &'n str {
        self.hints
            .iter()
            .find_map(|hint| match hint {
                Hint::Table { old, new } if old == table => Some(new.as_str()),
                _ => None,
            })
            .unwrap_or(table)
    }

    /// The new name of column `column` of the old table `table`.
    fn column<'n>(&'n self, table: &str, column: &'n str) -> &'n str {
        let new_table = self.table(table);
        self.hints
            .iter()
            .find_map(|hint| match hint {
                Hint::Column { table: t, old, new } if t == new_table && old == column => {
                    Some(new.as_str())
                }
                _ => None,
            })
            .unwrap_or(column)
    }

    /// `col` of the old snapshot with its enum type renamed (a native type:
    /// the only kind that exists as an object), then its labels, how ever
    /// they are stored (a label hint names the type as its type rename leaves
    /// it). Each label keeps its position, so the label order is unchanged.
    /// Returns the `(old, new)` labels renamed.
    fn enum_column(&self, col: &mut ferro_schema_ir::SchemaColumn) -> Vec<(String, String)> {
        let mut renamed = Vec::new();
        let Some((mut type_name, _)) = enum_declaration(col) else {
            return renamed;
        };
        if enum_type_of(col).is_some() {
            for hint in self.hints {
                match hint {
                    Hint::EnumType { old, new } if *old == type_name => {
                        col.enum_type_name = Some(new.clone());
                        type_name = new.clone();
                        break;
                    }
                    _ => {}
                }
            }
        }
        let Some(values) = col.enum_values.as_mut() else {
            return renamed;
        };
        for hint in self.hints {
            let Hint::Label {
                type_name: t,
                old,
                new,
            } = hint
            else {
                continue;
            };
            if *t != type_name {
                continue;
            }
            for value in values.iter_mut() {
                if enum_label_strings(std::slice::from_ref(value)).first() == Some(old) {
                    *value = serde_json::Value::String(new.clone());
                    renamed.push((old.clone(), new.clone()));
                }
            }
        }
        renamed
    }
}

/// A single-column index or unique naming function (`table`, `column`).
type SingleName = fn(&str, &str) -> String;
/// A composite index or unique naming function (`table`, `columns`).
type CompositeName = fn(&str, &[&str]) -> String;

/// `name` re-derived for the renamed table and columns, when the naming
/// function produced it from the old ones; otherwise unchanged.
fn rederived(name: &str, before: String, after: String) -> String {
    if name == before {
        after
    } else {
        name.to_string()
    }
}

fn renamed_check_expr(
    expr: &ferro_schema_ir::CheckExpr,
    column: &dyn Fn(&str) -> String,
) -> ferro_schema_ir::CheckExpr {
    use ferro_schema_ir::{CheckExpr, CheckOperand};
    match expr {
        CheckExpr::And { left, right } => CheckExpr::And {
            left: Box::new(renamed_check_expr(left, column)),
            right: Box::new(renamed_check_expr(right, column)),
        },
        CheckExpr::Or { left, right } => CheckExpr::Or {
            left: Box::new(renamed_check_expr(left, column)),
            right: Box::new(renamed_check_expr(right, column)),
        },
        CheckExpr::Not { child } => CheckExpr::Not {
            child: Box::new(renamed_check_expr(child, column)),
        },
        CheckExpr::IsNull { column: c } => CheckExpr::IsNull { column: column(c) },
        CheckExpr::IsNotNull { column: c } => CheckExpr::IsNotNull { column: column(c) },
        CheckExpr::Cmp {
            column: c,
            op,
            other,
        } => CheckExpr::Cmp {
            column: column(c),
            op: *op,
            other: match other {
                CheckOperand::Column { name } => CheckOperand::Column { name: column(name) },
                literal => literal.clone(),
            },
        },
        CheckExpr::In { column: c, values } => CheckExpr::In {
            column: column(c),
            values: values.clone(),
        },
        CheckExpr::Like { column: c, pattern } => CheckExpr::Like {
            column: column(c),
            pattern: pattern.clone(),
        },
    }
}

/// `model` of the old snapshot as the renames leave it: its table and columns
/// under their new names, every reference to them followed, and every
/// ferro-owned name re-derived by the naming function that made it.
fn renamed_model(model: &SchemaModel, renames: &Renames<'_>) -> SchemaModel {
    use ferro_ddl_lowering::{
        composite_index_name, composite_unique_index_name, db_check_constraint_name, fk_name,
        row_policy_name, row_policy_short_name, single_index_name, single_unique_index_name,
        table_check_constraint_name, table_check_suffix,
    };
    let t_old = model.table_name.as_str();
    let t_new = renames.table(t_old).to_string();
    let col = |name: &str| renames.column(t_old, name).to_string();
    let mut out = model.clone();
    out.table_name = t_new.clone();
    // A join table's model is named for its table.
    if model.model_name == t_old {
        out.model_name = t_new.clone();
    }
    let mut relabelled: BTreeMap<String, Vec<(String, String)>> = BTreeMap::new();
    for column in &mut out.columns {
        column.name = col(&column.name);
        let renamed = renames.enum_column(column);
        if !renamed.is_empty() {
            relabelled.insert(column.name.clone(), renamed);
        }
    }
    for fk in &mut out.foreign_keys {
        let (c_old, to_old) = (fk.column.clone(), fk.to_table.clone());
        fk.column = col(&c_old);
        fk.to_table = renames.table(&to_old).to_string();
        fk.to_column = renames.column(&to_old, &fk.to_column).to_string();
        fk.name = fk.name.as_deref().map(|name| {
            rederived(
                name,
                fk_name(t_old, &c_old, &to_old),
                fk_name(&t_new, &fk.column, &fk.to_table),
            )
        });
    }
    let index_name = |name: &str, cols_old: &[String], cols_new: &[String], unique: bool| {
        let old_refs: Vec<&str> = cols_old.iter().map(String::as_str).collect();
        let new_refs: Vec<&str> = cols_new.iter().map(String::as_str).collect();
        let (single, composite): (SingleName, CompositeName) = if unique {
            (single_unique_index_name, composite_unique_index_name)
        } else {
            (single_index_name, composite_index_name)
        };
        if let ([one_old], [one_new]) = (old_refs.as_slice(), new_refs.as_slice())
            && name == single(t_old, one_old)
        {
            return single(&t_new, one_new);
        }
        rederived(
            name,
            composite(t_old, &old_refs),
            composite(&t_new, &new_refs),
        )
    };
    for index in &mut out.indexes {
        let cols_new: Vec<String> = index.columns.iter().map(|c| col(c)).collect();
        index.name = index_name(&index.name, &index.columns, &cols_new, index.unique);
        index.columns = cols_new;
    }
    for unique in &mut out.uniques {
        let cols_new: Vec<String> = unique.columns.iter().map(|c| col(c)).collect();
        unique.name = index_name(&unique.name, &unique.columns, &cols_new, true);
        unique.columns = cols_new;
    }
    for check in &mut out.checks {
        let c_new = col(&check.column);
        // A `db_check` over a relabelled column names the labels it allows.
        for (old, new) in relabelled.get(&c_new).into_iter().flatten() {
            let (old, new) = (quote_label(old), quote_label(new));
            for value in &mut check.values {
                if *value == old {
                    *value = new.clone();
                }
            }
        }
        check.name = rederived(
            &check.name,
            db_check_constraint_name(t_old, &check.column),
            db_check_constraint_name(&t_new, &c_new),
        );
        check.column = c_new;
    }
    for check in &mut out.table_checks {
        if let Some(suffix) = table_check_suffix(t_old, &check.name) {
            check.name = rederived(
                &check.name,
                table_check_constraint_name(t_old, suffix),
                table_check_constraint_name(&t_new, suffix),
            );
        }
        check.predicate = renamed_check_expr(&check.predicate, &|c| col(c));
    }
    if let Some(declaration) = &mut out.row_security {
        for policy in &mut declaration.policies {
            if let Some(short) = row_policy_short_name(t_old, &policy.name) {
                policy.name = rederived(
                    &policy.name,
                    row_policy_name(t_old, short),
                    row_policy_name(&t_new, short),
                );
            }
            if let ferro_schema_ir::RowPolicyExpr::Setting { column, .. } = &mut policy.expr {
                *column = col(column);
            }
        }
    }
    out
}

/// The name `hints` give the table `table` (unchanged when no hint renames it).
pub(crate) fn renamed_table(hints: &[Hint], table: &str) -> String {
    Renames { hints }.table(table).to_string()
}

/// `old` as `hints` leave it: what the database holds once the renames have
/// run, and so the side every other change of the migration is planned
/// against. Models stay in `old`'s order, one for one.
pub(crate) fn renamed_snapshot(
    old: &IrEnvelope<SchemaIrPayload>,
    hints: &[Hint],
) -> IrEnvelope<SchemaIrPayload> {
    let renames = Renames { hints };
    let mut out = old.clone();
    out.payload.models = old
        .payload
        .models
        .iter()
        .map(|model| renamed_model(model, &renames))
        .collect();
    out
}

/// The rename ops that turn `old` into `renamed` (its [`renamed_snapshot`]),
/// table by table — the renamed tables first, the tables that reference them
/// after — each table's as one contiguous unit: its table rename, its column
/// renames, then its derived names (indexes, constraints, Postgres policies).
/// The reconciliation pass runs each table's consecutive ops in one
/// transaction; a generated step runs every table and column rename first.
pub(crate) fn rename_ops(
    old: &IrEnvelope<SchemaIrPayload>,
    renamed: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
) -> Vec<MigrationOp> {
    let mut pairs: Vec<(&SchemaModel, &SchemaModel)> = old
        .payload
        .models
        .iter()
        .zip(&renamed.payload.models)
        .collect();
    let owns_a_rename = |(o, r): &(&SchemaModel, &SchemaModel)| {
        o.table_name != r.table_name
            || o.columns
                .iter()
                .zip(&r.columns)
                .any(|(a, b)| a.name != b.name)
    };
    // Stable: the renamed tables' own names first, then referencing tables'.
    pairs.sort_by_key(|pair| !owns_a_rename(pair));

    let mut out = Vec::new();
    for (o, r) in pairs {
        let table = r.table_name.clone();
        if o.table_name != r.table_name {
            out.push(MigrationOp::RenameTable {
                old: o.table_name.clone(),
                new: table.clone(),
            });
        }
        for (a, b) in o.columns.iter().zip(&r.columns) {
            if a.name != b.name {
                out.push(MigrationOp::RenameColumn {
                    table: table.clone(),
                    old: a.name.clone(),
                    new: b.name.clone(),
                });
            }
        }
        for ((a, _, _), (b, _, _)) in emit::standalone_indexes(o)
            .into_iter()
            .zip(emit::standalone_indexes(r))
        {
            if a != b {
                out.push(MigrationOp::RenameIndex {
                    table: table.clone(),
                    old: a,
                    new: b,
                });
            }
        }
        let fk_names = |m: &SchemaModel| -> Vec<String> {
            m.foreign_keys
                .iter()
                .map(|fk| emit::fk_constraint_name(&m.table_name, fk))
                .collect()
        };
        let check_names = |m: &SchemaModel| -> Vec<String> {
            m.checks
                .iter()
                .map(|c| c.name.clone())
                .chain(m.table_checks.iter().map(|c| c.name.clone()))
                .collect()
        };
        for (a, b) in fk_names(o)
            .into_iter()
            .zip(fk_names(r))
            .chain(check_names(o).into_iter().zip(check_names(r)))
        {
            if a != b {
                out.push(MigrationOp::RenameConstraint {
                    table: table.clone(),
                    old: a,
                    new: b,
                });
            }
        }
        if dialect == Dialect::Postgres
            && let (Some(a), Some(b)) = (&o.row_security, &r.row_security)
        {
            for (pa, pb) in a.policies.iter().zip(&b.policies) {
                if pa.name != pb.name {
                    out.push(MigrationOp::RenamePolicy {
                        table: table.clone(),
                        old: pa.name.clone(),
                        new: pb.name.clone(),
                    });
                }
            }
        }
    }
    out.extend(enum_rename_ops(old, renamed, dialect));
    out
}

/// The enum renames that turn `old` into `renamed`, after every table's unit
/// (a SQLite label `UPDATE` names its tables and columns as those renames
/// leave them): each type rename (Postgres only — SQLite has no enum types),
/// then each label rename, over every column of its type.
fn enum_rename_ops(
    old: &IrEnvelope<SchemaIrPayload>,
    renamed: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
) -> Vec<MigrationOp> {
    let declaring = declared_enum_labels(&renamed.payload.models).declaring;
    let mut types = Vec::new();
    let mut labels = Vec::new();
    let columns = old
        .payload
        .models
        .iter()
        .zip(&renamed.payload.models)
        .flat_map(|(o, r)| o.columns.iter().zip(&r.columns));
    for (a, b) in columns {
        let (Some((type_a, labels_a)), Some((type_b, labels_b))) =
            (enum_declaration(a), enum_declaration(b))
        else {
            continue;
        };
        if type_a != type_b && dialect == Dialect::Postgres && enum_type_of(a).is_some() {
            let op = MigrationOp::RenameEnumType {
                old: type_a,
                new: type_b.clone(),
            };
            if !types.contains(&op) {
                types.push(op);
            }
        }
        for (label_a, label_b) in labels_a.into_iter().zip(labels_b) {
            if label_a == label_b {
                continue;
            }
            let op = MigrationOp::RenameEnumLabel {
                type_name: type_b.clone(),
                old: label_a,
                new: label_b,
                columns: declaring.get(&type_b).cloned().unwrap_or_default(),
            };
            if !labels.contains(&op) {
                labels.push(op);
            }
        }
    }
    types.extend(labels);
    types
}

pub(crate) fn index_models(models: &[SchemaModel]) -> BTreeMap<String, &SchemaModel> {
    models
        .iter()
        .map(|model| (model.table_name.clone(), model))
        .collect()
}

/// The table's primary-key columns, in column order.
fn primary_key_columns(model: &SchemaModel) -> Vec<String> {
    model
        .columns
        .iter()
        .filter(|col| col.primary_key)
        .map(|col| col.name.clone())
        .collect()
}

/// A [`MigrationOp::ChangePrimaryKey`] when the two models' primary keys are
/// not the same set of columns. Compared as sets: a live table reports its
/// columns in catalog order, a declared one in model order.
fn primary_key_change(
    table: &str,
    old_model: &SchemaModel,
    new_model: &SchemaModel,
) -> Option<MigrationOp> {
    let (from, to) = (
        primary_key_columns(old_model),
        primary_key_columns(new_model),
    );
    let set = |cols: &[String]| cols.iter().cloned().collect::<BTreeSet<_>>();
    (set(&from) != set(&to)).then(|| MigrationOp::ChangePrimaryKey {
        table: table.to_string(),
        from,
        to,
    })
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
        // A column declared with one native enum type on both sides that moved
        // to another is a type change too: the storage decision reads a live
        // native-enum column as already at any enum target, and only a
        // declared `old` names its type. A move of every column of a type is
        // its rename, applied before this diff runs (ADR-0032).
        let moved_enum = dialect == Dialect::Postgres
            && matches!(
                (enum_type_of(old_col), enum_type_of(new_col)),
                (Some((a, _)), Some((b, _))) if a != b
            );
        if moved_enum || schema_columns_storage_drift(old_col, new_col, dialect) {
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

    // Both sides through `standalone_indexes`: a declared snapshot carries a
    // unique in `uniques`, a live one in `indexes` (introspection leaves
    // `uniques` empty), and either way it is one standalone index.
    let old_by_name: BTreeMap<String, (Vec<String>, bool)> = emit::standalone_indexes(old_model)
        .into_iter()
        .map(|(name, columns, unique)| (name, (columns, unique)))
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
