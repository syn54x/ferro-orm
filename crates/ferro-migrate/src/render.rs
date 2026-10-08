//! Per-dialect rendering of a [`crate::Plan`]: each op becomes the exact
//! statements every migration door executes for it, through the
//! `ferro_ddl_lowering` renderers the reconciliation pass has always used.

use crate::emit::{
    emit_add_column, emit_alter_column_nullability, emit_alter_column_type, find_column,
    find_foreign_key, find_model, render_add_fk_sql, render_index_sql, standalone_indexes,
};
use crate::plan::{index_models, planned_before, relabels_rows};
use crate::{Dialect, EmissionError, MigrationOp, Report, render_create_table};
use ferro_ddl_lowering::{
    ConstraintMode, InPlaceChange, IndexMode, ResolvedStorage, Subject, fk_action_from_str,
    fk_action_sql, primary_key_kept_warning, quote_ident, render_check_addition, render_check_drop,
    render_check_rebuild, render_create_row_policy, render_disable_row_security,
    render_drop_constraint, render_drop_index_sql, render_drop_row_policy,
    render_enable_row_security, render_force_row_security, render_label_update,
    render_no_force_row_security, render_pg_enum_add_value, render_pg_enum_create_type,
    render_pg_enum_drop_type, render_pg_enum_rename_type, render_pg_enum_rename_value,
    render_rename_column, render_rename_constraint, render_rename_index, render_rename_policy,
    render_rename_table, render_validate_constraint, resolve_column_storage, row_policy_clauses,
    row_policy_rebuild_statements, row_security_statements, sqlite_in_place_report,
};
use ferro_schema_ir::{IrEnvelope, SchemaIrPayload, SchemaModel};
use std::collections::{BTreeSet, HashSet};

/// One planned op with what it renders to on one dialect.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize)]
pub struct RenderedOp {
    /// The op.
    pub op: MigrationOp,
    /// Statements to execute, in order (none when the dialect can only
    /// report).
    pub statements: Vec<String>,
    /// What rendering reports (a backend limitation that skips the op, a
    /// refused cast). One that [`Report::blocks`] stands in for a statement
    /// the op has none of.
    pub reports: Vec<Report>,
    /// For an `AddTable`, the row-security statements among `statements`
    /// (its flags and policies, last): what a consumer that creates the
    /// table its own way still executes as written (the Alembic bridge's
    /// `create_table`). Empty for every other op.
    pub row_security_statements: Vec<String>,
}

impl RenderedOp {
    fn new(op: &MigrationOp) -> Self {
        Self {
            op: op.clone(),
            statements: Vec::new(),
            reports: Vec::new(),
            row_security_statements: Vec::new(),
        }
    }
}

/// Reject an envelope whose declarations cannot render: a row policy whose
/// clauses do not resolve (a shorthand over a column the table lacks, or over
/// a storage the shorthand does not support). The planner reads such a
/// policy as absent; [`crate::Plan::render`] calls this on both sides, so a
/// plan built from invalid IR never yields a statement.
///
/// # Errors
/// An [`EmissionError`] naming the policy and the reason.
pub fn validate_schema_ir(ir: &IrEnvelope<SchemaIrPayload>) -> Result<(), EmissionError> {
    for model in &ir.payload.models {
        let Some(declaration) = model.row_security.as_ref() else {
            continue;
        };
        for policy in &declaration.policies {
            row_policy_clauses(model, policy).map_err(|message| EmissionError {
                message: format!(
                    "row policy '{}' on table '{}' cannot render: {message}",
                    policy.name, model.table_name
                ),
            })?;
        }
    }
    Ok(())
}

/// `ops` rendered for `dialect`, as one plan from `old` to `new`, in order:
/// the generator's own renders, between the stages it builds on either side
/// of a step, which no single plan holds. Every op of a plan with renames
/// names its table and columns as the renames leave `old`, so `old` is read
/// as the planner leaves it ([`crate::plan::planned_before`]). The same
/// renderers in a mode ([`render_from`]), never a second one (AGENTS.md §
/// I-1).
pub(crate) fn render_ops(
    ops: &[MigrationOp],
    old: &IrEnvelope<SchemaIrPayload>,
    new: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
    constraints: ConstraintMode,
) -> Result<Vec<RenderedOp>, EmissionError> {
    validate_schema_ir(old)?;
    render_from(
        ops,
        planned_before(old, new, dialect).as_ref(),
        new,
        dialect,
        constraints,
    )
}

/// `ops` rendered for `dialect` against `old` exactly as given (already the
/// planned-before side) and `new`, in order, every foreign key and check
/// added in `constraints` mode: `NOT VALID` is the generator's staged
/// constraint on an existing Postgres table (ADR-0043). An op reads the
/// declaration it creates from `new` and the shape it changes from `old`.
///
/// A native enum type a `CreateEnumType` op among `ops` creates is created
/// there and only there; an `AddTable` / `AddColumn` of any other native enum
/// type keeps its idempotent guard.
///
/// # Errors
/// An [`EmissionError`] when a side carries a declaration that cannot render,
/// when an op cannot be applied safely (adding a NOT NULL column with no
/// backfill, dropping a primary-key column), when the IR lacks what an op
/// names, or when the op cannot exist on `dialect`.
pub(crate) fn render_from(
    ops: &[MigrationOp],
    old: &IrEnvelope<SchemaIrPayload>,
    new: &IrEnvelope<SchemaIrPayload>,
    dialect: Dialect,
    constraints: ConstraintMode,
) -> Result<Vec<RenderedOp>, EmissionError> {
    validate_schema_ir(old)?;
    validate_schema_ir(new)?;
    let old_models = index_models(&old.payload.models);
    let new_models = index_models(&new.payload.models);
    // A type this plan creates, or that `old` already declares, needs no
    // guarded `CREATE TYPE` beside a table or column of it (ADR-0021: a reused
    // type is neither created nor dropped). A live `old` declares no enum
    // type — introspection reads none — so the pass keeps every guard.
    let types_created_by_plan: BTreeSet<String> = ops
        .iter()
        .filter_map(|op| match op {
            MigrationOp::CreateEnumType { type_name, .. } => Some(type_name.clone()),
            _ => None,
        })
        .chain(
            old.payload
                .models
                .iter()
                .flat_map(|model| &model.columns)
                .filter_map(|col| match resolve_column_storage(col, Dialect::Postgres) {
                    Ok(ResolvedStorage::PgEnum { type_name, .. }) => Some(type_name),
                    _ => None,
                }),
        )
        .collect();
    // Enum types shared across new tables: each idempotent guard once.
    let mut emitted_type_guards: HashSet<String> = HashSet::new();

    let mut rendered = Vec::with_capacity(ops.len());
    for op in ops {
        let mut out = RenderedOp::new(op);
        match op {
            MigrationOp::AddEnumLabel { type_name, label } => {
                require_postgres(op, dialect)?;
                out.statements
                    .push(render_pg_enum_add_value(type_name, label));
            }
            MigrationOp::CreateEnumType { type_name, labels } => {
                require_postgres(op, dialect)?;
                out.statements
                    .push(render_pg_enum_create_type(type_name, labels));
            }
            MigrationOp::DropEnumType { type_name } => {
                require_postgres(op, dialect)?;
                out.statements.push(render_pg_enum_drop_type(type_name));
            }
            // ADR-0011's warning only: the generator renders a removal's
            // backfill and swap-type contract (`generate::enums`).
            MigrationOp::RemoveEnumLabel {
                type_name, label, ..
            } => {
                out.reports
                    .extend(ferro_ddl_lowering::extra_enum_labels_warning(
                        type_name,
                        std::slice::from_ref(label),
                    ));
            }
            MigrationOp::RenameEnumLabel {
                type_name,
                old,
                new,
                columns,
            } => {
                // A native type renames its label once and its rows follow;
                // a column that keeps the label as text in its rows (every
                // enum on SQLite, a `db_type="text"` one on Postgres) has its
                // rows rewritten ([`relabels_rows`], the one decider).
                let mut native = false;
                let mut updates = Vec::new();
                for (table, column) in columns {
                    let model = find_model(&old_models, table)?;
                    if relabels_rows(find_column(model, column)?, dialect) {
                        updates.push((table, column, model));
                    } else {
                        native = true;
                    }
                }
                if native {
                    out.statements
                        .push(render_pg_enum_rename_value(type_name, old, new));
                }
                for (table, column, model) in updates {
                    let update = render_label_update(table, column, old, new);
                    // On Postgres a `db_check` over the column still allows
                    // only the old label: dropped before the rows change and
                    // added back (validated) over the new labels after.
                    let check = (dialect == Dialect::Postgres)
                        .then(|| model.checks.iter().find(|check| check.column == *column))
                        .flatten();
                    match check {
                        Some(check) => {
                            let rebuilt = render_check_rebuild(
                                table,
                                find_model(&new_models, table)?,
                                &check.name,
                                dialect,
                                ConstraintMode::Plain,
                            )
                            .ok_or_else(|| EmissionError {
                                message: format!(
                                    "Label rename on '{table}.{column}' has no CHECK '{}' in \
                                     the declared IR",
                                    check.name
                                ),
                            })?;
                            let mut statements = rebuilt.statements.into_iter();
                            out.statements.extend(statements.next());
                            out.statements.push(update);
                            out.statements.extend(statements);
                        }
                        None => out.statements.push(update),
                    }
                }
            }
            MigrationOp::RenameEnumType { old, new } => {
                require_postgres(op, dialect)?;
                out.statements.push(render_pg_enum_rename_type(old, new));
            }
            MigrationOp::AddTable { table } => {
                let model = find_model(&new_models, table)?;
                let emission = render_create_table(model, dialect)?;
                let created_guards = created_type_guards(model, &types_created_by_plan, dialect);
                for guard in emission.pre_create_sqls {
                    if !created_guards.contains(&guard) && emitted_type_guards.insert(guard.clone())
                    {
                        out.statements.push(guard);
                    }
                }
                out.statements.push(emission.create_sql);
                out.statements.extend(emission.post_create_sqls);
                out.reports.extend(emission.reports);
                // The same statements the emission ends with, from the one
                // function it takes them from.
                out.row_security_statements = row_security_statements(model, dialect)
                    .map_err(|message| EmissionError { message })?
                    .statements;
            }
            MigrationOp::DropTable { table } => {
                out.statements
                    .push(format!("DROP TABLE {}", quote_ident(table)));
            }
            MigrationOp::RenameTable { old, new } => {
                out.statements.push(render_rename_table(old, new));
            }
            MigrationOp::RenameColumn { table, old, new } => {
                out.statements.push(render_rename_column(table, old, new));
            }
            MigrationOp::RenameIndex { table, old, new } => match dialect {
                Dialect::Postgres => out.statements.push(render_rename_index(old, new)),
                // SQLite has no index rename: drop it, then build it under its
                // new name with the statement every door creates it with.
                Dialect::Sqlite => {
                    let (columns, unique) = standalone_indexes(find_model(&new_models, table)?)
                        .into_iter()
                        .find(|(name, _, _)| name == new)
                        .map(|(_, columns, unique)| (columns, unique))
                        .ok_or_else(|| EmissionError {
                            message: format!(
                                "Index rename '{old}' → '{new}' on table '{table}' has no index \
                                 '{new}' in the declared IR"
                            ),
                        })?;
                    out.statements
                        .push(render_drop_index_sql(old, IndexMode::Plain));
                    out.statements.push(render_index_sql(
                        table,
                        new,
                        &columns,
                        unique,
                        dialect,
                        IndexMode::Plain,
                    ));
                }
            },
            MigrationOp::RenameConstraint { table, old, new } => match dialect {
                Dialect::Postgres => out
                    .statements
                    .push(render_rename_constraint(table, old, new)),
                Dialect::Sqlite => out.reports.push(sqlite_in_place_report(
                    InPlaceChange::RenameConstraint,
                    Subject::table(table),
                    format!(
                        "Constraint '{old}' on '{table}' is now named '{new}', and SQLite \
                         cannot rename a table constraint in place; `ferro migrate new` \
                         renames it by rebuilding the table."
                    ),
                )),
            },
            MigrationOp::RenamePolicy { table, old, new } => {
                require_postgres(op, dialect)?;
                out.statements.push(render_rename_policy(table, old, new));
            }
            MigrationOp::AddColumn { table, column } => {
                let model = find_model(&new_models, table)?;
                let emission = emit_add_column(
                    table,
                    column,
                    model,
                    dialect,
                    &types_created_by_plan,
                    constraints,
                )?;
                out.statements.extend(emission.statements);
                out.reports.extend(emission.reports);
            }
            MigrationOp::DropColumn { table, column } => {
                let old_model = find_model(&old_models, table)?;
                let old_col = find_column(old_model, column)?;
                if old_col.primary_key {
                    return Err(EmissionError {
                        message: format!(
                            "Cannot drop column '{}.{}': it is part of the primary key. \
                             Primary-key changes need a reviewed migration \
                             (`ferro migrate new`).",
                            table, column
                        ),
                    });
                }
                out.statements.push(format!(
                    "ALTER TABLE {} DROP COLUMN {}",
                    quote_ident(table),
                    quote_ident(column)
                ));
            }
            MigrationOp::AlterColumnType { table, column } => {
                let old_col = find_column(find_model(&old_models, table)?, column)?;
                let new_col = find_column(find_model(&new_models, table)?, column)?;
                let emission = emit_alter_column_type(table, column, old_col, new_col, dialect)?;
                out.statements.extend(emission.statements);
                out.reports.extend(emission.reports);
            }
            MigrationOp::AlterColumnNullability { table, column } => {
                let old_col = find_column(find_model(&old_models, table)?, column)?;
                let new_col = find_column(find_model(&new_models, table)?, column)?;
                let emission =
                    emit_alter_column_nullability(table, column, old_col, new_col, dialect);
                out.statements.extend(emission.statements);
                out.reports.extend(emission.reports);
            }
            // No door changes a primary key in place: warn and skip.
            MigrationOp::ChangePrimaryKey { table, from, to } => {
                out.reports.push(primary_key_kept_warning(table, to, from));
            }
            MigrationOp::AddIndex {
                table,
                name,
                columns,
                unique,
            } => {
                out.statements.push(render_index_sql(
                    table,
                    name,
                    columns,
                    *unique,
                    dialect,
                    IndexMode::Plain,
                ));
            }
            // DROP INDEX is schema-scoped (not table-qualified) on both
            // dialects, so only the index name is needed.
            MigrationOp::DropIndex { table: _, name } => {
                out.statements
                    .push(render_drop_index_sql(name, IndexMode::Plain));
            }
            // ADR-0044: an invalid index is present (so `IF NOT EXISTS` would
            // skip it) but never used. Drop it by name — it is known to exist —
            // then run the exact create statement the `AddIndex` path renders.
            MigrationOp::RebuildIndex {
                table,
                name,
                columns,
                unique,
            } => {
                out.statements
                    .push(format!("DROP INDEX {}", quote_ident(name)));
                out.statements.push(render_index_sql(
                    table,
                    name,
                    columns,
                    *unique,
                    dialect,
                    IndexMode::Plain,
                ));
            }
            MigrationOp::ValidateConstraint { table, name } => match dialect {
                Dialect::Postgres => out.statements.push(render_validate_constraint(table, name)),
                Dialect::Sqlite => {
                    return Err(EmissionError {
                        message: format!(
                            "Validate operation for constraint '{name}' on table '{table}' \
                             cannot run on SQLite, which has no unvalidated constraints; \
                             the planner must never plan it there"
                        ),
                    });
                }
            },
            MigrationOp::AddForeignKey { table, column } => {
                let model = find_model(&new_models, table)?;
                let fk = find_foreign_key(model, table, column)?;
                match dialect {
                    Dialect::Postgres => {
                        out.statements
                            .push(render_add_fk_sql(table, fk, constraints))
                    }
                    Dialect::Sqlite => out.reports.push(sqlite_in_place_report(
                        InPlaceChange::AddForeignKey,
                        Subject::column(table, column),
                        format!(
                            "Declared FOREIGN KEY on '{}.{}' (on_delete {}) has no live \
                             constraint, and SQLite cannot add table constraints to an \
                             existing table. Referential integrity for this column is not \
                             database-enforced; generate a reviewed migration with \
                             `ferro migrate new` to rebuild the table with the constraint.",
                            table,
                            column,
                            fk_action_sql(fk_action_from_str(fk.on_delete.as_deref())),
                        ),
                    )),
                }
            }
            MigrationOp::RebuildForeignKey {
                table,
                column,
                old_name,
            } => {
                let model = find_model(&new_models, table)?;
                let fk = find_foreign_key(model, table, column)?;
                match dialect {
                    Dialect::Postgres => {
                        out.statements.push(render_drop_constraint(table, old_name));
                        out.statements
                            .push(render_add_fk_sql(table, fk, constraints));
                    }
                    Dialect::Sqlite => {
                        let live_action = find_model(&old_models, table)
                            .ok()
                            .and_then(|old| {
                                old.foreign_keys.iter().find(|live| live.column == *column)
                            })
                            .map(|live| {
                                fk_action_sql(fk_action_from_str(live.on_delete.as_deref()))
                            })
                            .unwrap_or("<unknown>");
                        out.reports.push(sqlite_in_place_report(
                            InPlaceChange::RebuildForeignKey,
                            Subject::column(table, column),
                            format!(
                                "Foreign key on '{}.{}' declares on_delete {} but the live \
                                 constraint enforces {}; SQLite cannot alter constraints in \
                                 place, so the live behavior remains. Generate a reviewed \
                                 migration with `ferro migrate new` to apply the declared \
                                 action.",
                                table,
                                column,
                                fk_action_sql(fk_action_from_str(fk.on_delete.as_deref())),
                                live_action,
                            ),
                        ));
                    }
                }
            }
            MigrationOp::AddCheck { table, name } => {
                let model = find_model(&new_models, table)?;
                let emission = render_check_addition(table, model, name, dialect, constraints)
                    .ok_or_else(|| EmissionError {
                        message: format!(
                            "Check-addition operation for '{}' on table '{}' has no matching \
                             CHECK constraint in the declared IR",
                            name, table
                        ),
                    })?;
                out.statements.extend(emission.statement);
                out.reports.extend(emission.warning);
            }
            MigrationOp::RebuildCheck { table, name } => {
                let model = find_model(&new_models, table)?;
                let emission = render_check_rebuild(table, model, name, dialect, constraints)
                    .ok_or_else(|| EmissionError {
                        message: format!(
                            "Check-rebuild operation for '{}' on table '{}' has no matching \
                             CHECK constraint in the declared IR",
                            name, table
                        ),
                    })?;
                out.statements.extend(emission.statements);
                out.reports.extend(emission.warning);
            }
            MigrationOp::DropCheck { table, name } => {
                let model = find_model(&new_models, table)?;
                let still_declared = model.table_checks.iter().any(|check| check.name == *name)
                    || model.checks.iter().any(|check| check.name == *name);
                if still_declared {
                    return Err(EmissionError {
                        message: format!(
                            "Check-drop operation for '{name}' on table '{table}' is still \
                             declared in the model IR"
                        ),
                    });
                }
                let emission = render_check_drop(table, name, dialect);
                out.statements.extend(emission.statement);
                out.reports.extend(emission.warning);
            }
            MigrationOp::AddRowPolicy { table, name } => {
                require_postgres(op, dialect)?;
                let model = find_model(&new_models, table)?;
                let policy = model
                    .row_security
                    .as_ref()
                    .and_then(|declaration| declaration.policies.iter().find(|p| p.name == *name))
                    .ok_or_else(|| undeclared_policy(op, table, name))?;
                out.statements.push(
                    render_create_row_policy(model, policy)
                        .map_err(|message| EmissionError { message })?,
                );
            }
            MigrationOp::RebuildRowPolicy { table, name } => {
                require_postgres(op, dialect)?;
                let model = find_model(&new_models, table)?;
                let statements = row_policy_rebuild_statements(model, name)
                    .map_err(|message| EmissionError { message })?
                    .ok_or_else(|| undeclared_policy(op, table, name))?;
                out.statements.extend(statements);
            }
            MigrationOp::DropRowPolicy { table, name } => {
                require_postgres(op, dialect)?;
                let model = find_model(&new_models, table)?;
                let still_declared = model.row_security.as_ref().is_some_and(|declaration| {
                    declaration
                        .policies
                        .iter()
                        .any(|policy| policy.name == *name)
                });
                if still_declared {
                    return Err(EmissionError {
                        message: format!(
                            "Row-policy drop for '{name}' on table '{table}' is still declared \
                             in the model IR"
                        ),
                    });
                }
                out.statements.push(render_drop_row_policy(table, name));
            }
            MigrationOp::EnableRowSecurity { table } => {
                require_postgres(op, dialect)?;
                out.statements.push(render_enable_row_security(table));
            }
            MigrationOp::ForceRowSecurity { table } => {
                require_postgres(op, dialect)?;
                out.statements.push(render_force_row_security(table));
            }
            MigrationOp::DisableRowSecurity { table } => {
                require_postgres(op, dialect)?;
                out.statements.push(render_disable_row_security(table));
            }
            MigrationOp::NoForceRowSecurity { table } => {
                require_postgres(op, dialect)?;
                out.statements.push(render_no_force_row_security(table));
            }
        }
        rendered.push(out);
    }
    Ok(rendered)
}

/// The type guards `model`'s create emission carries for types this plan's
/// `CreateEnumType` ops create — rendered by the same function, so the
/// create-table emission's copy can be left out byte for byte.
fn created_type_guards(
    model: &SchemaModel,
    types_created_by_plan: &BTreeSet<String>,
    dialect: Dialect,
) -> HashSet<String> {
    model
        .columns
        .iter()
        .filter_map(|col| match resolve_column_storage(col, dialect) {
            Ok(ResolvedStorage::PgEnum { type_name, labels })
                if types_created_by_plan.contains(&type_name) =>
            {
                Some(render_pg_enum_create_type(&type_name, &labels))
            }
            _ => None,
        })
        .collect()
}

/// Enum-type and row-security ops exist only on Postgres (SQLite stores enums
/// as text and has no row-level security, ADR-0014); the planner never plans
/// one for SQLite, so meeting one here is a loud error.
fn require_postgres(op: &MigrationOp, dialect: Dialect) -> Result<(), EmissionError> {
    if dialect == Dialect::Postgres {
        return Ok(());
    }
    Err(EmissionError {
        message: format!(
            "{op:?} is a Postgres-only operation and cannot render for SQLite; the planner \
             must never plan it there"
        ),
    })
}

fn undeclared_policy(op: &MigrationOp, table: &str, name: &str) -> EmissionError {
    EmissionError {
        message: format!(
            "{op:?}: row policy '{name}' on table '{table}' is not declared in the model IR"
        ),
    }
}
