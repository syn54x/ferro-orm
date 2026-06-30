//! Executable SQL emission from IR-backed migration plans.

use crate::{Dialect, EmissionError, EmissionResult, MigrationOp, MigrationPlan};
use ferro_ddl_lowering::{
    self, apply_canonical_type, canonical_from_schema_column, db_check_constraint_name,
    fk_action_from_str, fk_action_sql, literal_default_value, pg_alter_type_target, quote_ident,
    render_db_check, single_index_name, single_unique_index_name, sqlite_declared_type,
    sqlite_type_storage_drift,
};
use ferro_schema_ir::{IrEnvelope, SchemaColumn, SchemaIrPayload, SchemaModel};
use sea_query::{
    Alias, ColumnDef, ForeignKey, Index, PostgresQueryBuilder, SqliteQueryBuilder, Table,
};
use std::collections::BTreeMap;

/// A rendered `CREATE TABLE` plus its standalone post-create artifacts.
///
/// This is the single create-table emission shape used by the AddTable path.
/// Foreign keys are folded INLINE into [`create_sql`](Self::create_sql) so the
/// output is byte-identical to the runtime JSON path on both backends.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct CreateTableEmission {
    /// `CREATE TABLE` including inline anonymous FKs and inline single-column `UNIQUE`.
    pub create_sql: String,
    /// Standalone `CREATE [UNIQUE] INDEX` statements plus the Postgres `db_check`
    /// `ALTER`. Never contains foreign keys (those are inline in `create_sql`).
    pub post_create_sqls: Vec<String>,
    /// Non-fatal warnings (e.g. the SQLite `db_check` elision).
    pub warnings: Vec<String>,
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

/// Render the full `CREATE TABLE` emission for one model, folding foreign keys
/// INLINE so the output is byte-identical to the runtime JSON path on both
/// backends. This is the single create-table emitter for the AddTable path.
///
/// # Errors
/// Returns an [`EmissionError`] when a column's storage type cannot be resolved
/// from its IR metadata.
pub fn render_create_table(
    model: &SchemaModel,
    dialect: Dialect,
) -> Result<CreateTableEmission, EmissionError> {
    let ld = dialect;
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

    // Inline, anonymous foreign keys. The runtime defaults a missing `on_delete`
    // to CASCADE (`fk_action_from_str(None) == Cascade`), preserved here.
    for fk in &model.foreign_keys {
        let action = fk_action_from_str(fk.on_delete.as_deref());
        table_stmt.foreign_key(
            ForeignKey::create()
                .from(Alias::new(table_lower), Alias::new(&fk.column))
                .to(Alias::new(&fk.to_table), Alias::new(&fk.to_column))
                .on_delete(action),
        );
    }

    let create_sql = match dialect {
        Dialect::Sqlite => table_stmt.build(SqliteQueryBuilder),
        Dialect::Postgres => table_stmt.build(PostgresQueryBuilder),
    };

    let (post_create_sqls, warnings) = post_create_artifacts(model, dialect)?;
    Ok(CreateTableEmission {
        create_sql,
        post_create_sqls,
        warnings,
    })
}

fn render_index_sql(
    table_lower: &str,
    name: &str,
    columns: &[String],
    unique: bool,
    dialect: Dialect,
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
        Dialect::Sqlite => stmt.to_string(SqliteQueryBuilder),
        Dialect::Postgres => stmt.to_string(PostgresQueryBuilder),
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

/// The standalone indexes/uniques the create path emits as separate
/// `CREATE [UNIQUE] INDEX` statements: every `model.indexes`, plus `model.uniques`
/// that are not inline single-column uniques. Returned as (name, columns, unique).
pub(crate) fn standalone_indexes(model: &SchemaModel) -> Vec<(String, Vec<String>, bool)> {
    let mut out = Vec::new();
    for index in &model.indexes {
        out.push((index.name.clone(), index.columns.clone(), index.unique));
    }
    for unique in &model.uniques {
        if single_column_unique_is_inline(model, &unique.columns) {
            continue;
        }
        out.push((unique.name.clone(), unique.columns.clone(), true));
    }
    out
}

fn post_create_artifacts(
    model: &SchemaModel,
    dialect: Dialect,
) -> Result<(Vec<String>, Vec<String>), EmissionError> {
    let table_lower = model.table_name.as_str();
    let mut statements = Vec::new();
    let mut warnings = Vec::new();

    for (name, columns, unique) in standalone_indexes(model) {
        statements.push(render_index_sql(table_lower, &name, &columns, unique, dialect));
    }

    for check in &model.checks {
        let emission = render_db_check(table_lower, check, dialect);
        if let Some(stmt) = emission.statement {
            statements.push(stmt);
        }
        if let Some(warning) = emission.warning {
            warnings.push(warning);
        }
    }

    Ok((statements, warnings))
}


/// Order `AddTable` models so each table's FK targets are created before it.
///
/// A FK dependency on a table that is *not* part of this add set (already
/// exists in the live DB) does not constrain ordering. Cycles fall through in
/// arbitrary order — the inline FKs are anonymous, so a cyclic create still
/// runs (SQLite is lenient; Postgres `CREATE TABLE` with a forward inline FK
/// reference is the pre-existing behavior preserved here).
pub fn order_models_for_create<'a>(models: &[&'a SchemaModel]) -> Vec<&'a SchemaModel> {
    let mut remaining: Vec<&SchemaModel> = models.to_vec();
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
    dialect: Dialect,
    result: &mut EmissionResult,
) -> Result<(), EmissionError> {
    let ordered = order_models_for_create(&add_models);
    for model in &ordered {
        let emission = render_create_table(model, dialect)?;
        result.statements.push(emission.create_sql);
        result.statements.extend(emission.post_create_sqls);
        result.warnings.extend(emission.warnings);
    }
    Ok(())
}

fn emit_add_column(
    table: &str,
    column: &str,
    model: &SchemaModel,
    dialect: Dialect,
) -> Result<EmissionResult, EmissionError> {
    let col = find_column(model, column)?;
    let ld = dialect;
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

    let inline_unique = col.unique && dialect == Dialect::Postgres;
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
        Dialect::Sqlite => stmt.to_string(SqliteQueryBuilder),
        Dialect::Postgres => stmt.to_string(PostgresQueryBuilder),
    });

    if backfill_default.is_some() && dialect == Dialect::Postgres {
        result.statements.push(format!(
            "ALTER TABLE {} ALTER COLUMN {} DROP DEFAULT",
            quote_ident(table),
            quote_ident(column)
        ));
    }

    if col.unique && dialect == Dialect::Sqlite {
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
            let emission = render_db_check(table, check, dialect);
            if let Some(stmt) = emission.statement {
                result.statements.push(stmt);
            }
            if let Some(warning) = emission.warning {
                result.warnings.push(warning);
            }
        }
    }

    if let Some(fk) = model.foreign_keys.iter().find(|fk| fk.column == column) {
        if dialect == Dialect::Postgres {
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
    dialect: Dialect,
) -> Result<EmissionResult, EmissionError> {
    let mut result = EmissionResult::default();
    let ld = dialect;

    match dialect {
        Dialect::Postgres => {
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
        Dialect::Sqlite => {
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
    dialect: Dialect,
) -> EmissionResult {
    let mut result = EmissionResult::default();
    if old_col.primary_key || new_col.primary_key {
        return result;
    }

    match dialect {
        Dialect::Postgres => {
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
        Dialect::Sqlite => {
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
    dialect: Dialect,
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
            MigrationOp::AddIndex { table, name, columns, unique } => {
                result.statements.push(render_index_sql(table, name, columns, *unique, dialect));
            }
            // `table` is intentionally unused: DROP INDEX is schema-scoped (not
            // table-qualified) on both SQLite and Postgres, so only the index name is needed.
            MigrationOp::DropIndex { table: _, name } => {
                result.statements.push(format!("DROP INDEX IF EXISTS \"{}\"", name));
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
