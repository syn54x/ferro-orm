//! Executable SQL emission from IR-backed migration plans.

use crate::{Dialect, EmissionError, EmissionResult, MigrationOp, MigrationPlan};
use ferro_ddl_lowering::{
    self, ResolvedStorage, apply_canonical_type_for, canonical_from_schema_column,
    canonical_to_db_type_token, db_check_constraint_name, fk_action_from_str, fk_action_sql,
    fk_name, literal_default_value, pg_alter_type_target, quote_ident, refused_conversion,
    refused_conversion_warning, render_db_check, render_pg_enum_create_type,
    resolve_column_storage, single_index_name, single_unique_index_name, sqlite_declared_type,
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
    /// Statements that must run BEFORE `create_sql`: the idempotent
    /// `CREATE TYPE ... AS ENUM` guards for native Postgres enum columns
    /// (FF-B B2). Empty on SQLite.
    pub pre_create_sqls: Vec<String>,
    /// `CREATE TABLE` including inline NAMED FKs (`CONSTRAINT "fk_..."`).
    /// Single-column uniques are NOT inline — they are named `uq_` unique
    /// indexes in [`post_create_sqls`](Self::post_create_sqls) (FF-B B4/D1).
    pub create_sql: String,
    /// Standalone `CREATE [UNIQUE] INDEX` statements plus the Postgres `db_check`
    /// `ALTER`. Never contains foreign keys (those are inline in `create_sql`).
    pub post_create_sqls: Vec<String>,
    /// Non-fatal warnings (e.g. the SQLite `db_check` elision).
    pub warnings: Vec<String>,
}

/// Apply a resolved storage to a sea-query [`ColumnDef`]. Enum columns render
/// as the bare (unquoted) type name, byte-matching SQLAlchemy's spelling.
fn apply_resolved_storage(col_def: &mut ColumnDef, storage: &ResolvedStorage, dialect: Dialect) {
    match storage {
        ResolvedStorage::Scalar(canonical) => {
            apply_canonical_type_for(col_def, *canonical, dialect)
        }
        ResolvedStorage::PgEnum { type_name, .. } => {
            col_def.custom(Alias::new(type_name));
        }
    }
}

/// The FK constraint name for one IR foreign key: the compiler-provided
/// `SchemaForeignKey.name` when set, else the shared `fk_name` convention.
fn fk_constraint_name(table_lower: &str, fk: &ferro_schema_ir::SchemaForeignKey) -> String {
    fk.name
        .clone()
        .unwrap_or_else(|| fk_name(table_lower, &fk.column, &fk.to_table))
}

/// sea-query's SQLite builder drops the constraint name of an inline FK in
/// CREATE TABLE mode (its Postgres builder honors it). Insert the
/// `CONSTRAINT "fk_..." ` prefix deterministically: we control both the
/// anchor bytes (rendered by the same builder) and the emission order, and
/// each FK's anchor is unique within the statement (one FK per column).
/// Pinned by the create-table goldens.
fn name_sqlite_inline_fks(create_sql: String, model: &SchemaModel) -> String {
    let mut sql = create_sql;
    for fk in &model.foreign_keys {
        let anchor = format!(
            "FOREIGN KEY ({}) REFERENCES {}",
            quote_ident(&fk.column),
            quote_ident(&fk.to_table)
        );
        let named = format!(
            "CONSTRAINT {} {}",
            quote_ident(&fk_constraint_name(&model.table_name, fk)),
            anchor
        );
        sql = sql.replacen(&anchor, &named, 1);
    }
    sql
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

    let mut pre_create_sqls: Vec<String> = Vec::new();
    for col in &model.columns {
        let storage =
            resolve_column_storage(col, ld).map_err(|message| EmissionError { message })?;
        if let ResolvedStorage::PgEnum { type_name, labels } = &storage {
            let guard = render_pg_enum_create_type(type_name, labels);
            if !pre_create_sqls.contains(&guard) {
                pre_create_sqls.push(guard);
            }
        }
        let mut col_def = ColumnDef::new(Alias::new(&col.name));
        apply_resolved_storage(&mut col_def, &storage, ld);
        if col.primary_key {
            col_def.primary_key();
            if col.autoincrement {
                col_def.auto_increment();
            }
        }
        // PK columns get an explicit NOT NULL (the compiler clamps their IR
        // nullability to false): Postgres implies it anyway, but SQLite's
        // PRAGMA reports an INTEGER PRIMARY KEY as nullable without the
        // keyword, which reads back as a phantom nullability diff (FF-B B5).
        if !col.nullable {
            col_def.not_null();
        }
        // Single-column uniques are NOT inline: they are emitted as standalone
        // named `uq_` unique indexes (see `standalone_indexes`), the one shape
        // fresh-create, the SQLite ALTER path, and Alembic reflection all share.
        table_stmt.col(&mut col_def);
    }

    // Inline, NAMED foreign keys. The runtime defaults a missing `on_delete`
    // to CASCADE (`fk_action_from_str(None) == Cascade`), preserved here.
    for fk in &model.foreign_keys {
        let action = fk_action_from_str(fk.on_delete.as_deref());
        table_stmt.foreign_key(
            ForeignKey::create()
                .name(&fk_constraint_name(table_lower, fk))
                .from(Alias::new(table_lower), Alias::new(&fk.column))
                .to(Alias::new(&fk.to_table), Alias::new(&fk.to_column))
                .on_delete(action),
        );
    }

    let create_sql = match dialect {
        Dialect::Sqlite => name_sqlite_inline_fks(table_stmt.build(SqliteQueryBuilder), model),
        Dialect::Postgres => table_stmt.build(PostgresQueryBuilder),
    };

    let (post_create_sqls, warnings) = post_create_artifacts(model, dialect)?;
    Ok(CreateTableEmission {
        pre_create_sqls,
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

/// The standalone indexes/uniques the create path emits as separate
/// `CREATE [UNIQUE] INDEX` statements: every `model.indexes` and every
/// `model.uniques` (single-column uniques included — nothing is inline).
/// Returned as (name, columns, unique).
pub(crate) fn standalone_indexes(model: &SchemaModel) -> Vec<(String, Vec<String>, bool)> {
    let mut out = Vec::new();
    for index in &model.indexes {
        out.push((index.name.clone(), index.columns.clone(), index.unique));
    }
    for unique in &model.uniques {
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
/// Delegates to [`crate::order_by_dependencies`] for the dependency
/// semantics: a self-referential FK and a FK to a table outside this add set
/// do not constrain ordering, and genuine cross-table cycles fall through in
/// input order (SQLite tolerates the forward references; Postgres rejects
/// them — #302 follow-up).
pub fn order_models_for_create<'a>(models: &[&'a SchemaModel]) -> Vec<&'a SchemaModel> {
    crate::order_by_dependencies(
        models.to_vec(),
        |model| model.table_name.clone(),
        |model| {
            model
                .foreign_keys
                .iter()
                .map(|fk| fk.to_table.clone())
                .collect()
        },
    )
}

fn emit_add_table_passes(
    add_models: Vec<&SchemaModel>,
    dialect: Dialect,
    result: &mut EmissionResult,
) -> Result<(), EmissionError> {
    let ordered = order_models_for_create(&add_models);
    // Enum types can be shared across models in one add set; emit each
    // idempotent CREATE TYPE guard once.
    let mut emitted_type_guards: std::collections::HashSet<String> =
        std::collections::HashSet::new();
    for model in &ordered {
        let emission = render_create_table(model, dialect)?;
        for guard in emission.pre_create_sqls {
            if emitted_type_guards.insert(guard.clone()) {
                result.statements.push(guard);
            }
        }
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
    let storage = resolve_column_storage(col, ld).map_err(|message| EmissionError {
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

    let mut col_def = ColumnDef::new(Alias::new(column));
    apply_resolved_storage(&mut col_def, &storage, ld);
    if !col.nullable {
        col_def.not_null();
    }
    if let Some(default_value) = &backfill_default {
        col_def.default(default_value.clone());
    }

    let stmt = Table::alter()
        .table(Alias::new(table))
        .add_column(&mut col_def)
        .to_owned();

    let mut result = EmissionResult::default();
    // A native-enum column needs its type to exist first (idempotent guard).
    if let ResolvedStorage::PgEnum { type_name, labels } = &storage {
        result
            .statements
            .push(render_pg_enum_create_type(type_name, labels));
    }
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

    // The canonical single-column unique shape on both dialects: the same
    // standalone named `uq_` unique index fresh-create emits (FF-B B4/D1).
    if col.unique {
        result.statements.push(render_index_sql(
            table,
            &single_unique_index_name(table, column),
            &[column.to_string()],
            true,
            dialect,
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
            result.statements.push(render_add_fk_sql(table, fk));
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

/// `ALTER TABLE ... ADD CONSTRAINT ... FOREIGN KEY ... ON DELETE ...` for one
/// IR foreign key. Postgres-only — SQLite cannot add table constraints to an
/// existing table; its callers warn instead.
fn render_add_fk_sql(table: &str, fk: &ferro_schema_ir::SchemaForeignKey) -> String {
    format!(
        "ALTER TABLE {} ADD CONSTRAINT {} FOREIGN KEY ({}) REFERENCES {} ({}) ON DELETE {}",
        quote_ident(table),
        quote_ident(&fk_constraint_name(table, fk)),
        quote_ident(&fk.column),
        quote_ident(&fk.to_table),
        quote_ident(&fk.to_column),
        fk_action_sql(fk_action_from_str(fk.on_delete.as_deref())),
    )
}

/// Find the declared FK for `column` on `table` in the new-IR model.
fn find_foreign_key<'a>(
    model: &'a SchemaModel,
    table: &str,
    column: &str,
) -> Result<&'a ferro_schema_ir::SchemaForeignKey, EmissionError> {
    model
        .foreign_keys
        .iter()
        .find(|fk| fk.column == column)
        .ok_or_else(|| EmissionError {
            message: format!(
                "Foreign-key operation for '{}.{}' has no matching FK in the declared IR",
                table, column
            ),
        })
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
            // A live native-enum column is never auto-reconciled, whatever the
            // model says: enum-to-anything casts (and label changes) are
            // reviewed-migration territory. A matching enum model is a no-op;
            // a scalar model against a live enum is left to Alembic.
            if old_col.postgres_native_enum {
                return Ok(result);
            }
            if old_col.primary_key || new_col.primary_key {
                return Ok(result);
            }
            let new_storage =
                resolve_column_storage(new_col, ld).map_err(|message| EmissionError {
                    message: format!("Cannot alter type for '{}.{}': {}", table, column, message),
                })?;
            // Refusal rails (#154 generalized): a conversion that could
            // reinterpret or destroy stored values warns and skips — never a
            // silent ALTER (FF-B B1/B2).
            if let Some(kind) = refused_conversion(old_col, &new_storage, ld) {
                let old_db_type = old_col.db_type.clone().unwrap_or_default();
                let (new_target, keep_db_type) = match &new_storage {
                    ResolvedStorage::PgEnum { type_name, .. } => {
                        (type_name.clone(), old_db_type.clone())
                    }
                    ResolvedStorage::Scalar(new_c) => {
                        let old_canonical =
                            canonical_from_schema_column(old_col, ld).map_err(|message| {
                                EmissionError {
                                    message: format!(
                                        "Cannot alter type for '{}.{}': {}",
                                        table, column, message
                                    ),
                                }
                            })?;
                        (
                            pg_alter_type_target(*new_c),
                            canonical_to_db_type_token(old_canonical, ld),
                        )
                    }
                };
                result.warnings.push(refused_conversion_warning(
                    kind,
                    table,
                    column,
                    &old_db_type,
                    &new_target,
                    &keep_db_type,
                ));
                return Ok(result);
            }
            let new_canonical = match new_storage {
                ResolvedStorage::Scalar(canonical) => canonical,
                // Live column already IS the native enum (otherwise the rail
                // above refused): nothing to alter. Label additions/renames
                // are reviewed-migration territory.
                ResolvedStorage::PgEnum { .. } => return Ok(result),
            };
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
            MigrationOp::AddForeignKey { table, column } => {
                let model = find_model(&new_models, table)?;
                let fk = find_foreign_key(model, table, column)?;
                match dialect {
                    Dialect::Postgres => result.statements.push(render_add_fk_sql(table, fk)),
                    Dialect::Sqlite => result.warnings.push(format!(
                        "Declared FOREIGN KEY on '{}.{}' (on_delete {}) has no live \
                         constraint, and SQLite cannot add table constraints to an \
                         existing table. Referential integrity for this column is not \
                         database-enforced; use Alembic if you need the constraint.",
                        table,
                        column,
                        fk_action_sql(fk_action_from_str(fk.on_delete.as_deref())),
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
                        result.statements.push(format!(
                            "ALTER TABLE {} DROP CONSTRAINT {}",
                            quote_ident(table),
                            quote_ident(old_name),
                        ));
                        result.statements.push(render_add_fk_sql(table, fk));
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
                        result.warnings.push(format!(
                            "Foreign key on '{}.{}' declares on_delete {} but the live \
                             constraint enforces {}; SQLite cannot alter constraints in \
                             place, so the live behavior remains. Migrate with Alembic to \
                             apply the declared action.",
                            table,
                            column,
                            fk_action_sql(fk_action_from_str(fk.on_delete.as_deref())),
                            live_action,
                        ));
                    }
                }
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
