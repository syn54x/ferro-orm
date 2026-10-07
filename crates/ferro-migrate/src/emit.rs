//! Executable SQL emission from IR-backed migration plans.

use crate::{Dialect, EmissionError, EmissionResult};
use ferro_ddl_lowering::{
    self, CheckEmission, ConstraintMode, IndexMode, ResolvedStorage, apply_canonical_type_for,
    canonical_from_schema_column,
    canonical_to_db_type_token, db_check_constraint_name, fk_action_from_str, fk_action_sql,
    fk_name, literal_default_value, pg_alter_type_target, quote_ident, refused_conversion,
    refused_conversion_warning, render_db_check, render_json_backfill_default,
    render_pg_enum_create_type, render_sqlite_add_column_references, render_table_check_body,
    resolve_column_storage, row_security_statements, single_index_name, single_unique_index_name,
    sqlite_declared_type, sqlite_type_storage_drift,
};
use ferro_schema_ir::{SchemaColumn, SchemaModel};
use sea_query::{
    Alias, ColumnDef, Expr, ForeignKey, Index, PostgresQueryBuilder, QueryBuilder,
    SqliteQueryBuilder, Table,
};
use std::collections::{BTreeMap, BTreeSet};

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
    /// `CREATE TABLE` including inline NAMED FKs (`CONSTRAINT "fk_..."`) and,
    /// on SQLite, each `db_check` as a named column constraint on its column.
    /// Single-column uniques are NOT inline — they are named `uq_` unique
    /// indexes in [`post_create_sqls`](Self::post_create_sqls) (FF-B B4/D1).
    pub create_sql: String,
    /// Standalone `CREATE [UNIQUE] INDEX` statements plus the Postgres `db_check`
    /// `ALTER`. Never contains foreign keys (those are inline in `create_sql`).
    pub post_create_sqls: Vec<String>,
    /// Non-fatal warnings (e.g. the SQLite row-security skip).
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
pub(crate) fn fk_constraint_name(table_lower: &str, fk: &ferro_schema_ir::SchemaForeignKey) -> String {
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

/// Splice the model's NAMED table CHECK clauses into the CREATE TABLE
/// constraint list (ADR-0014: inline on both dialects, never a follow-up
/// ALTER).
///
/// sea-query has no named-CHECK form — `TableCreateStatement::check` renders a
/// bare `CHECK (...)` — so ferro renders the whole clause itself and appends it
/// where sea-query closes the list, the same "we control both the anchor bytes
/// and the emission order" reasoning as [`name_sqlite_inline_fks`]. Splicing
/// before any CHECK text exists keeps the operation independent of the
/// predicate body's contents.
fn append_named_table_checks(
    create_sql: String,
    model: &SchemaModel,
) -> Result<String, EmissionError> {
    if model.table_checks.is_empty() {
        return Ok(create_sql);
    }
    let clauses = model
        .table_checks
        .iter()
        .map(|check| {
            format!(
                "CONSTRAINT {} CHECK ({})",
                quote_ident(&check.name),
                render_table_check_body(check)
            )
        })
        .collect::<Vec<_>>()
        .join(", ");
    // Both builders close the column/constraint list with a literal " )" and
    // emit nothing after it for the table options ferro sets. A sea-query
    // upgrade that changes that is a loud emission failure, never a silently
    // dropped constraint.
    let head = create_sql.strip_suffix(" )").ok_or_else(|| EmissionError {
        message: format!(
            "Cannot inline table CHECK constraints for '{}': the rendered CREATE TABLE does \
             not end in the expected constraint-list close. SQL: {}",
            model.table_name, create_sql
        ),
    })?;
    Ok(format!("{head}, {clauses} )"))
}

pub(crate) fn find_model<'a>(
    models: &'a BTreeMap<String, &'a SchemaModel>,
    table: &str,
) -> Result<&'a SchemaModel, EmissionError> {
    models.get(table).copied().ok_or_else(|| EmissionError {
        message: format!("model '{}' not found in IR context", table),
    })
}

pub(crate) fn find_column<'a>(
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
    render_create_table_as(model, dialect, None)
}

/// [`render_create_table`], with the `CREATE TABLE` statement naming
/// `table_name_override` instead of the model's table when one is given — a
/// SQLite table rebuild creates `_ferro_new_<table>` in the model's shape
/// (ADR-0034). Only the created table's name changes: every constraint and
/// index name, every `REFERENCES` target, and every post-create statement
/// still name the model's table, which the rebuild renames the new table to.
///
/// # Errors
/// As [`render_create_table`].
pub fn render_create_table_as(
    model: &SchemaModel,
    dialect: Dialect,
    table_name_override: Option<&str>,
) -> Result<CreateTableEmission, EmissionError> {
    let ld = dialect;
    let table_lower = model.table_name.as_str();
    let mut table_stmt = Table::create()
        .table(Alias::new(table_name_override.unwrap_or(table_lower)))
        .if_not_exists()
        .to_owned();

    // One db_check emission per column check, decided once: Postgres yields a
    // post-create statement, SQLite an inline fragment for the owning column.
    let check_emissions: Vec<(&ferro_schema_ir::SchemaCheck, CheckEmission)> = model
        .checks
        .iter()
        .map(|check| (check, render_db_check(table_lower, check, dialect, ConstraintMode::Plain)))
        .collect();
    if let Some((orphan, _)) = check_emissions.iter().find(|(check, emission)| {
        emission.inline.is_some()
            && !model
                .columns
                .iter()
                .any(|col| check.name == db_check_constraint_name(table_lower, &col.name))
    }) {
        return Err(EmissionError {
            message: format!(
                "Cannot inline CHECK constraint '{}' on table '{}': no declared column owns it.",
                orphan.name, table_lower
            ),
        });
    }

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
        // A SQLite db_check rides its column's definition as a named column
        // constraint (#514); Postgres has no inline fragment.
        append_inline_checks(&mut col_def, &check_emissions, table_lower, &col.name);
        // Single-column uniques are NOT inline: they are emitted as standalone
        // named `uq_` unique indexes (see `standalone_indexes`), the one shape
        // fresh-create, the ALTER path on both dialects, and Alembic
        // reflection all share.
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
    let create_sql = append_named_table_checks(create_sql, model)?;

    let (post_create_sqls, warnings) = post_create_artifacts(model, &check_emissions, dialect)?;
    Ok(CreateTableEmission {
        pre_create_sqls,
        create_sql,
        post_create_sqls,
        warnings,
    })
}

/// `CREATE [UNIQUE] INDEX` for one index: the one renderer of every door's
/// index (the create pass, the reconciliation pass, the generator).
///
/// `mode` is the token after `INDEX` ([`IndexMode`]): `IF NOT EXISTS` for
/// every door's plain statement, `CONCURRENTLY` for the generator's Postgres
/// index step (ADR-0044). The two renderings differ by that token only.
pub(crate) fn render_index_sql(
    table_lower: &str,
    name: &str,
    columns: &[String],
    unique: bool,
    dialect: Dialect,
    mode: IndexMode,
) -> String {
    let mut stmt = Index::create()
        .name(name)
        .table(Alias::new(table_lower))
        .to_owned();
    if unique {
        stmt.unique();
    }
    for col in columns {
        stmt.col(Alias::new(col));
    }
    let bare = match dialect {
        Dialect::Sqlite => stmt.to_string(SqliteQueryBuilder),
        Dialect::Postgres => stmt.to_string(PostgresQueryBuilder),
    };
    let head = if unique {
        "CREATE UNIQUE INDEX "
    } else {
        "CREATE INDEX "
    };
    match bare.strip_prefix(head) {
        Some(rest) => format!("{head}{} {rest}", mode.create_token()),
        // sea-query always opens with `head`; never reached.
        None => bare,
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

/// Append every inline db_check fragment owned by `column` to its definition.
/// The owner is matched by constraint name, the same rule `emit_add_column`
/// uses (`ck_<table>_<col>`).
fn append_inline_checks(
    col_def: &mut ColumnDef,
    check_emissions: &[(&ferro_schema_ir::SchemaCheck, CheckEmission)],
    table: &str,
    column: &str,
) {
    let owned_name = db_check_constraint_name(table, column);
    for (check, emission) in check_emissions {
        if check.name == owned_name
            && let Some(inline) = &emission.inline
        {
            col_def.extra(inline.clone());
        }
    }
}

fn post_create_artifacts(
    model: &SchemaModel,
    check_emissions: &[(&ferro_schema_ir::SchemaCheck, CheckEmission)],
    dialect: Dialect,
) -> Result<(Vec<String>, Vec<String>), EmissionError> {
    let table_lower = model.table_name.as_str();
    let mut statements = Vec::new();
    let mut warnings = Vec::new();

    for (name, columns, unique) in standalone_indexes(model) {
        statements.push(render_index_sql(table_lower, &name, &columns, unique, dialect, IndexMode::Plain));
    }

    // Inline fragments already rode their column in the CREATE TABLE.
    for (_, emission) in check_emissions {
        if let Some(stmt) = &emission.statement {
            statements.push(stmt.clone());
        }
        if let Some(warning) = &emission.warning {
            warnings.push(warning.clone());
        }
    }

    // Row security lands last: the flags and policies go on after the table's
    // columns, indexes and checks exist, so nothing this pass runs against the
    // fresh table is itself filtered (PRD #406).
    let rls = row_security_statements(model, dialect).map_err(|message| EmissionError { message })?;
    statements.extend(rls.statements);
    if let Some(warning) = rls.warning {
        warnings.push(warning);
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

/// The single-column index and unique a column's own `unique` / `index` flag
/// declares, as `(name, columns, unique)`: the standalone named `uq_` unique
/// index and `idx_` index fresh-create emits (FF-B B4/D1), unique first. What
/// an `ADD COLUMN` of `col` builds.
pub(crate) fn added_column_indexes(table: &str, col: &SchemaColumn) -> Vec<(String, Vec<String>, bool)> {
    let mut out = Vec::new();
    if col.unique {
        out.push((
            single_unique_index_name(table, &col.name),
            vec![col.name.clone()],
            true,
        ));
    }
    if col.index {
        out.push((single_index_name(table, &col.name), vec![col.name.clone()], false));
    }
    out
}

/// The `ALTER TABLE … ADD COLUMN` emission for one new column of an existing
/// table, with everything that rides it: its backfill-default drop, its
/// single-column unique/index, its column check and its foreign key. A native
/// enum column carries the idempotent type guard ahead of the add unless
/// `types_created_by_plan` names its type — that plan's `CreateEnumType` op
/// already created it.
///
/// `constraints` is [`ConstraintMode::Plain`] on every door but the
/// generator, which adds the column's foreign key and check `NOT VALID` on an
/// existing Postgres table (ADR-0043).
pub(crate) fn emit_add_column(
    table: &str,
    column: &str,
    model: &SchemaModel,
    dialect: Dialect,
    types_created_by_plan: &BTreeSet<String>,
    constraints: ConstraintMode,
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
                 be added to existing tables. Generate a reviewed migration with \
                 `ferro migrate new`.",
                table, column
            ),
        });
    }

    // Field defaults are Pydantic/client defaults. ADD COLUMN uses them only
    // as a temporary backfill for NOT NULL; nullable adds stay DEFAULT-free.
    let json_backfill = if col.nullable {
        None
    } else {
        col.default
            .as_ref()
            .and_then(|d| render_json_backfill_default(d, &storage, dialect))
    };
    let scalar_backfill = if col.nullable || json_backfill.is_some() {
        None
    } else {
        col.default.as_ref().and_then(literal_default_value)
    };
    let has_backfill = json_backfill.is_some() || scalar_backfill.is_some();
    if !col.nullable && !has_backfill {
        return Err(EmissionError {
            message: format!(
                "Cannot add NOT NULL column '{}.{}' to an existing table: it has no \
                 literal default to backfill existing rows. Make the field nullable, \
                 give it a literal default, or generate a reviewed migration with \
                 `ferro migrate new`.",
                table, column
            ),
        });
    }

    let mut col_def = ColumnDef::new(Alias::new(column));
    apply_resolved_storage(&mut col_def, &storage, ld);
    if !col.nullable {
        col_def.not_null();
    }
    if let Some(expr) = &json_backfill {
        col_def.default(Expr::cust(expr.clone()));
    } else if let Some(default_value) = &scalar_backfill {
        col_def.default(default_value.clone());
    }

    // The column's db_check: Postgres runs its idempotent ALTER after the add;
    // SQLite carries it inline on the added column (#514).
    let owned_check = db_check_constraint_name(table, column);
    let check_emissions: Vec<(&ferro_schema_ir::SchemaCheck, CheckEmission)> = model
        .checks
        .iter()
        .filter(|check| check.name == owned_check)
        .map(|check| (check, render_db_check(table, check, dialect, constraints)))
        .collect();
    append_inline_checks(&mut col_def, &check_emissions, table, column);

    // SQLite's ADD COLUMN accepts a column-level REFERENCES clause only when
    // the added column's default is NULL — a nullable add, which never carries
    // a DEFAULT here. Any other shape keeps its column and warns below.
    let fk = model.foreign_keys.iter().find(|fk| fk.column == column);
    let sqlite_inline_fk = dialect == Dialect::Sqlite && col.nullable;
    if let Some(fk) = fk
        && sqlite_inline_fk
    {
        col_def.extra(render_sqlite_add_column_references(fk));
    }

    let stmt = Table::alter()
        .table(Alias::new(table))
        .add_column(&mut col_def)
        .to_owned();

    let mut result = EmissionResult::default();
    // A native-enum column needs its type to exist first (idempotent guard).
    if let ResolvedStorage::PgEnum { type_name, labels } = &storage
        && !types_created_by_plan.contains(type_name)
    {
        result
            .statements
            .push(render_pg_enum_create_type(type_name, labels));
    }
    result.statements.push(match dialect {
        Dialect::Sqlite => stmt.to_string(SqliteQueryBuilder),
        Dialect::Postgres => stmt.to_string(PostgresQueryBuilder),
    });

    if has_backfill && dialect == Dialect::Postgres {
        result.statements.push(format!(
            "ALTER TABLE {} ALTER COLUMN {} DROP DEFAULT",
            quote_ident(table),
            quote_ident(column)
        ));
    }

    for (name, columns, unique) in added_column_indexes(table, col) {
        result.statements.push(render_index_sql(
            table,
            &name,
            &columns,
            unique,
            dialect,
            IndexMode::Plain,
        ));
    }

    for (_, emission) in check_emissions {
        if let Some(stmt) = emission.statement {
            result.statements.push(stmt);
        }
        if let Some(warning) = emission.warning {
            result.warnings.push(warning);
        }
    }

    if let Some(fk) = fk {
        match dialect {
            Dialect::Postgres => result.statements.push(render_add_fk_sql(table, fk, constraints)),
            Dialect::Sqlite if sqlite_inline_fk => {}
            Dialect::Sqlite => result.warnings.push(format!(
                "Added foreign-key column '{}.{}' without its FOREIGN KEY constraint: SQLite's \
                 ADD COLUMN accepts a REFERENCES clause only for a column whose default is \
                 NULL, and this column is NOT NULL with a backfill default. Referential \
                 integrity for this column is not database-enforced; generate a reviewed \
                 migration with `ferro migrate new` to rebuild the table with the constraint.",
                table, column
            )),
        }
    }

    Ok(result)
}

/// The SQL value existing rows get for a `NOT NULL` column that is new to
/// them, decided as [`emit_add_column`] decides its backfill `DEFAULT`: a
/// JSON container rendered for the column's storage, else the scalar literal.
/// `None` for a nullable column, or one with no literal default (rows then
/// get no value, and a `NOT NULL` column refuses them).
///
/// # Errors
/// The column's storage cannot be resolved.
pub(crate) fn backfill_value_sql(
    col: &SchemaColumn,
    dialect: Dialect,
) -> Result<Option<String>, EmissionError> {
    if col.nullable {
        return Ok(None);
    }
    let Some(default) = &col.default else {
        return Ok(None);
    };
    let storage =
        resolve_column_storage(col, dialect).map_err(|message| EmissionError { message })?;
    if let Some(json) = render_json_backfill_default(default, &storage, dialect) {
        return Ok(Some(json));
    }
    Ok(literal_default_value(default).map(|value| match dialect {
        Dialect::Sqlite => SqliteQueryBuilder.value_to_string(&value),
        Dialect::Postgres => PostgresQueryBuilder.value_to_string(&value),
    }))
}

/// `ALTER TABLE ... ADD CONSTRAINT ... FOREIGN KEY ... ON DELETE ...` for one
/// IR foreign key. Postgres-only — SQLite cannot add table constraints to an
/// existing table: a nullable added column carries
/// `render_sqlite_add_column_references` inline, and every other shape warns.
/// `mode` appends ` NOT VALID` for the generator's staged foreign key on an
/// existing Postgres table ([`ConstraintMode`], ADR-0043).
pub(crate) fn render_add_fk_sql(
    table: &str,
    fk: &ferro_schema_ir::SchemaForeignKey,
    mode: ConstraintMode,
) -> String {
    format!(
        "ALTER TABLE {} ADD CONSTRAINT {} FOREIGN KEY ({}) REFERENCES {} ({}) ON DELETE {}{}",
        quote_ident(table),
        quote_ident(&fk_constraint_name(table, fk)),
        quote_ident(&fk.column),
        quote_ident(&fk.to_table),
        quote_ident(&fk.to_column),
        fk_action_sql(fk_action_from_str(fk.on_delete.as_deref())),
        mode.suffix(),
    )
}

/// Find the declared FK for `column` on `table` in the new-IR model.
pub(crate) fn find_foreign_key<'a>(
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

pub(crate) fn emit_alter_column_type(
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
                     '{}'. SQLite cannot change column types in place; generate a \
                     reviewed migration with `ferro migrate new` to migrate this column.",
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

pub(crate) fn emit_alter_column_nullability(
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
                     cannot change column nullability in place; generate a reviewed \
                     migration with `ferro migrate new` to migrate this column.",
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

#[cfg(test)]
pub(crate) use ferro_ddl_lowering::{
    composite_index_name as test_composite_index_name,
    composite_unique_index_name as test_composite_unique_index_name,
    db_check_constraint_name as test_db_check_constraint_name,
    single_index_name as test_single_index_name,
};
