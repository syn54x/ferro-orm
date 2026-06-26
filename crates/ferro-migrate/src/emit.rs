//! Executable SQL emission from IR-backed migration plans.

use crate::{BackendDialect, EmissionError, EmissionResult, MigrationOp, MigrationPlan};
use ferro_ddl_lowering::{
    self, apply_canonical_type, canonical_from_schema_column, db_check_constraint_name,
    fk_action_from_str, fk_action_sql, literal_default_value, pg_alter_type_target, quote_ident,
    single_index_name, single_unique_index_name, sqlite_declared_type, sqlite_type_storage_drift,
    Dialect,
};
use ferro_schema_ir::{IrEnvelope, SchemaColumn, SchemaIrPayload, SchemaModel};
use sea_query::{
    Alias, ColumnDef, Index, PostgresQueryBuilder, SqliteQueryBuilder, Table,
};
use std::collections::BTreeMap;

fn lowering_dialect(dialect: BackendDialect) -> Dialect {
    match dialect {
        BackendDialect::Sqlite => Dialect::Sqlite,
        BackendDialect::Postgres => Dialect::Postgres,
    }
}

fn index_models<'a>(models: &'a [SchemaModel]) -> BTreeMap<String, &'a SchemaModel> {
    let mut indexed = BTreeMap::new();
    for model in models {
        indexed.insert(model.table_name.clone(), model);
    }
    indexed
}

fn find_model<'a>(
    models: &'a BTreeMap<String, &'a SchemaModel>,
    table: &str,
) -> Result<&'a SchemaModel, EmissionError> {
    models.get(table).copied().ok_or_else(|| EmissionError {
        message: format!("model '{}' not found in IR context", table),
    })
}

fn find_column<'a>(
    model: &'a SchemaModel,
    column: &str,
) -> Result<&'a SchemaColumn, EmissionError> {
    model
        .columns
        .iter()
        .find(|c| c.name == column)
        .ok_or_else(|| EmissionError {
            message: format!("column '{}.{}' not found in IR context", model.table_name, column),
        })
}

fn render_create_table(model: &SchemaModel, dialect: BackendDialect) -> Result<String, EmissionError> {
    let ld = lowering_dialect(dialect);
    let table_lower = model.table_name.as_str();
    let mut table_stmt = Table::create()
        .table(Alias::new(table_lower))
        .if_not_exists()
        .to_owned();

    for col in &model.columns {
        let canonical = canonical_from_schema_column(col, ld).map_err(|message| EmissionError {
            message,
        })?;
        let mut col_def = ColumnDef::new(Alias::new(&col.name));
        apply_canonical_type(&mut col_def, canonical);
        if col.primary_key {
            col_def.primary_key();
            if col.autoincrement {
                col_def.auto_increment();
            }
        }
        if !col.nullable {
            col_def.not_null();
        }
        if col.unique {
            col_def.unique_key();
        }
        table_stmt.col(&mut col_def);
    }

    let sql = match dialect {
        BackendDialect::Sqlite => table_stmt.build(SqliteQueryBuilder),
        BackendDialect::Postgres => table_stmt.build(PostgresQueryBuilder),
    };
    Ok(sql)
}

fn render_index_sql(
    table_lower: &str,
    name: &str,
    columns: &[String],
    unique: bool,
    dialect: BackendDialect,
) -> String {
    let mut stmt = Index::create()
        .name(name)
        .table(Alias::new(table_lower))
        .if_not_exists()
        .to_owned();
    if unique {
        stmt.unique();
    }
    for col in columns {
        stmt.col(Alias::new(col));
    }
    match dialect {
        BackendDialect::Sqlite => stmt.to_string(SqliteQueryBuilder),
        BackendDialect::Postgres => stmt.to_string(PostgresQueryBuilder),
    }
}

fn single_column_unique_is_inline(model: &SchemaModel, unique_columns: &[String]) -> bool {
    if unique_columns.len() != 1 {
        return false;
    }
    let col_name = &unique_columns[0];
    model
        .columns
        .iter()
        .any(|col| col.name == *col_name && col.unique)
}

fn post_create_artifacts(
    model: &SchemaModel,
    dialect: BackendDialect,
) -> Result<(Vec<String>, Vec<String>), EmissionError> {
    let table_lower = model.table_name.as_str();
    let mut statements = Vec::new();
    let mut warnings = Vec::new();

    for index in &model.indexes {
        statements.push(render_index_sql(
            table_lower,
            &index.name,
            &index.columns,
            index.unique,
            dialect,
        ));
    }

    for unique in &model.uniques {
        if single_column_unique_is_inline(model, &unique.columns) {
            continue;
        }
        statements.push(render_index_sql(
            table_lower,
            &unique.name,
            &unique.columns,
            true,
            dialect,
        ));
    }

    for check in &model.checks {
        match dialect {
            BackendDialect::Postgres => {
                statements.push(format!(
                    "ALTER TABLE \"{table}\" ADD CONSTRAINT \"{name}\" CHECK ({expression})",
                    table = table_lower,
                    name = check.name,
                    expression = check.expression,
                ));
            }
            BackendDialect::Sqlite => {
                warnings.push(format!(
                    "Check constraint '{}' on table '{}' is not emitted on SQLite (requires table rebuild).",
                    check.name, table_lower
                ));
            }
        }
    }

    Ok((statements, warnings))
}

fn foreign_key_statements(
    model: &SchemaModel,
    dialect: BackendDialect,
) -> Vec<String> {
    let table_lower = model.table_name.as_str();
    let mut statements = Vec::new();
    for fk in &model.foreign_keys {
        if dialect == BackendDialect::Postgres {
            let on_delete = fk_action_from_str(fk.on_delete.as_deref());
            statements.push(format!(
                "ALTER TABLE {} ADD FOREIGN KEY ({}) REFERENCES {} ({}) ON DELETE {}",
                quote_ident(table_lower),
                quote_ident(&fk.column),
                quote_ident(&fk.to_table),
                quote_ident(&fk.to_column),
                fk_action_sql(on_delete),
            ));
        }
    }
    statements
}

fn order_add_table_models<'a>(
    models: Vec<&'a SchemaModel>,
) -> Vec<&'a SchemaModel> {
    let mut remaining: Vec<&SchemaModel> = models;
    let mut ordered = Vec::new();
    let mut created = std::collections::HashSet::new();

    while !remaining.is_empty() {
        let available: std::collections::HashSet<String> = remaining
            .iter()
            .map(|m| m.table_name.clone())
            .collect();
        let mut progress = false;
        let mut index = 0;
        while index < remaining.len() {
            let deps: Vec<String> = remaining[index]
                .foreign_keys
                .iter()
                .map(|fk| fk.to_table.clone())
                .collect();
            if deps
                .iter()
                .all(|dep| created.contains(dep) || !available.contains(dep))
            {
                let model = remaining.remove(index);
                created.insert(model.table_name.clone());
                ordered.push(model);
                progress = true;
            } else {
                index += 1;
            }
        }
        if !progress {
            ordered.append(&mut remaining);
            break;
        }
    }
    ordered
}

fn emit_add_table_passes(
    add_models: Vec<&SchemaModel>,
    dialect: BackendDialect,
    result: &mut EmissionResult,
) -> Result<(), EmissionError> {
    let ordered = order_add_table_models(add_models);
    for model in &ordered {
        result.statements.push(render_create_table(model, dialect)?);
        let (artifacts, warnings) = post_create_artifacts(model, dialect)?;
        result.statements.extend(artifacts);
        result.warnings.extend(warnings);
    }
    for model in &ordered {
        if dialect == BackendDialect::Sqlite {
            for fk in &model.foreign_keys {
                result.warnings.push(format!(
                    "Foreign key '{}.{}' -> '{}' is not emitted on SQLite (requires table rebuild).",
                    model.table_name, fk.column, fk.to_table
                ));
            }
        } else {
            result
                .statements
                .extend(foreign_key_statements(model, dialect));
        }
    }
    Ok(())
}

fn emit_add_column(
    table: &str,
    column: &str,
    model: &SchemaModel,
    dialect: BackendDialect,
) -> Result<EmissionResult, EmissionError> {
    let col = find_column(model, column)?;
    let ld = lowering_dialect(dialect);
    let canonical = canonical_from_schema_column(col, ld).map_err(|message| EmissionError {
        message,
    })?;

    if col.primary_key {
        return Err(EmissionError {
            message: format!(
                "Cannot add column '{}.{}': it is a primary key, and primary keys cannot \
                 be added to existing tables. Use Alembic for this migration.",
                table, column
            ),
        });
    }

    let backfill_default = if col.nullable {
        None
    } else {
        match col.default.as_ref().and_then(literal_default_value) {
            Some(value) => Some(value),
            None => {
                return Err(EmissionError {
                    message: format!(
                        "Cannot add NOT NULL column '{}.{}' to an existing table: it has no \
                         literal default to backfill existing rows. Make the field nullable, \
                         give it a literal default, or use Alembic for this migration.",
                        table, column
                    ),
                });
            }
        }
    };

    let inline_unique = col.unique && dialect == BackendDialect::Postgres;
    let mut col_def = ColumnDef::new(Alias::new(column));
    apply_canonical_type(&mut col_def, canonical);
    if !col.nullable {
        col_def.not_null();
    }
    if inline_unique {
        col_def.unique_key();
    }
    if let Some(default_value) = &backfill_default {
        col_def.default(default_value.clone());
    }

    let stmt = Table::alter()
        .table(Alias::new(table))
        .add_column(&mut col_def)
        .to_owned();

    let mut result = EmissionResult::default();
    result.statements.push(match dialect {
        BackendDialect::Sqlite => stmt.to_string(SqliteQueryBuilder),
        BackendDialect::Postgres => stmt.to_string(PostgresQueryBuilder),
    });

    if backfill_default.is_some() && dialect == BackendDialect::Postgres {
        result.statements.push(format!(
            "ALTER TABLE {} ALTER COLUMN {} DROP DEFAULT",
            quote_ident(table),
            quote_ident(column)
        ));
    }

    if col.unique && dialect == BackendDialect::Sqlite {
        let index_name = single_unique_index_name(table, column);
        let index_stmt = Index::create()
            .unique()
            .name(&index_name)
            .table(Alias::new(table))
            .col(Alias::new(column))
            .if_not_exists()
            .to_owned();
        result
            .statements
            .push(index_stmt.to_string(SqliteQueryBuilder));
        result.warnings.push(format!(
            "Added unique column '{}.{}' as a unique index '{}' (SQLite cannot add an \
             inline UNIQUE constraint to an existing table).",
            table, column, index_name
        ));
    }

    if col.index {
        result.statements.push(render_index_sql(
            table,
            &single_index_name(table, column),
            &[column.to_string()],
            false,
            dialect,
        ));
    }

    for check in &model.checks {
        if check.name == db_check_constraint_name(table, column) {
            if dialect == BackendDialect::Postgres {
                result.statements.push(format!(
                    "ALTER TABLE \"{table}\" ADD CONSTRAINT \"{name}\" CHECK ({expression})",
                    name = check.name,
                    expression = check.expression,
                ));
            }
        }
    }

    if let Some(fk) = model.foreign_keys.iter().find(|fk| fk.column == column) {
        if dialect == BackendDialect::Postgres {
            let on_delete = fk_action_from_str(fk.on_delete.as_deref());
            result.statements.push(format!(
                "ALTER TABLE {} ADD FOREIGN KEY ({}) REFERENCES {} ({}) ON DELETE {}",
                quote_ident(table),
                quote_ident(column),
                quote_ident(&fk.to_table),
                quote_ident(&fk.to_column),
                fk_action_sql(on_delete),
            ));
        } else {
            result.warnings.push(format!(
                "Added foreign-key column '{}.{}' without its FOREIGN KEY constraint \
                 (SQLite cannot add table constraints to an existing table). Referential \
                 integrity for this column is not database-enforced; use Alembic if you \
                 need the constraint.",
                table, column
            ));
        }
    }

    Ok(result)
}

fn emit_alter_column_type(
    table: &str,
    column: &str,
    old_col: &SchemaColumn,
    new_col: &SchemaColumn,
    dialect: BackendDialect,
) -> Result<EmissionResult, EmissionError> {
    let mut result = EmissionResult::default();
    let ld = lowering_dialect(dialect);

    match dialect {
        BackendDialect::Postgres => {
            if old_col.postgres_native_enum {
                return Ok(result);
            }
            if new_col.enum_type_name.is_some() {
                result.warnings.push(format!(
                    "Column '{}.{}' uses a native Postgres enum type; type reconciliation \
                     is deferred to Alembic.",
                    table, column
                ));
                return Ok(result);
            }
            if old_col.primary_key || new_col.primary_key {
                return Ok(result);
            }
            let new_canonical = canonical_from_schema_column(new_col, ld).map_err(|message| {
                EmissionError {
                    message: format!(
                        "Cannot alter type for '{}.{}': {}",
                        table, column, message
                    ),
                }
            })?;
            let target = pg_alter_type_target(new_canonical);
            result.statements.push(format!(
                "ALTER TABLE {table} ALTER COLUMN {col} TYPE {target} USING {col}::{target}",
                table = quote_ident(table),
                col = quote_ident(column),
                target = target,
            ));
        }
        BackendDialect::Sqlite => {
            if old_col.primary_key || new_col.primary_key {
                return Ok(result);
            }
            let new_canonical = canonical_from_schema_column(new_col, ld).map_err(|message| {
                EmissionError {
                    message: format!(
                        "Cannot alter type for '{}.{}': {}",
                        table, column, message
                    ),
                }
            })?;
            if sqlite_type_storage_drift(old_col.db_type.as_deref().unwrap_or(""), new_canonical) {
                result.warnings.push(format!(
                    "Column '{}.{}' is declared '{}' in the database but the model expects \
                     '{}'. SQLite cannot change column types in place; use Alembic to \
                     migrate this column.",
                    table,
                    column,
                    old_col.db_type.as_deref().unwrap_or(""),
                    sqlite_declared_type(new_canonical),
                ));
            }
        }
    }
    Ok(result)
}

fn emit_alter_column_nullability(
    table: &str,
    column: &str,
    old_col: &SchemaColumn,
    new_col: &SchemaColumn,
    dialect: BackendDialect,
) -> EmissionResult {
    let mut result = EmissionResult::default();
    if old_col.primary_key || new_col.primary_key {
        return result;
    }

    match dialect {
        BackendDialect::Postgres => {
            if !new_col.nullable && old_col.nullable {
                result.statements.push(format!(
                    "ALTER TABLE {} ALTER COLUMN {} SET NOT NULL",
                    quote_ident(table),
                    quote_ident(column),
                ));
            } else if new_col.nullable && !old_col.nullable {
                result.statements.push(format!(
                    "ALTER TABLE {} ALTER COLUMN {} DROP NOT NULL",
                    quote_ident(table),
                    quote_ident(column),
                ));
            }
        }
        BackendDialect::Sqlite => {
            if old_col.nullable != new_col.nullable {
                result.warnings.push(format!(
                    "Column '{}.{}' is {} in the database but the model expects {}. SQLite \
                     cannot change column nullability in place; use Alembic to migrate \
                     this column.",
                    table,
                    column,
                    if old_col.nullable {
                        "nullable"
                    } else {
                        "NOT NULL"
                    },
                    if new_col.nullable {
                        "nullable"
                    } else {
                        "NOT NULL"
                    },
                ));
            }
        }
    }
    result
}

/// Render executable SQL for each operation in `plan` using IR metadata.
pub fn emit_sql_with_ir(
    plan: &MigrationPlan,
    old_ir: &IrEnvelope<SchemaIrPayload>,
    new_ir: &IrEnvelope<SchemaIrPayload>,
    dialect: BackendDialect,
) -> Result<EmissionResult, EmissionError> {
    let old_models = index_models(&old_ir.payload.models);
    let new_models = index_models(&new_ir.payload.models);

    let mut result = EmissionResult {
        statements: Vec::new(),
        warnings: plan.warnings.clone(),
    };

    let mut add_table_models = Vec::new();
    for operation in &plan.operations {
        if let MigrationOp::AddTable { table } = operation {
            add_table_models.push(find_model(&new_models, table)?);
        }
    }

    if !add_table_models.is_empty() {
        emit_add_table_passes(add_table_models, dialect, &mut result)?;
    }

    for operation in &plan.operations {
        match operation {
            MigrationOp::AddTable { .. } => {}
            MigrationOp::DropTable { table } => {
                result
                    .statements
                    .push(format!("DROP TABLE \"{}\"", table));
            }
            MigrationOp::AddColumn { table, column } => {
                let model = find_model(&new_models, table)?;
                let partial = emit_add_column(table, column, model, dialect)?;
                result.statements.extend(partial.statements);
                result.warnings.extend(partial.warnings);
            }
            MigrationOp::DropColumn { table, column } => {
                let old_model = find_model(&old_models, table)?;
                let old_col = find_column(old_model, column)?;
                if old_col.primary_key {
                    return Err(EmissionError {
                        message: format!(
                            "Cannot drop column '{}.{}': it is part of the primary key. \
                             Primary-key changes must be migrated with Alembic.",
                            table, column
                        ),
                    });
                }
                result.statements.push(format!(
                    "ALTER TABLE \"{}\" DROP COLUMN \"{}\"",
                    table, column
                ));
            }
            MigrationOp::AlterColumnType { table, column } => {
                let old_model = find_model(&old_models, table)?;
                let new_model = find_model(&new_models, table)?;
                let old_col = find_column(old_model, column)?;
                let new_col = find_column(new_model, column)?;
                let partial = emit_alter_column_type(table, column, old_col, new_col, dialect)?;
                result.statements.extend(partial.statements);
                result.warnings.extend(partial.warnings);
            }
            MigrationOp::AlterColumnNullability { table, column } => {
                let old_model = find_model(&old_models, table)?;
                let new_model = find_model(&new_models, table)?;
                let old_col = find_column(old_model, column)?;
                let new_col = find_column(new_model, column)?;
                let partial =
                    emit_alter_column_nullability(table, column, old_col, new_col, dialect);
                result.statements.extend(partial.statements);
                result.warnings.extend(partial.warnings);
            }
        }
    }

    Ok(result)
}

#[cfg(test)]
pub(crate) use ferro_ddl_lowering::{
    composite_index_name as test_composite_index_name,
    composite_unique_index_name as test_composite_unique_index_name,
    db_check_constraint_name as test_db_check_constraint_name,
    single_index_name as test_single_index_name,
};
